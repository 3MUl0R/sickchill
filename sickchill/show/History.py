import re
import urllib.parse
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import subliminal

from sickchill import logger, settings
from sickchill.helper.metaclasses import Singleton
from sickchill.providers.result_classes import SearchResult

if TYPE_CHECKING:
    from sickchill.oldbeard.subtitles import Scores
    from sickchill.providers.GenericProvider import GenericProvider
    from sickchill.tv import TVEpisode, TVShow

from sickchill.helper.common import remove_extension, try_int
from sickchill.helper.exceptions import EpisodeNotFoundException
from sickchill.oldbeard.common import (
    ARCHIVED,
    DOWNLOADED,
    FAILED,
    IGNORED,
    SKIPPED,
    SNATCHED,
    SNATCHED_BEST,
    SNATCHED_PROPER,
    SUBTITLED,
    UNAIRED,
    WANTED,
    Quality,
)
from sickchill.oldbeard.db import DBConnection

# Statuses mark_failed will act on. SNATCHED* is the normal case; FAILED means a previous mark_failed
# wrote the status and then died before logging or reverting, so the work should be finished, not skipped.
RESUMABLE_STATUSES = (SNATCHED, SNATCHED_PROPER, SNATCHED_BEST, FAILED)

# Statuses an episode may safely be restored to. Reverting into a SNATCHED* status would drop the episode
# into the state neither the daily nor the backlog search looks at; reverting into FAILED loops.
RESTORABLE_STATUSES = (WANTED, DOWNLOADED, ARCHIVED, SKIPPED, IGNORED, UNAIRED)


def _restorable(status) -> bool:
    """True when `status` is a composite status an episode can be safely reverted to."""
    if status is None:
        return False
    try:
        base = Quality.splitCompositeStatus(int(status))[0]
    except (TypeError, ValueError):
        return False
    return base in RESTORABLE_STATUSES


