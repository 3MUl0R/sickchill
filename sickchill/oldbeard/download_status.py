"""Ask the download client what became of the episodes we snatched.

SickChill has always been fire-and-forget: it sends an nzb and never asks again. The only automatic failure
signal it understands is a `_FAILED_` directory turning up in the post-processing folder, which requires the
client to post-process a failed job into the completed directory. Clients configured not to do that -- and any
job that is deleted, aborted, or silently stalls -- are never noticed, so the episode sits in a snatched status
forever. The searches never rescue it either: a dead snatch at a satisfying quality looks identical to a good
one, so the daily and backlog searches skip it.

This walks the pending_downloads rows written by snatch_episode, asks the client about each job, and hands the
genuinely failed ones to a FailedQueueItem, which blocks the release and puts the episode back to WANTED.

The reconciler itself never touches an episode's status. Its only powers are to update its own rows, delete its
own rows, and enqueue. That keeps every status write behind the episode lock in History.mark_failed.
"""

import threading
import time

from sickchill import logger, settings
from sickchill.oldbeard import db, nzbget, sab, search_queue
from sickchill.oldbeard.common import ARCHIVED, DOWNLOADED, FAILED, IGNORED, SKIPPED, SNATCHED, SNATCHED_BEST, SNATCHED_PROPER, WANTED, Quality
from sickchill.show.Show import Show

# The episode is still ours to reconcile.
SNATCHED_STATUSES = (SNATCHED, SNATCHED_PROPER, SNATCHED_BEST)
# The rest of the system has resolved the episode; the row has done its job.
RESOLVED_STATUSES = (DOWNLOADED, ARCHIVED, WANTED, IGNORED, SKIPPED)

# Ceiling on failures actioned per cycle. A classification bug should cost twenty episodes, not the library.
MAX_FAILURES_PER_CYCLE = 20

CLIENTS = {"sabnzbd": sab, "nzbget": nzbget}


