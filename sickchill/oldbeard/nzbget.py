import datetime
import http.client
import xmlrpc.client
from base64 import standard_b64encode
from typing import TYPE_CHECKING

from sickchill import logger, settings
from sickchill.helper.common import try_int
from sickchill.oldbeard.common import Quality
from sickchill.oldbeard.helpers import make_context

if TYPE_CHECKING:
    from sickchill.providers.result_classes import SearchResult


def get_proxy(https: bool, host: str, username: str, password: str, verify: bool) -> xmlrpc.client.ServerProxy:
    scheme = "http"
    if https:
        scheme += "s"
    return xmlrpc.client.ServerProxy(f"{scheme}://{username}:{password}@{host}/xmlrpc", context=make_context(verify))


def get_job_states(client_ids):
    """
    Ask nzbget what became of the given jobs.

    :param client_ids: NZBIDs, as strings.
    :return: {nzbid: "queued" | "Completed" | "Failed" | <nzbget's own status>}. Unknown ids are absent.
    :raise: ValueError when nzbget cannot be reached. Never guess.
    """
    try:
        proxy = get_proxy(settings.NZBGET_USE_HTTPS, settings.NZBGET_HOST, settings.NZBGET_USERNAME, settings.NZBGET_PASSWORD, settings.SSL_VERIFY)

        states = {str(group["NZBID"]): "queued" for group in proxy.listgroups(0)}

        wanted = set(client_ids) - set(states)
        if wanted:
            for entry in proxy.history(False):
                nzbid = str(entry.get("NZBID"))
                if nzbid not in wanted:
                    continue
                # nzbget reports e.g. "SUCCESS/ALL", "FAILURE/PAR", "DELETED/MANUAL". Only the family matters.
                family = str(entry.get("Status") or "").split("/")[0]
                states[nzbid] = {"SUCCESS": "Completed", "FAILURE": "Failed"}.get(family, family)

        return states
    except Exception as error:
        raise ValueError(f"Could not ask nzbget about its jobs: {error}")


def delete_history_item(client_id):
    """
    Delete a failed job's history record from nzbget (modern 3-argument editqueue signature).

    nzbget takes no del-files flag here: current versions remove a FAILED download's remaining files
    themselves when its history record is deleted. Versions old enough to reject this signature simply
    fail the call and keep their history.

    :return: True when nzbget confirmed the delete. Never raises: cleanup is best-effort and must not
        disturb the reconciliation cycle that asked for it.
    """
    try:
        proxy = get_proxy(settings.NZBGET_USE_HTTPS, settings.NZBGET_HOST, settings.NZBGET_USERNAME, settings.NZBGET_PASSWORD, settings.SSL_VERIFY)
        return bool(proxy.editqueue("HistoryFinalDelete", "", [int(client_id)]))
    except Exception as error:
        logger.debug(f"Could not delete history item {client_id} from nzbget: {error}")
        return False


