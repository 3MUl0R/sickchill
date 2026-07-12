import datetime
from typing import TYPE_CHECKING
from urllib.parse import urljoin

from sickchill import logger, settings
from sickchill.oldbeard import helpers

session = helpers.make_session()

# nzo_ids per history request. SAB has no documented limit; this keeps the query string well short of what
# any proxy in front of it is likely to truncate.
HISTORY_CHUNK = 50

if TYPE_CHECKING:
    from sickchill.providers.result_classes import SearchResult


def send_nzb(result: "SearchResult"):
    """
    Sends an NZB to SABnzbd via the API.

    :param result: The NZBSearchResult object to send to SAB
    """

    category = settings.SAB_CATEGORY
    if result.show.is_anime:
        category = settings.SAB_CATEGORY_ANIME

    # if it aired more than 7 days ago, override with the backlog category IDs
    for curEp in result.episodes:
        if datetime.date.today() - curEp.airdate > datetime.timedelta(days=7):
            category = settings.SAB_CATEGORY_ANIME_BACKLOG if result.show.is_anime else settings.SAB_CATEGORY_BACKLOG

    # set up a dict with the URL params in it
    params = {"output": "json"}
    if settings.SAB_USERNAME:
        params["ma_username"] = settings.SAB_USERNAME
    if settings.SAB_PASSWORD:
        params["ma_password"] = settings.SAB_PASSWORD
    if settings.SAB_APIKEY:
        params["apikey"] = settings.SAB_APIKEY

    if category:
        params["cat"] = category

    if result.priority:
        params["priority"] = 2 if settings.SAB_FORCED else 1

    logger.info("Sending NZB to SABnzbd")
    url = urljoin(settings.SAB_HOST, "api")

    if result.is_nzb:
        params["mode"] = "addurl"
        params["name"] = result.url
        json_response = helpers.getURL(url, params=params, session=session, returns="json", verify=False)
    elif result.is_nzbdata:
        params["mode"] = "addfile"
        multi_part_params = {"nzbfile": (f"{result.name}.nzb", result.extraInfo[0])}
        json_response = helpers.getURL(url, params=params, files=multi_part_params, session=session, returns="json", verify=False)
    else:
        json_response = {"error": "This result was a torrent, maybe from jackett? Please report."}

    if not json_response:
        logger.info("Error connecting to sab, no data returned")
        return False

    logger.debug(f"Result text from SAB: {json_response}")

    status, error_ = _check_sab_response(json_response)
    if status:
        result.client_id = _get_nzo_id(json_response)
    return status


def get_job_states(client_ids):
    """
    Ask SAB what became of the given jobs.

    :param client_ids: nzo_ids to look up.
    :return: {nzo_id: "queued" | "Completed" | "Failed" | <whatever SAB called it>}. Ids SAB knows nothing
        about are simply absent -- the caller must not read that as failure until it has seen it repeatedly.
    :raise: ValueError if SAB cannot be reached or answers with something unrecognisable. Never guess.
    """
    states = {}

    queue = _api_call({"mode": "queue"})
    for slot in queue.get("queue", {}).get("slots", []):
        if slot.get("nzo_id"):
            states[slot["nzo_id"]] = "queued"

    # SAB truncates on very long URLs, so ask in batches. Verified against SAB 5.0.4: the nzo_ids filter
    # returns exactly the requested slots.
    remaining = [client_id for client_id in client_ids if client_id not in states]
    for offset in range(0, len(remaining), HISTORY_CHUNK):
        chunk = remaining[offset : offset + HISTORY_CHUNK]
        history = _api_call({"mode": "history", "nzo_ids": ",".join(chunk)})
        for slot in history.get("history", {}).get("slots", []):
            if slot.get("nzo_id"):
                states[slot["nzo_id"]] = slot.get("status") or ""

    return states


def delete_history_item(client_id):
    """
    Remove a failed job from SAB's history together with its files -- the incomplete leftovers and the
    `_FAILED_` folder SAB leaves in the completed directory when post-processing fails.

    archive=0 matters: SAB >= 4.2 otherwise moves the entry to its archive instead of deleting it.
    Idempotent on SAB's side (deleting an unknown id answers {"status": true}, verified against 5.0.4).

    :return: True when SAB confirmed the delete. Never raises: cleanup is best-effort and must not
        disturb the reconciliation cycle that asked for it.
    """
    try:
        jdata = _api_call({"mode": "history", "name": "delete", "value": client_id, "del_files": 1, "archive": 0})
        if not jdata.get("status"):
            logger.debug(f"SAB declined to delete history item {client_id}: {jdata}")
            return False
        return True
    except Exception as error:
        logger.debug(f"Could not delete history item {client_id} from SAB: {error}")
        return False


def _api_call(params):
    """Call the SAB api and return the parsed body, raising rather than returning junk on any problem."""
    params = dict(params, output="json")
    if settings.SAB_USERNAME:
        params["ma_username"] = settings.SAB_USERNAME
    if settings.SAB_PASSWORD:
        params["ma_password"] = settings.SAB_PASSWORD
    if settings.SAB_APIKEY:
        params["apikey"] = settings.SAB_APIKEY

    url = urljoin(settings.SAB_HOST, "api")
    jdata = helpers.getURL(url, params=params, session=session, returns="json", verify=False)

    if not isinstance(jdata, dict) or "error" in jdata:
        raise ValueError(f"SAB did not answer {params['mode']}: {jdata}")

    return jdata


def _get_nzo_id(jdata) -> str:
    """
    Pull the queued job's id out of an addurl/addfile response.

    SAB answers {"status": true, "nzo_ids": ["<uuid>"]}. Without the id there is no way to ask SAB later
    whether the job succeeded, so the download is untracked -- that is a degradation, not an error.
    """
    try:
        return str(jdata["nzo_ids"][0])
    except (KeyError, IndexError, TypeError):
        logger.debug("SAB accepted the nzb but returned no nzo_id; this download will not be tracked")
        return ""


def _check_sab_response(jdata):
    """
    Check response from SAB

    :param jdata: Response from requests api call
    :return: a list of (Boolean, string) which is True if SAB is not reporting an error
    """
    if "error" in jdata:
        logger.exception(jdata["error"])
        return False, jdata["error"]
    else:
        return True, jdata


def get_sab_acces_method(host=None):
    """
    Find out how we should connect to SAB

    :param host: hostname where SAB lives
    :return: (boolean, string) with True if method was successful
    """
    params = {"mode": "auth", "output": "json"}
    url = urljoin(host, "api")
    data = helpers.getURL(url, params=params, session=session, returns="json", verify=False)
    if not data:
        return False, data

    return _check_sab_response(data)


def test_client_connection(host=None, username=None, password=None, apikey=None):
    """
    Sends a simple API request to SAB to determine if the given connection information is connect

    :param host: The host where SAB is running (incl port)
    :param username: The username to use for the HTTP request
    :param password: The password to use for the HTTP request
    :param apikey: The API key to provide to SAB
    :return: A tuple containing the success boolean and a message
    """

    # build up the URL parameters
    params = {"mode": "queue", "output": "json", "ma_username": username, "ma_password": password, "apikey": apikey}

    url = urljoin(host, "api")

    data = helpers.getURL(url, params=params, session=session, returns="json", verify=False)
    if not data:
        return False, data

    # check the result and determine if it's good or not
    result, message = _check_sab_response(data)
    if not result:
        return False, message

    return True, "Success"