class DownloadStatusUpdater(object):
    def __init__(self):
        self.amActive = False
        self.lock = threading.Lock()

    def run(self, force=False):
        if not settings.USE_FAILED_DOWNLOADS:
            return

        self.amActive = True
        try:
            self._reconcile()
        finally:
            self.amActive = False

    def _reconcile(self):
        main_db_con = db.DBConnection()
        rows = main_db_con.select("SELECT * FROM pending_downloads ORDER BY snatch_time")
        if not rows:
            return

        now = int(time.time())
        rows = [row for row in rows if not self._resolve_safely(main_db_con, row)]
        if not rows:
            return

        # Rows belonging to a client we no longer talk to can never be verified. They are never failed; they
        # age out below.
        module = CLIENTS.get(settings.NZB_METHOD)
        states = {}
        if module:
            queryable = [row["client_id"] for row in rows if row["client"] == settings.NZB_METHOD]
            if queryable:
                try:
                    states = module.get_job_states(queryable)
                except Exception as error:
                    logger.info(f"Could not ask {settings.NZB_METHOD} about its jobs, will try again next cycle: {error}")
                    return

        failing = []
        for row in rows:
            try:
                if row["state"] == "failing":
                    failing.append(row)
                elif row["client"] != settings.NZB_METHOD or not module:
                    self._backstop(main_db_con, row, now)
                elif self._classify(main_db_con, row, states, now):
                    failing.append(row)
            except Exception as error:
                logger.debug(f"Could not classify the pending download of {row['release_name']}, skipping it this cycle: {error}")

        self._enqueue(main_db_con, failing[:MAX_FAILURES_PER_CYCLE], now)
        deferred = len(failing) - MAX_FAILURES_PER_CYCLE
        if deferred > 0:
            logger.warning(f"{deferred} failed downloads deferred to the next cycle by the per-cycle cap of {MAX_FAILURES_PER_CYCLE}")

    def _resolve_safely(self, main_db_con, row):
        """_resolve, but one unhappy row cannot take the whole cycle down with it.

        Show.find raises on an ambiguous indexer id, and a row can outlive the show it names in ways this code
        has not thought of. Skipping the row costs one cycle; letting it escape costs every other row.
        """
        try:
            return self._resolve(main_db_con, row)
        except Exception as error:
            logger.debug(f"Could not reconcile the pending download of {row['release_name']}, skipping it this cycle: {error}")
            return True

    def _resolve(self, main_db_con, row):
        """True when the row has outlived its purpose and has been deleted.

        The episode's *stored* status is what settles this, never the in-memory one: the post-processor sets
        DOWNLOADED in memory, then moves the file and fetches subtitles, and only then commits. Deleting the
        row on the in-memory value would drop the download's tracking while the import could still fail.

        Every delete here is job-scoped, because the status is read and acted on in two steps. A snatch landing
        between them replaces the row with one describing a live download, and an episode-scoped delete would
        drop that new download from tracking for good. Scoped, the delete simply matches nothing and the new
        row is reconciled on the next cycle.
        """
        show = Show.find(settings.show_list, row["showid"])
        episode_object = show.get_episode(row["season"], row["episode"]) if show else None
        if not episode_object:
            self._delete_job(main_db_con, row)
            return True

        stored = main_db_con.select_one(
            "SELECT status FROM tv_episodes WHERE showid = ? AND season = ? AND episode = ?",
            [row["showid"], row["season"], row["episode"]],
        )
        if not stored:
            self._delete_job(main_db_con, row)
            return True

        status = Quality.splitCompositeStatus(int(stored["status"] or -1))[0]

        if status in RESOLVED_STATUSES:
            # Post-processing imported it, or someone reset the episode. Either way it is no longer ours.
            # A 'failing' row reaching WANTED means the FailedQueueItem finished its work.
            self._delete_job(main_db_con, row)
            return True

        if status in SNATCHED_STATUSES:
            return False

        if status == FAILED and row["state"] == "failing":
            # mark_failed wrote FAILED and did not get as far as reverting. Keep the row: it is the only
            # record of the work left to do, and mark_failed will resume from it.
            return False

        # FAILED without us having asked for it, or a status we do not model. Not ours to reconcile.
        self._delete_job(main_db_con, row)
        return True

    def _classify(self, main_db_con, row, states, now):
        """Decide what the client's answer means for this row. True when it should be failed."""
        state = states.get(row["client_id"])

        if state == "queued":
            self._update(main_db_con, row, absent_count=0)
            return False

        if state == "Completed":
            if row["state"] != "completed":
                # Downloaded, but post-processing owns it from here. The row stays until the episode's stored
                # status says the import landed -- deleting it now would lose the episode if the import fails.
                self._update(main_db_con, row, state="completed", state_time=now, absent_count=0)
                return False

            # It finished, and the episode never left a snatched status, so post-processing did not pick it
            # up. That is a post-processing gap, not a bad release: warn, never fail, and let it age out.
            stuck_for = now - row["state_time"]
            if not row["warned"] and stuck_for > settings.FAILED_DOWNLOAD_PP_STUCK_HOURS * 3600:
                logger.warning(
                    f"{row['release_name']} finished downloading {stuck_for // 3600} hours ago but the episode is still snatched. "
                    "Post-processing has not picked it up."
                )
                self._update(main_db_con, row, warned=1)

            self._expire(main_db_con, row, now, settings.FAILED_DOWNLOAD_ROW_TTL_DAYS * 86400, row["state_time"])
            return False

        if state == "Failed":
            return True

        if state is None:
            # Absent from both the queue and the history. Could be a deleted job, or a purged history. Wait
            # for several successful polls to agree before believing it.
            absent = min(int(row["absent_count"]) + 1, settings.FAILED_DOWNLOAD_ABSENT_CYCLES)
            self._update(main_db_con, row, absent_count=absent)
            vanished = now - row["snatch_time"] > settings.FAILED_DOWNLOAD_VANISHED_HOURS * 3600
            if absent >= settings.FAILED_DOWNLOAD_ABSENT_CYCLES and vanished:
                return True
            self._backstop(main_db_con, row, now)
            return False

        # Some status this version of SickChill has never heard of. Leave it alone rather than risk a storm.
        logger.debug(f"{settings.NZB_METHOD} reports {row['release_name']} as {state!r}, which means nothing to us. Leaving it be.")
        self._backstop(main_db_con, row, now)
        return False

    def _enqueue(self, main_db_con, rows, now):
        by_show_season = {}
        for row in rows:
            if int(row["enqueue_count"]) >= settings.FAILED_DOWNLOAD_MAX_ENQUEUES:
                logger.error(
                    f"Giving up on {row['release_name']}: {row['enqueue_count']} retry attempts and the episode is still snatched. Removing it from tracking."
                )
                self._delete_job(main_db_con, row)
                continue
            by_show_season.setdefault((row["showid"], row["season"]), []).append(row)

        for (showid, season), season_rows in by_show_season.items():
            try:
                show = Show.find(settings.show_list, showid)
            except Exception as error:
                logger.debug(f"Could not look up show {showid} to retry its failed downloads: {error}")
                continue
            if not show:
                continue

            segment = []
            failed_releases = {}
            for row in season_rows:
                episode_object = show.get_episode(season, row["episode"])
                if not episode_object or settings.searchQueueScheduler.action.is_episode_in_queue(episode_object):
                    continue

                # Durable before the enqueue: the queue lives in memory, so a crash between the two would
                # otherwise lose the failure. A 'failing' row is picked up and re-enqueued next cycle.
                #
                # The rowcount is the superseded-job check. Between classifying this row and now, snatch_episode
                # may have replaced it with a live download; the update then matches nothing and the failure we
                # are holding is about a job that no longer exists. Enqueueing it would retry over the new one.
                if not self._update(main_db_con, row, state="failing", state_time=now, enqueue_count=int(row["enqueue_count"]) + 1):
                    logger.debug(f"Not retrying {row['release_name']}, the episode has been snatched again since it failed")
                    continue

                segment.append(episode_object)
                failed_releases[(season, row["episode"])] = {
                    "release": row["release_name"],
                    "provider": row["provider"],
                    "size": row["size"],
                    "old_status": row["old_status"],
                    "client_id": row["client_id"],
                }

            if segment:
                logger.info(f"Retrying {len(segment)} failed download(s) for {show.name} season {season}")
                settings.searchQueueScheduler.action.add_item(search_queue.FailedQueueItem(show, segment, failed_releases=failed_releases))

    def _backstop(self, main_db_con, row, now):
        """The ceiling for a row the client will not account for. Never fails the episode; only stops tracking.

        Rows the client still reports as queued never reach here: those downloads are alive, however long they
        take, and their row lives as long as the job does. Everything else expires, so no state keeps a row
        forever.
        """
        self._expire(main_db_con, row, now, settings.FAILED_DOWNLOAD_ROW_MAX_AGE_DAYS * 86400, row["snatch_time"])

    def _expire(self, main_db_con, row, now, ttl_seconds, since):
        if now - since > ttl_seconds:
            logger.warning(
                f"Giving up tracking {row['release_name']}; the download client has had nothing useful to say about it for "
                f"{ttl_seconds // 86400} days. The episode is left exactly as it is."
            )
            self._delete_job(main_db_con, row)

    @staticmethod
    def _delete_job(main_db_con, row):
        """Stop tracking this row's job, and only this row's job.

        No reconciliation decision is ever applied by episode. Each one is about a single download, taken from
        a row that was read some statements ago; a snatch in the meantime replaces that row with a live
        download, and an episode-scoped delete would drop it from tracking forever. Naming the job makes the
        delete match nothing in that ordering, which is right.

        Deleting a whole episode's rows is a lifecycle concern, and belongs to the two places that own an
        episode's existence: tv.py's delete_episode and delete_show.
        """
        main_db_con.action(
            "DELETE FROM pending_downloads WHERE showid = ? AND season = ? AND episode = ? AND client_id = ?",
            [row["showid"], row["season"], row["episode"], row["client_id"]],
        )
        return True

    @staticmethod
    def _update(main_db_con, row, **columns):
        """Update a row, but only while it still tracks the job we made this decision about.

        client_id is the concurrency token. A re-snatch replaces the row, and every decision made about the
        job it replaced is void: acting on it would fail a download that is currently working.

        :return: rows matched. Zero means the job was superseded and the caller's decision is stale.
        """
        assignments = ", ".join(f"{column} = ?" for column in columns)
        result = main_db_con.action(
            f"UPDATE pending_downloads SET {assignments} WHERE showid = ? AND season = ? AND episode = ? AND client_id = ?",
            list(columns.values()) + [row["showid"], row["season"], row["episode"], row["client_id"]],
        )
        return getattr(result, "rowcount", 0)