def send_nzb(result: "SearchResult", proper=False) -> bool:
    """
    Sends NZB to NZBGet client

    :param result: search result
    :param proper: True if this is a Proper download, False if not. Defaults to False
    """
    if not settings.NZBGET_HOST:
        logger.warning("No NZBget host found in configuration. Please configure it.")
        return False

    proxy = get_proxy(settings.NZBGET_USE_HTTPS, settings.NZBGET_HOST, settings.NZBGET_USERNAME, settings.NZBGET_PASSWORD, settings.SSL_VERIFY)
    try:
        if proxy.writelog("INFO", _("SickChill connected to drop off {name} any moment now.").format(name=f"{result.name}.nzb")):
            logger.debug("Successfully connected to NZBget")
        else:
            logger.warning("Successfully connected to NZBget, but unable to send a message")

    except http.client.error:
        logger.warning("Please check your NZBget host and port (if it is running). NZBget is not responding to this combination")
        return False

    except xmlrpc.client.ProtocolError as error:
        if error.errmsg == "Unauthorized":
            logger.warning("NZBget username or password is incorrect.")
        else:
            logger.exception(f"Protocol Error: {error}")
        return False

    newest_episode_age = datetime.date.today() - max({episode.airdate for episode in result.episodes if episode.airdate})
    if newest_episode_age <= datetime.timedelta(days=7):
        add_to_top = True
        nzbget_priority = settings.NZBGET_PRIORITY

        category = settings.NZBGET_CATEGORY
        if result.show.is_anime:
            category = settings.NZBGET_CATEGORY_ANIME

    else:
        add_to_top = False
        nzbget_priority = 0

        category = settings.NZBGET_CATEGORY_BACKLOG
        if result.show.is_anime:
            category = settings.NZBGET_CATEGORY_ANIME_BACKLOG

    dupe_key = ""
    # if it aired recently make it high priority and generate DupeKey/Score
    for curEp in result.episodes:
        if not dupe_key:
            dupe_key = f"SickChill-{curEp.show.indexerid}"
        dupe_key += f"-{curEp.season:02d}{curEp.episode:02d}"

    dupe_score = result.quality or 0

    if result.show.quality and dupe_score:
        allowed_qualities, preferred_qualities = Quality.splitQuality(result.show.quality)
        if preferred_qualities:
            if result.quality == max(preferred_qualities):
                dupe_score *= 1000
            elif result.quality in preferred_qualities:
                dupe_score *= 800
        elif allowed_qualities:
            if result.quality == max(allowed_qualities):
                dupe_score *= 500
            elif result.quality in allowed_qualities:
                dupe_score *= 300

    elif dupe_score and dupe_score != Quality.UNKNOWN:
        dupe_score *= 100

    if proper:
        dupe_score += 10

    nzb_data_content = None
    if result.is_nzbdata:
        data = result.extraInfo[0]
        nzb_data_content = standard_b64encode(data)

    logger.info("Sending NZB to NZBget")
    logger.debug(f"URL: {result.url}")

    try:
        # Find out if nzbget supports priority (Version 9.0+), old versions beginning with a 0.x will use the old command
        nzbget_version_str = proxy.version()
        nzbget_version = try_int(nzbget_version_str[: nzbget_version_str.find(".")])
        if nzbget_version == 0:
            if nzb_data_content:
                nzbget_result = proxy.append(f"{result.name}.nzb", category, add_to_top, nzb_data_content)
            else:
                if result.is_nzb:
                    if not result.provider.login():
                        return False

                    data = result.provider.get_url(result.url, returns="content")
                    if not data:
                        return False

                    nzb_data_content = standard_b64encode(data)

                nzbget_result = proxy.append(f"{result.name}.nzb", category, add_to_top, nzb_data_content)
        elif nzbget_version == 12:
            if nzb_data_content is not None:
                nzbget_result = proxy.append(f"{result.name}.nzb", category, nzbget_priority, False, nzb_data_content, False, dupe_key, dupe_score, "score")
            else:
                nzbget_result = proxy.appendurl(f"{result.name}.nzb", category, nzbget_priority, False, result.url, False, dupe_key, dupe_score, "score")
        # v13+ has a new combined append method that accepts both (url and content)
        # also the return value has changed from boolean to integer
        # (Positive number representing NZBID of the queue item. 0 and negative numbers represent error codes.)
        elif nzbget_version >= 13:
            nzbid = proxy.append(
                f"{result.name}.nzb",
                nzb_data_content if nzb_data_content is not None else result.url,
                category,
                nzbget_priority,
                False,
                False,
                dupe_key,
                dupe_score,
                "score",
            )
            nzbget_result = nzbid > 0
            if nzbget_result:
                # Keep the NZBID so the download can be reconciled against nzbget's history later.
                # Older versions return a bool, so those downloads stay untracked.
                result.client_id = str(nzbid)
        else:
            if nzb_data_content is not None:
                nzbget_result = proxy.append(f"{result.name}.nzb", category, nzbget_priority, False, nzb_data_content)
            else:
                nzbget_result = proxy.appendurl(f"{result.name}.nzb", category, nzbget_priority, False, result.url)

        if nzbget_result:
            logger.debug("NZB sent to NZBget successfully")
            return True
        else:
            logger.warning(_("NZBget could not add {name} to the queue").format(name=f"{result.name}.nzb"))
            return False
    except Exception:
        logger.warning(_("Connect Error to NZBget: could not add {name} to the queue").format(name=f"{result.name}.nzb"))
        return False


def test_client_connection(https: bool, host: str, username: str, password: str, verify: bool):
    proxy = get_proxy(https, host, username, password, verify)

    try:
        if proxy.writelog("INFO", "SickChill connection test succeeded."):
            logger.debug("NZBget connections succeeded")
        else:
            logger.warning("NZBget connections succeeded, but unable to send a message")

        return True
    except http.client.error:
        logger.warning("NZBGet connection test failed, check your NZBget host and port (if it is running). NZBget is not responding to this combination")
        return False