class History(object, metaclass=Singleton):
    date_format = "%Y%m%d%H%M%S"

    def __init__(self):
        self.db: DBConnection = DBConnection()  # DataSource: sickchill.db
        self.failed_db: DBConnection = DBConnection("failed.db")  # DataSource: failed.db

        self.trim()

    def remove(self, items):
        """
        Removes the selected history
        :param items: Contains the properties of the log entries to remove
        """
        query = ""

        params = []
        for item in items:
            if query:
                query += " OR "

            query += "(date IN (?) AND showid = ? AND season = ? AND episode = ?)"
            params.extend([",".join(item["dates"]), item["show_id"], item["season"], item["episode"]])

        self.db.action("DELETE FROM history WHERE " + query, params)

    def clear(self):
        """
        Clear all the history
        """
        # noinspection SqlWithoutWhere
        self.db.action("DELETE FROM history")

    def get(self, limit: int = 100, action: str = None):
        """
        :param limit: The maximum number of elements to return
        :param action: The type of action to filter in the history. Either 'downloaded' or 'snatched'. Anything else or
                        no value will return everything (up to ``limit``)
        :return: The last ``limit`` elements of type ``action`` in the history
        """

        action = action.lower() if isinstance(action, str) else ""

        if action == "downloaded":
            actions = Quality.DOWNLOADED
        elif action == "snatched":
            actions = Quality.SNATCHED
        else:
            actions = Quality.DOWNLOADED + Quality.SNATCHED

        limit = max(try_int(limit, 0), 0)

        # DataSource: sickchill.db
        common_sql = (
            "SELECT action, date, episode, provider, h.quality, resource, season, show_name, showid "
            "FROM history h, tv_shows s "
            "WHERE h.showid = s.indexer_id "
        )  # DataSource: sickchill.db
        replacements = ",".join(["?"] * len(actions))
        filter_sql = f"AND action IN ({replacements})"
        order_sql = "ORDER BY date DESC "

        if limit == 0:
            if actions:
                results = self.db.select(common_sql + filter_sql + order_sql, actions)
            else:
                results = self.db.select(common_sql + order_sql)
        else:
            if actions:
                results = self.db.select(common_sql + filter_sql + order_sql + "LIMIT ?", actions + [limit])
            else:
                results = self.db.select(common_sql + order_sql + "LIMIT ?", [limit])

        data = []
        for result in results:
            data.append(
                {
                    "action": result["action"],
                    "date": result["date"],
                    "episode": result["episode"],
                    "provider": result["provider"],
                    "quality": result["quality"],
                    "resource": result["resource"],
                    "season": result["season"],
                    "show_id": result["showid"],
                    "show_name": result["show_name"],
                }
            )

        return data

    def trim(self):
        """
        Remove all elements older than 30 days from the history
        """
        back_thirty_days = (datetime.today() - timedelta(days=30)).strftime(self.date_format)
        self.db.action("DELETE FROM history WHERE date < ?", [back_thirty_days])
        if settings.USE_FAILED_DOWNLOADS:
            self.failed_db.action("DELETE FROM history WHERE date < ?", [back_thirty_days])

    def _log_history_item(self, action: int, showid: int, season: int, episode: int, quality: int, resource: str, provider, version=-1):
        """
        Insert a history item in DB

        :param action: action taken (snatch, download, etc)
        :param showid: showid this entry is about
        :param season: show season
        :param episode: show episode
        :param quality: media quality
        :param resource: resource used
        :param provider: provider used
        :param version: tracked version of file (defaults to -1)
        """
        # DataSource: sickchill.db
        return self.db.action(
            "INSERT INTO history (action, date, showid, season, episode, quality, resource, provider, version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [action, datetime.today().strftime(self.date_format), showid, season, episode, quality, resource, provider, version],
        )

    def log_snatch(self, result: SearchResult):
        """
        Log history of snatch

        :param result: search result object
        """
        provider_class: "GenericProvider" = result.provider
        show: "TVShow" = result.episodes[0].show
        if provider_class:
            provider = provider_class.name
        else:
            provider = "unknown"

        for episode in result.episodes:
            self._log_history_item(
                Quality.compositeStatus(SNATCHED, result.quality),
                episode.show.indexerid,
                episode.season,
                episode.episode,
                result.quality,
                result.name,
                provider,
                result.version,
            )
            if settings.USE_FAILED_DOWNLOADS:
                self.failed_db.action(
                    'INSERT INTO history (date, size, "release", provider, showid, season, episode, old_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                    [
                        datetime.today().strftime(self.date_format),
                        result.size,
                        self.prepare_failed_name(result.name),
                        provider,
                        show.indexerid,
                        episode.season,
                        episode.episode,
                        episode.status,
                    ],
                )

    def log_download(self, episode: "TVEpisode", filename: str, quality: int, group: str = None, version: int = -1):
        """
        Log history of download

        :param episode: episode of show
        :param filename: file on disk where the download is
        :param quality: Quality of download
        :param group: Release group
        :param version: Version of file (defaults to -1)
        """
        self._log_history_item(episode.status, episode.show.indexerid, episode.season, episode.episode, quality, filename, group or -1, version)

    def log_subtitle(self, show: int, season: int, episode: int, status: int, subtitle: subliminal.subtitle.Subtitle, scores: "Scores"):
        """
        Log download of subtitle

        :param show: Showid of download
        :param season: Show season
        :param episode: Show episode
        :param status: Status of download
        :param subtitle: Result object
        :param scores: Scores named tuple
        """
        if settings.SUBTITLES_HISTORY:
            logger.debug(
                f"[{subtitle.provider_name}] Subtitle score for {subtitle.id} is: {scores.res}/{scores.percent}% (min={scores.min}/{scores.min_percent})"
            )
            status, quality = Quality.splitCompositeStatus(status)
            self._log_history_item(
                Quality.compositeStatus(SUBTITLED, quality), show, season, episode, quality, subtitle.language.opensubtitles, subtitle.provider_name
            )

    def log_failed(self, episode_object: "TVEpisode", release: str, provider: str = "", size: int = None):
        """
        Log a failed download

        :param episode_object: Episode object
        :param release: Release group
        :param provider: Provider used for snatch
        :param size: Size of the release. When known, pass it: pick_best_result rejects a result only when
            (name, size, provider) matches a failed row exactly, and the size derived below can come out as
            -1, which no real result ever has -- a row that blocks nothing.
        """

        status, quality = Quality.splitCompositeStatus(episode_object.status)
        self._log_history_item(
            Quality.compositeStatus(FAILED, quality), episode_object.show.indexerid, episode_object.season, episode_object.episode, quality, release, provider
        )

        if not settings.USE_FAILED_DOWNLOADS:
            return

        release = self.prepare_failed_name(release)

        if size is None:
            size, provider = self._derive_failed_size(release, provider)

        if not self.has_failed(release, size, provider):
            self.failed_db.action('INSERT INTO failed ("release", size, provider) VALUES (?, ?, ?)', [release, size, provider])

        self.remove_snatch(release, size, provider)

    def _derive_failed_size(self, release: str, provider: str):
        """Recover a release's size and provider from the snatch history. `release` must already be prepared.

        Only used when the caller does not know the size. The multi-size branch gives up and records -1,
        which has_failed can never match against a real result, so the resulting failed row blocks nothing.
        """
        size = -1
        sql_results = self.failed_db.select('SELECT * FROM history WHERE "release" = ?', [release])

        if not sql_results:
            logger.warning("Release not found in snatch history.")
        elif len(sql_results) > 1:
            logger.warning("Multiple logged snatches found for release")
            num_sizes = len(set(x["size"] for x in sql_results))
            providers = len(set(x["provider"] for x in sql_results))
            if num_sizes == 1:
                logger.warning("However, they're all the same size. Continuing with found size.")
                size = sql_results[0]["size"]
            else:
                logger.warning("They also vary in size. Deleting the logged snatches and recording this release with no size/provider")
                for result in sql_results:
                    self.remove_snatch(result["release"], result["size"], result["provider"])

            if providers == 1:
                logger.info("They're also from the same provider. Using it as well.")
                provider = sql_results[0]["provider"]
        else:
            size = sql_results[0]["size"]
            provider = sql_results[0]["provider"]

        return size, provider

    @staticmethod
    def prepare_failed_name(release: str):
        """Standardizes release name for failed DB"""

        fixed = urllib.parse.unquote(release)
        if fixed.endswith((".nzb", ".torrent")):
            fixed = remove_extension(fixed)

        return re.sub(r"[.\-+ ]", "_", fixed)

    def log_success(self, release):
        self.failed_db.action('DELETE FROM history WHERE "release" = ?', [self.prepare_failed_name(release)])

    def has_failed(self, release: str, size: int, provider: str = "%"):
        """
        Returns True if a release has previously failed.

        If provider is given, return True only if the release is found
        with that specific provider. Otherwise, return True if the release
        is found with any provider.

        :param release: Release name to record failure
        :param size: Size of release
        :param provider: Specific provider to search (defaults to all providers)
        :return: True if a release has previously failed.
        """

        return bool(
            self.failed_db.select_one(
                'SELECT "release" FROM failed WHERE "release" = ? AND size = ? AND provider LIKE ? ', [self.prepare_failed_name(release), size, provider]
            )
        )

    def find_old_status(self, episode_object: "TVEpisode"):
        """The most recent snatch history entry for this episode that names a status worth restoring.

        Ordered ascending so the dict comprehension's last-wins semantics keep the newest row. Rows whose
        old_status is itself a snatched status are skipped: an episode re-snatched after an earlier snatch
        died records that dead snatched status as its "original" one, and restoring it would put the episode
        right back where no searcher looks.
        """
        sql_results = self.failed_db.select(
            "SELECT episode, old_status FROM history WHERE showid = ? AND season = ? ORDER BY date ASC",
            [episode_object.show.indexerid, episode_object.season],
        )

        history_eps = {res["episode"]: res["old_status"] for res in sql_results if _restorable(res["old_status"])}
        return history_eps.get(episode_object.episode)

    def revert_episode(self, episode_object: "TVEpisode", old_status=None, expect_status=None) -> bool:
        """Restore a failed download's episode to the state it held before the snatch.

        :param old_status: a hint, usually the status recorded at snatch time. Consulted only if restorable;
            otherwise the snatch history is searched, and failing that the episode is set back to WANTED.
        :param expect_status: when given, the revert only happens while the episode still holds this exact
            composite status. snatch_episode and the post-processor both mutate an episode in memory before
            persisting it, so the in-memory value is the authoritative one and is checked first; the
            conditional UPDATE then catches a status written straight to the database by someone else.
        :return: True when the episode was reverted. False means something else owns it now.
        """
        if not settings.USE_FAILED_DOWNLOADS:
            return False

        try:
            with episode_object.lock:
                return self._revert_episode_locked(episode_object, old_status, expect_status)
        except EpisodeNotFoundException as error:
            logger.warning(f"Unable to create episode, please set its status manually: {error}")
            return False

    def _revert_episode_locked(self, episode_object: "TVEpisode", old_status=None, expect_status=None) -> bool:
        """revert_episode's body. The caller must already hold episode_object.lock.

        mark_failed holds that lock across its whole operation, so the status it wrote cannot be replaced by a
        newer snatch between the write and this revert. Without that, mark_failed could report success on an
        episode it had just lost ownership of, and its caller would search and snatch over the live download.
        """
        if not _restorable(old_status):
            old_status = self.find_old_status(episode_object)

        if not _restorable(old_status):
            old_status = WANTED
            if episode_object.location:
                logger.warning(
                    f"No restorable status found for {episode_object.pretty_name}, setting it back to WANTED even though a file exists at "
                    f"{episode_object.location}. It may be downloaded again."
                )

        logger.info(f"Reverting episode ({episode_object.season}, {episode_object.episode}): {episode_object.episode}")

        if expect_status is None:
            episode_object.status = old_status
            episode_object.save_to_db()
            return True

        if episode_object.status != expect_status:
            logger.info(f"Not reverting {episode_object.pretty_name}, something else changed its status first")
            return False

        result = self.db.action(
            "UPDATE tv_episodes SET status = ? WHERE showid = ? AND season = ? AND episode = ? AND status = ?",
            [old_status, episode_object.show.indexerid, episode_object.season, episode_object.episode, expect_status],
        )
        if not getattr(result, "rowcount", 0):
            logger.info(f"Not reverting {episode_object.pretty_name}, its stored status changed while we were reverting it")
            return False

        episode_object.status = old_status
        episode_object.save_to_db()
        return True

    def mark_failed(
        self, episode_object: "TVEpisode", release: str = None, provider: str = None, size: int = None, old_status=None, client_id=None, force=False
    ):
        """
        Mark an episode_object as failed: block the release, then restore the episode's previous status.

        :param release: the release that failed. When known, pass it; otherwise the most recent snatch of
            this episode is looked up, which is a guess when the episode was snatched more than once.
        :param size: size of that release, forwarded to log_failed.
        :param old_status: status to restore, forwarded to revert_episode.
        :param client_id: the download client's id for the job that failed. If the episode has since been
            snatched again, this will not match the id stamped on the episode and nothing is done -- failing
            the newer job would abandon a healthy download.
        :param force: skip the status check. For the Retry button, which is a deliberate user action on an
            episode that may already be downloaded.
        :return: True when the episode was marked and reverted, False when a guard declined. Callers must not
            search for an episode this declined: it belongs to a newer download, and searching would snatch
            over the top of one that is working.

        The whole operation runs under episode_object.lock. Releasing it between writing FAILED and reverting
        would let a snatch land in the gap, and this would report success on an episode it no longer owned.
        """
        if not settings.USE_FAILED_DOWNLOADS:
            return False

        logger.info(f"Marking episode as bad: [{episode_object.pretty_name}]")

        try:
            with episode_object.lock:
                # Both the status and snatch_client_id are written inside this lock by snatch_episode, so
                # comparing them here cannot see one without the other.
                #
                # The stamp is three-valued, and the difference is load-bearing:
                #   None -- this process has not snatched this episode. The stamp does not survive a restart,
                #           so it is absent rather than stale, and says nothing either way. Proceed.
                #   ""   -- snatched by a client that reports no job id: a torrent, blackhole, nzbget < 13.
                #           Still a *newer* snatch than the one that failed, so decline.
                #   "id" -- snatched as that job. Decline unless it is the job we were asked to fail.
                stamped = getattr(episode_object, "snatch_client_id", None)
                if client_id and stamped is not None and stamped != client_id:
                    logger.info(f"Not failing {episode_object.pretty_name}, it has been snatched again since this download failed")
                    return False

                status, quality = Quality.splitCompositeStatus(episode_object.status)
                if not force and status not in RESUMABLE_STATUSES:
                    logger.info(f"Not failing {episode_object.pretty_name}, its status is no longer snatched")
                    return False

                if status == FAILED:
                    # A previous mark_failed wrote this and died before logging or reverting. Finish the job,
                    # but keep the status around so the revert below stays conditional on it.
                    composite_failed = episode_object.status
                else:
                    composite_failed = Quality.compositeStatus(FAILED, quality)
                    episode_object.status = composite_failed
                    episode_object.save_to_db()

                # Read before log_failed, whose remove_snatch deletes the very row this reads.
                if old_status is None:
                    old_status = self.find_old_status(episode_object)

                if not release:
                    (release, provider) = self.find_release(episode_object)

                if release:
                    self.log_failed(episode_object, release, provider, size=size)

                return self._revert_episode_locked(episode_object, old_status=old_status, expect_status=composite_failed)

        except EpisodeNotFoundException as error:
            logger.warning(f"Unable to get episode, please set its status manually: {error}")
            return False

    def remove_snatch(self, release: str, size: int, provider: str):
        """
        Remove a snatch from history

        :param release: release to delete
        :param size: Size of release
        :param provider: Provider to delete it from
        """
        self.failed_db.action('DELETE FROM history WHERE "release" = ? AND size = ? AND provider = ?', [self.prepare_failed_name(release), size, provider])

    def find_release(self, episode_object: "TVEpisode"):
        """
        The most recently snatched release for this episode, as (release, provider).

        A guess when the episode was snatched more than once: only the caller knows which release actually
        failed. Prefer passing the release to mark_failed. Returns (None, None) when nothing is recorded.
        """

        # Search for release in snatch history. Newest first: without an order SQLite may return any row,
        # so an episode snatched twice could have the wrong -- possibly good -- release blocked.
        result = self.failed_db.select_one(
            'SELECT "release", provider FROM history WHERE showid = ? AND season = ? AND episode = ? ORDER BY date DESC',
            [episode_object.show.indexerid, episode_object.season, episode_object.episode],
        )

        if result:
            logger.debug(f"Failed release found for season ({episode_object.season}): ({result['release']})")
            return str(result["release"]), str(result["provider"])

        # Release was not found
        logger.debug(f"No releases found for season ({episode_object.season}) of ({episode_object.show.indexerid})")
        return None, None
