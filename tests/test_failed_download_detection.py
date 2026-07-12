"""Tests for actively detecting failed downloads.

The interesting cases are the concurrent ones. SickChill mutates an episode's status in memory under
episode.lock and only persists it later, outside that lock, in both snatch_episode and the post-processor. So
for the duration of a file move the database says SNATCHED while memory says DOWNLOADED. Several tests below
therefore construct that exact skew -- an in-memory status the database has not seen -- and assert we do not
act on the stale value. Those tests pass against a database-only guard and are the whole point.
"""

import os
import threading
import unittest
from unittest import mock

from sickchill import settings
from sickchill.oldbeard import db, download_status, search, search_queue
from sickchill.oldbeard.common import (
    ARCHIVED,
    DOWNLOADED,
    FAILED,
    IGNORED,
    SKIPPED,
    SNATCHED,
    SNATCHED_BEST,
    SNATCHED_PROPER,
    UNAIRED,
    UNKNOWN,
    WANTED,
    Quality,
)
from sickchill.oldbeard.databases import main as main_db
from sickchill.show.History import History, _restorable
from tests import conftest

HDTV = Quality.HDTV
FULLHDTV = Quality.FULLHDTV


def composite(status, quality=HDTV):
    return Quality.compositeStatus(status, quality)


class RestorableTest(unittest.TestCase):
    """Which statuses an episode may be reverted to.

    Reverting into a SNATCHED* status is the bug this whole change exists to stop: neither the daily nor the
    backlog search looks at SNATCHED_BEST, so the episode is not merely un-retried, it is un-retryable.
    """

    def test_snatched_and_failed_are_never_restorable(self):
        for status in (SNATCHED, SNATCHED_PROPER, SNATCHED_BEST, FAILED):
            for quality in (Quality.NONE, HDTV, FULLHDTV):
                self.assertFalse(_restorable(composite(status, quality)), f"{status}+{quality} must not be restorable")

    def test_junk_is_never_restorable(self):
        for status in (None, 0, "abc", "", [], UNKNOWN):
            self.assertFalse(_restorable(status), f"{status!r} must not be restorable")

    def test_real_statuses_are_restorable_at_every_quality(self):
        for status in (WANTED, DOWNLOADED, ARCHIVED, SKIPPED, IGNORED, UNAIRED):
            for quality in (Quality.NONE, HDTV, FULLHDTV):
                self.assertTrue(_restorable(composite(status, quality)), f"{status}+{quality} must be restorable")

    def test_downloaded_keeps_its_quality(self):
        # A failed upgrade must be restored to the exact quality already on disk, not merely to "downloaded".
        status = composite(DOWNLOADED, FULLHDTV)
        self.assertTrue(_restorable(status))
        self.assertEqual(Quality.splitCompositeStatus(status), (DOWNLOADED, FULLHDTV))


class PendingDownloadSqlTest(unittest.TestCase):
    """The UPSERT snatch_episode writes."""

    def setUp(self):
        settings.NZB_METHOD = "sabnzbd"

    def _sql(self, prior_status):
        result = mock.Mock()
        result.show.indexerid = 1
        result.client_id = "job-a"
        result.name = "Some.Release"
        result.provider.name = "NZBGeek"
        result.size = 123
        episode = mock.Mock(season=2, episode=3)
        return search._pending_download_sql(result, episode, prior_status, now=1000)

    def test_binds_match_placeholders(self):
        query, args = self._sql(WANTED)
        self.assertEqual(query.count("?"), len(args))

    def test_carry_forward_flag_set_for_snatched_prior(self):
        # An episode re-snatched while stranded in SNATCHED_BEST must not record that as its "original" status.
        for status in (SNATCHED, SNATCHED_PROPER, SNATCHED_BEST, FAILED):
            self.assertEqual(self._sql(composite(status))[1][-1], 1, f"{status} should carry forward")

    def test_carry_forward_flag_clear_for_real_prior(self):
        for status in (WANTED, composite(DOWNLOADED), composite(ARCHIVED)):
            self.assertEqual(self._sql(status)[1][-1], 0, f"{status} should overwrite")

    def test_an_untracked_snatch_stops_tracking_the_previous_download(self):
        """A torrent or blackhole snatch reports no job id, so it writes no row -- but it still supersedes the
        download before it. Left behind, the old row has the reconciler keep asking about a job whose episode
        something else is now downloading, and eventually fail that healthy download."""
        result = mock.Mock()
        result.show.indexerid = 1
        query, args = search._forget_pending_download_sql(result, mock.Mock(season=2, episode=3), "job-a")

        self.assertIn("DELETE", query)
        self.assertIn("client_id = ?", query, "the delete must name the job it supersedes, never the episode")
        self.assertEqual(args, [1, 2, 3, "job-a"])
        self.assertEqual(query.count("?"), len(args))

    def test_the_untracked_stamp_is_not_none(self):
        """None means 'this process never snatched this episode' -- an episode reloaded after a restart. An
        untracked snatch is a real, newer snatch and must be distinguishable from that."""
        self.assertIsNotNone(search.UNTRACKED_SNATCH)
        self.assertEqual(search.UNTRACKED_SNATCH, "")

    def test_upsert_carries_old_status_forward(self):
        """Run the real statement. DOWNLOADED must survive a re-snatch of a stranded episode."""
        import sqlite3

        con = sqlite3.connect(":memory:")
        con.execute(
            "CREATE TABLE pending_downloads (showid NUMERIC NOT NULL, season NUMERIC NOT NULL, episode NUMERIC NOT NULL, "
            "client_id TEXT NOT NULL, client TEXT NOT NULL, release_name TEXT NOT NULL, provider TEXT NOT NULL, "
            "size NUMERIC DEFAULT -1, old_status NUMERIC NOT NULL, state TEXT NOT NULL DEFAULT 'pending', "
            "snatch_time NUMERIC NOT NULL, state_time NUMERIC NOT NULL, absent_count NUMERIC DEFAULT 0, "
            "enqueue_count NUMERIC DEFAULT 0, warned NUMERIC DEFAULT 0, PRIMARY KEY (showid, season, episode))"
        )
        downloaded = composite(DOWNLOADED, FULLHDTV)

        def upsert(client_id, prior):
            result = mock.Mock()
            result.show.indexerid = 1
            result.client_id = client_id
            result.name = f"Release.{client_id}"
            result.provider.name = "NZBGeek"
            result.size = 1
            query, args = search._pending_download_sql(result, mock.Mock(season=2, episode=3), prior, now=1)
            con.execute(query, args)

        upsert("job-a", downloaded)  # upgrade snatch over a file on disk
        upsert("job-b", composite(SNATCHED_BEST))  # ...died, episode stranded, snatched again

        row = con.execute("SELECT client_id, old_status, state FROM pending_downloads").fetchone()
        self.assertEqual(row[0], "job-b", "the row must track the newest job")
        self.assertEqual(row[1], downloaded, "old_status must not be overwritten with SNATCHED_BEST")
        self.assertEqual(row[2], "pending")

        upsert("job-c", WANTED)  # a genuine non-snatched prior does replace it
        self.assertEqual(con.execute("SELECT old_status FROM pending_downloads").fetchone()[0], WANTED)


class SnatchEpisodeTest(unittest.TestCase):
    """Drives the real snatch_episode, because the bookkeeping it emits is the whole basis of tracking.

    An untracked snatch -- a torrent, blackhole, nzbget < 13 -- writes no row, but it still supersedes the
    download before it. Both halves of that (the stamp and the delete) live inside snatch_episode, so asserting
    on the helpers alone would pass while the function called neither.
    """

    class _Episode:
        def __init__(self, season, episode):
            self.season, self.episode = season, episode
            self.lock = threading.Lock()
            self.status = WANTED
            self.snatch_client_id = "never assigned"

        def get_sql(self):
            return ["UPDATE tv_episodes SET status = ?", [self.status]]

        def naming_pattern(self, pattern):
            return "Show - 1x02 - Name - HDTV"

    def setUp(self):
        settings.USE_FAILED_DOWNLOADS = True
        settings.ALLOW_HIGH_PRIORITY = False
        settings.USE_TRAKT = False
        settings.NZB_METHOD = "sabnzbd"
        settings.TORRENT_METHOD = "blackhole"

    def _snatch(self, *, torrent, client_id="", tracked_job="job-a", on_lock=None):
        """Drive snatch_episode. on_lock, if given, runs while the episode lock is held -- the only moment a
        concurrent snatch could not have stamped this episode."""
        episode = self._Episode(2, 3)
        result = mock.Mock()
        result.show.indexerid = 1
        result.name = "Some.Release.720p"
        result.quality = HDTV
        result.size = 100
        result.episodes = [episode]
        result.is_torrent = torrent
        result.is_nzb = not torrent
        result.client_id = client_id
        result.provider.name = "NZBGeek"

        con = mock.Mock()
        self.locked_at_commit = []

        def select_one(*args, **kwargs):
            if on_lock:
                on_lock()
            return {"client_id": tracked_job} if tracked_job else None

        con.select_one.side_effect = select_one
        # The stamp is ordered by the episode lock. If the row is not committed under that same lock, the two
        # orders can disagree, which is the whole bug.
        con.mass_action.side_effect = lambda statements: self.locked_at_commit.append(episode.lock.locked())

        def send_nzb(res, *args, **kwargs):
            res.client_id = client_id
            return True

        with mock.patch.object(search, "_download_result", return_value=True):
            with mock.patch.object(search.sab, "send_nzb", side_effect=send_nzb):
                with mock.patch.object(search, "is_first_best_match", return_value=False):
                    with mock.patch.object(search, "History"):
                        with mock.patch.object(search, "notifiers"):
                            with mock.patch.object(search, "ui"):
                                with mock.patch.object(search.db, "DBConnection", return_value=con):
                                    self.assertTrue(search.snatch_episode(result))

        self.assertTrue(con.mass_action.called, "the snatch must have committed something")
        statements = [entry for call in con.mass_action.call_args_list for entry in call[0][0] if entry]
        return episode, statements

    @staticmethod
    def _find(statements, fragment):
        return [entry for entry in statements if fragment in entry[0]]

    def test_an_untracked_torrent_snatch_forgets_the_previous_download(self):
        episode, statements = self._snatch(torrent=True)

        self.assertEqual(episode.snatch_client_id, search.UNTRACKED_SNATCH, "the stamp must say 'snatched, no job id'")
        self.assertIsNotNone(episode.snatch_client_id, "None would read as 'never snatched' and defeat the guard")
        self.assertTrue(self._find(statements, "DELETE FROM pending_downloads"), "the superseded row must be dropped")
        self.assertFalse(self._find(statements, "INSERT INTO pending_downloads"))

    def test_the_forget_delete_names_the_job_it_supersedes(self):
        """mass_action commits outside the episode lock. An episode-scoped delete queued here could land after
        a later tracked snatch wrote its own row, silently dropping that live download from tracking."""
        _, statements = self._snatch(torrent=True, tracked_job="job-a")
        query, args = self._find(statements, "DELETE FROM pending_downloads")[0]

        self.assertIn("client_id = ?", query, "the delete must name the job it supersedes")
        self.assertEqual(args, [1, 2, 3, "job-a"])

    def test_a_delete_queued_before_a_newer_snatch_does_not_remove_its_row(self):
        """The interleaving itself, run against a real table.

        Untracked snatch A reads the tracked job under the episode lock. Tracked snatch B then writes its own
        row and commits before A's mass_action runs. A's delete must miss, leaving B's live download tracked.
        An episode-scoped delete passes every other test in this class and loses B's row here.
        """
        import sqlite3

        database = sqlite3.connect(":memory:")
        database.execute("CREATE TABLE pending_downloads (showid, season, episode, client_id, PRIMARY KEY (showid, season, episode))")
        database.execute("INSERT INTO pending_downloads VALUES (1, 2, 3, 'job-a')")

        def b_commits_its_upsert():
            # B could not have stamped while A held the lock, so this is B's row landing after A released but
            # before A's queued statements ran.
            database.execute("UPDATE pending_downloads SET client_id = 'job-b' WHERE showid = 1")

        _, statements = self._snatch(torrent=True, tracked_job="job-a", on_lock=b_commits_its_upsert)

        # Now run what snatch_episode queued, exactly as mass_action would, after B's row is in place.
        for query, args in self._find(statements, "DELETE FROM pending_downloads"):
            database.execute(query, args)

        survivors = database.execute("SELECT client_id FROM pending_downloads").fetchall()
        self.assertEqual(survivors, [("job-b",)], "B's live download must still be tracked")

    def test_nothing_tracked_means_nothing_to_forget(self):
        _, statements = self._snatch(torrent=True, tracked_job=None)
        self.assertFalse(self._find(statements, "DELETE FROM pending_downloads"), "no row, no delete")

    def test_the_row_and_the_stamp_commit_under_one_lock(self):
        """A queued statement is ordered by whenever its commit runs; the stamp is ordered by the episode lock.
        Commit the row outside the lock and a tracked snatch that stamped first can commit second, leaving the
        row naming its job while the stamp names the untracked snatch that overtook it."""
        for torrent, client_id in ((False, "job-a"), (True, "")):
            with self.subTest(torrent=torrent):
                self._snatch(torrent=torrent, client_id=client_id)
                self.assertTrue(self.locked_at_commit, "the snatch committed nothing")
                self.assertTrue(all(self.locked_at_commit), "every statement must commit while the episode lock is held")

    def test_a_tracked_nzb_snatch_records_the_job(self):
        episode, statements = self._snatch(torrent=False, client_id="job-a")

        self.assertEqual(episode.snatch_client_id, "job-a")
        self.assertTrue(self._find(statements, "INSERT INTO pending_downloads"))
        self.assertFalse(self._find(statements, "DELETE FROM pending_downloads"))

    def test_an_nzb_the_client_gave_no_id_for_is_untracked(self):
        """SAB can accept a job and answer without an nzo_id. That snatch cannot be followed, and must not be
        left tracked under the previous job's id."""
        episode, statements = self._snatch(torrent=False, client_id="")

        self.assertEqual(episode.snatch_client_id, search.UNTRACKED_SNATCH)
        self.assertTrue(self._find(statements, "DELETE FROM pending_downloads"))


class SabTest(unittest.TestCase):
    def test_nzo_id_extracted(self):
        self.assertEqual(sab_module()._get_nzo_id({"status": True, "nzo_ids": ["abc"]}), "abc")

    def test_missing_nzo_id_is_not_an_error(self):
        for jdata in ({"status": True}, {"nzo_ids": []}, None, "nonsense"):
            self.assertEqual(sab_module()._get_nzo_id(jdata), "")

    def test_send_nzb_stamps_client_id(self):
        sab = sab_module()
        result = mock.Mock()
        result.is_nzb, result.is_nzbdata = True, False
        result.show.is_anime = False
        result.episodes = []
        result.priority = 0
        with mock.patch.object(sab.helpers, "getURL", return_value={"status": True, "nzo_ids": ["nzo-1"]}) as get_url:
            self.assertTrue(sab.send_nzb(result))
        self.assertTrue(get_url.called, "the fixture response must actually be consumed")
        self.assertEqual(result.client_id, "nzo-1")

    def test_get_job_states_merges_queue_and_history(self):
        sab = sab_module()
        calls = []

        def fake_api(params):
            calls.append(params)
            if params["mode"] == "queue":
                return {"queue": {"slots": [{"nzo_id": "in-queue"}]}}
            return {"history": {"slots": [{"nzo_id": "done", "status": "Completed"}, {"nzo_id": "bad", "status": "Failed"}]}}

        with mock.patch.object(sab, "_api_call", side_effect=fake_api):
            states = sab.get_job_states(["in-queue", "done", "bad", "unknown"])

        self.assertEqual(states, {"in-queue": "queued", "done": "Completed", "bad": "Failed"})
        self.assertNotIn("unknown", states, "an id the client knows nothing about must simply be absent")
        # A job already seen in the queue is not asked about again.
        self.assertNotIn("in-queue", calls[1]["nzo_ids"])

    def test_get_job_states_chunks_history_requests(self):
        sab = sab_module()
        seen = []

        def fake_api(params):
            if params["mode"] == "queue":
                return {"queue": {"slots": []}}
            seen.append(params["nzo_ids"].split(","))
            return {"history": {"slots": []}}

        with mock.patch.object(sab, "_api_call", side_effect=fake_api):
            sab.get_job_states([f"id-{n}" for n in range(sab.HISTORY_CHUNK + 5)])

        self.assertEqual(len(seen), 2)
        self.assertEqual(len(seen[0]), sab.HISTORY_CHUNK)
        self.assertEqual(len(seen[1]), 5)

    def test_api_call_raises_rather_than_guessing(self):
        sab = sab_module()
        for body in (None, "", {"error": "API Key Required"}, ["not", "a", "dict"]):
            with mock.patch.object(sab.helpers, "getURL", return_value=body):
                with self.assertRaises(ValueError):
                    sab._api_call({"mode": "queue"})

    def test_delete_history_item_deletes_files_and_skips_the_archive(self):
        """Without archive=0, SAB >= 4.2 shelves the entry instead of deleting it, and without
        del_files=1 the _FAILED_ folder this exists to remove stays on disk."""
        sab = sab_module()
        with mock.patch.object(sab.helpers, "getURL", return_value={"status": True}) as get_url:
            self.assertTrue(sab.delete_history_item("nzo-1"))

        params = get_url.call_args.kwargs["params"]
        self.assertEqual(params["mode"], "history")
        self.assertEqual(params["name"], "delete")
        self.assertEqual(params["value"], "nzo-1")
        self.assertEqual(params["del_files"], 1)
        self.assertEqual(params["archive"], 0)

    def test_delete_history_item_reports_a_declined_delete(self):
        sab = sab_module()
        with mock.patch.object(sab.helpers, "getURL", return_value={"status": False}):
            self.assertFalse(sab.delete_history_item("nzo-1"))

    def test_delete_history_item_never_raises(self):
        sab = sab_module()
        for body in (None, "nonsense", {"error": "API Key Incorrect"}):
            with mock.patch.object(sab.helpers, "getURL", return_value=body):
                self.assertFalse(sab.delete_history_item("nzo-1"))
        with mock.patch.object(sab.helpers, "getURL", side_effect=OSError("connection torn down")):
            self.assertFalse(sab.delete_history_item("nzo-1"))


def sab_module():
    from sickchill.oldbeard import sab

    return sab


class NzbgetTest(unittest.TestCase):
    def test_job_states_map_nzbget_status_families(self):
        from sickchill.oldbeard import nzbget

        proxy = mock.Mock()
        proxy.listgroups.return_value = [{"NZBID": 7}]
        proxy.history.return_value = [
            {"NZBID": 8, "Status": "SUCCESS/ALL"},
            {"NZBID": 9, "Status": "FAILURE/PAR"},
            {"NZBID": 10, "Status": "DELETED/MANUAL"},
        ]
        with mock.patch.object(nzbget, "get_proxy", return_value=proxy):
            states = nzbget.get_job_states(["7", "8", "9", "10"])

        self.assertEqual(states, {"7": "queued", "8": "Completed", "9": "Failed", "10": "DELETED"})

    def test_unreachable_client_raises(self):
        from sickchill.oldbeard import nzbget

        with mock.patch.object(nzbget, "get_proxy", side_effect=OSError("no route to host")):
            with self.assertRaises(ValueError):
                nzbget.get_job_states(["1"])

    def test_delete_history_item_uses_the_modern_signature(self):
        from sickchill.oldbeard import nzbget

        proxy = mock.Mock()
        proxy.editqueue.return_value = True
        with mock.patch.object(nzbget, "get_proxy", return_value=proxy):
            self.assertTrue(nzbget.delete_history_item("42"))

        proxy.editqueue.assert_called_once_with("HistoryFinalDelete", "", [42])

    def test_delete_history_item_never_raises(self):
        from sickchill.oldbeard import nzbget

        with mock.patch.object(nzbget, "get_proxy", side_effect=OSError("no route to host")):
            self.assertFalse(nzbget.delete_history_item("42"))
        # A pre-modern nzbget rejects the 3-argument signature with a Fault; that is a skipped
        # cleanup, not an error.
        proxy = mock.Mock()
        proxy.editqueue.side_effect = Exception("Invalid parameter")
        with mock.patch.object(nzbget, "get_proxy", return_value=proxy):
            self.assertFalse(nzbget.delete_history_item("42"))


class IsEpisodeInQueueTest(unittest.TestCase):
    """The old is_ep_in_queue compares whole segment lists and never looks at the running item."""

    def _episode(self, indexerid, season, episode):
        return mock.Mock(show=mock.Mock(indexerid=indexerid), season=season, episode=episode)

    def _queue(self):
        queue = search_queue.SearchQueue()
        queue.queue = []
        queue.currentItem = None
        return queue

    def test_matches_a_single_episode_inside_a_larger_segment(self):
        queue = self._queue()
        wanted = self._episode(1, 2, 3)
        item = mock.Mock(spec=search_queue.FailedQueueItem)
        item.segment = [self._episode(1, 2, 1), wanted, self._episode(1, 2, 5)]
        queue.queue = [item]

        self.assertTrue(queue.is_episode_in_queue(self._episode(1, 2, 3)))
        self.assertFalse(queue.is_episode_in_queue(self._episode(1, 2, 4)))

    def test_sees_the_currently_running_item(self):
        queue = self._queue()
        item = mock.Mock(spec=search_queue.FailedQueueItem)
        item.segment = [self._episode(1, 2, 3)]
        queue.currentItem = item

        self.assertTrue(queue.is_episode_in_queue(self._episode(1, 2, 3)))

    def test_ignores_other_shows(self):
        queue = self._queue()
        item = mock.Mock(spec=search_queue.FailedQueueItem)
        item.segment = [self._episode(99, 2, 3)]
        queue.queue = [item]

        self.assertFalse(queue.is_episode_in_queue(self._episode(1, 2, 3)))


class ClassifyTest(unittest.TestCase):
    """What the client's answer means for a pending row."""

    def setUp(self):
        self.updater = download_status.DownloadStatusUpdater()
        self.con = mock.Mock()
        settings.FAILED_DOWNLOAD_ABSENT_CYCLES = 3
        settings.FAILED_DOWNLOAD_VANISHED_HOURS = 12
        settings.FAILED_DOWNLOAD_PP_STUCK_HOURS = 6
        settings.FAILED_DOWNLOAD_ROW_TTL_DAYS = 14
        settings.FAILED_DOWNLOAD_ROW_MAX_AGE_DAYS = 60
        settings.NZB_METHOD = "sabnzbd"

    def _row(self, **overrides):
        row = {
            "showid": 1,
            "season": 2,
            "episode": 3,
            "client_id": "job-a",
            "client": "sabnzbd",
            "release_name": "Some.Release",
            "provider": "NZBGeek",
            "size": 1,
            "old_status": WANTED,
            "state": "pending",
            "snatch_time": 0,
            "state_time": 0,
            "absent_count": 0,
            "enqueue_count": 0,
            "warned": 0,
        }
        row.update(overrides)
        return row

    def _classify(self, row, state, now=1000):
        states = {row["client_id"]: state} if state is not None else {}
        return self.updater._classify(self.con, row, states, now)

    def test_queued_is_left_alone(self):
        self.assertFalse(self._classify(self._row(absent_count=2), "queued"))
        self.assertIn("absent_count", self.con.action.call_args[0][0])

    def test_failed_fails(self):
        self.assertTrue(self._classify(self._row(), "Failed"))

    def test_completed_moves_to_completed_and_is_never_failed(self):
        self.assertFalse(self._classify(self._row(), "Completed"))
        self.assertIn("state = ?", self.con.action.call_args[0][0])
        self.assertIn("completed", self.con.action.call_args[0][1])

    def test_completed_but_never_imported_warns_and_never_fails(self):
        # 51 of this user's dead episodes are releases SAB reports as Completed. That is a post-processing
        # gap, not a bad release: warning it is right, re-downloading it is not.
        row = self._row(state="completed", state_time=0)
        now = 7 * 3600
        self.assertFalse(self._classify(row, "Completed", now=now))
        self.assertIn("warned", self.con.action.call_args[0][0])

    def test_completed_expires_after_the_ttl_leaving_the_episode_alone(self):
        row = self._row(state="completed", state_time=0, warned=1)
        now = (settings.FAILED_DOWNLOAD_ROW_TTL_DAYS * 86400) + 1
        self.assertFalse(self._classify(row, "Completed", now=now))
        self.assertIn("DELETE", self.con.action.call_args[0][0])

    def test_expiry_deletes_only_this_job(self):
        """Expiry gives up on a download, not on an episode. If it was snatched again, that newer row is a live
        download and must keep being tracked."""
        row = self._row(state="completed", state_time=0, warned=1)
        now = (settings.FAILED_DOWNLOAD_ROW_TTL_DAYS * 86400) + 1
        self._classify(row, "Completed", now=now)

        query, args = self.con.action.call_args[0]
        self.assertIn("DELETE", query)
        self.assertIn("client_id = ?", query)
        self.assertIn("job-a", args)

    def test_absent_needs_both_repeated_polls_and_elapsed_time(self):
        vanished = settings.FAILED_DOWNLOAD_VANISHED_HOURS * 3600 + 1

        # enough time, not enough polls
        self.assertFalse(self._classify(self._row(absent_count=0), None, now=vanished))
        # enough polls, not enough time
        self.assertFalse(self._classify(self._row(absent_count=3), None, now=10))
        # both
        self.assertTrue(self._classify(self._row(absent_count=2), None, now=vanished))

    def test_absent_count_is_clamped(self):
        self.updater._update = mock.Mock()
        self._classify(self._row(absent_count=99), None, now=10)
        self.assertEqual(self.updater._update.call_args[1]["absent_count"], settings.FAILED_DOWNLOAD_ABSENT_CYCLES)

    def test_unknown_status_never_fails(self):
        # A future SAB status must not be able to cause a re-snatch storm.
        for state in ("Extracting", "Verifying", "Moving", "", "Propagating"):
            self.assertFalse(self._classify(self._row(), state), f"{state!r} must not fail the download")

    def test_backstop_deletes_a_row_the_client_will_not_account_for(self):
        row = self._row(snatch_time=0)
        now = settings.FAILED_DOWNLOAD_ROW_MAX_AGE_DAYS * 86400 + 1
        self.assertFalse(self._classify(row, "Something Odd", now=now))
        self.assertIn("DELETE", self.con.action.call_args[0][0])

    def test_a_queued_row_is_never_expired_however_old(self):
        # A job paused in SAB for a year is alive. Its row is not a leak.
        row = self._row(snatch_time=0)
        now = settings.FAILED_DOWNLOAD_ROW_MAX_AGE_DAYS * 86400 * 10
        self.assertFalse(self._classify(row, "queued", now=now))
        self.assertNotIn("DELETE", self.con.action.call_args[0][0])

    def test_updates_are_scoped_to_the_client_id(self):
        """A re-snatch replaces the row; decisions about the job it replaced must not touch it."""
        self._classify(self._row(), "queued")
        query, args = self.con.action.call_args[0]
        self.assertIn("client_id = ?", query)
        self.assertIn("job-a", args)


class ReconcileGuardTest(unittest.TestCase):
    """Only the *stored* status may delete a row: the post-processor sets DOWNLOADED in memory, then moves the
    file, and only then commits. Deleting on the in-memory value would drop tracking while the import can still
    fail."""

    def setUp(self):
        settings.USE_FAILED_DOWNLOADS = True
        settings.NZB_METHOD = "sabnzbd"
        self.updater = download_status.DownloadStatusUpdater()

    def _row(self, **overrides):
        row = {"showid": 1, "season": 2, "episode": 3, "client_id": "job-a", "state": "pending", "release_name": "R"}
        row.update(overrides)
        return row

    def _resolve(self, stored_status, row=None, episode=object()):
        con = mock.Mock()
        con.select_one.return_value = {"status": stored_status} if stored_status is not None else None
        show = mock.Mock()
        show.get_episode.return_value = episode
        with mock.patch.object(download_status.Show, "find", return_value=show):
            resolved = self.updater._resolve(con, row or self._row())
        return resolved, con

    def test_stored_downloaded_deletes_the_row(self):
        resolved, con = self._resolve(composite(DOWNLOADED))
        self.assertTrue(resolved)
        self.assertIn("DELETE", con.action.call_args[0][0])

    def test_stored_snatched_keeps_the_row(self):
        for status in (SNATCHED, SNATCHED_PROPER, SNATCHED_BEST):
            resolved, con = self._resolve(composite(status))
            self.assertFalse(resolved)
            con.action.assert_not_called()

    def test_a_failing_row_at_failed_is_kept_so_mark_failed_can_resume(self):
        # mark_failed writes FAILED, then logs, then reverts. A crash between the first two leaves FAILED with
        # the release unblocked -- deleting the row here would destroy the only record of the work left.
        resolved, con = self._resolve(composite(FAILED), row=self._row(state="failing"))
        self.assertFalse(resolved)
        con.action.assert_not_called()

    def test_a_pending_row_at_failed_is_not_ours(self):
        resolved, _ = self._resolve(composite(FAILED), row=self._row(state="pending"))
        self.assertTrue(resolved)

    def test_missing_show_or_episode_drops_the_row(self):
        resolved, con = self._resolve(composite(SNATCHED), episode=None)
        self.assertTrue(resolved)
        self.assertIn("DELETE", con.action.call_args[0][0])

    def test_resolving_deletes_only_this_job(self):
        """The stored status is read, then acted on. A snatch landing in between replaces the row with a live
        download; an episode-scoped delete would drop it from tracking for good."""
        for stored in (composite(DOWNLOADED), composite(FAILED), None):
            with self.subTest(stored=stored):
                resolved, con = self._resolve(stored)
                self.assertTrue(resolved)
                query, args = con.action.call_args[0]
                self.assertIn("DELETE", query)
                self.assertIn("client_id = ?", query)
                self.assertIn("job-a", args)

    def test_one_unhappy_row_does_not_take_the_cycle_down(self):
        """Show.find raises on an ambiguous indexer id. Skipping the row costs a cycle; letting the exception
        escape costs every other row."""
        from sickchill.helper.exceptions import MultipleShowObjectsException

        con = mock.Mock()
        with mock.patch.object(download_status.Show, "find", side_effect=MultipleShowObjectsException()):
            self.assertTrue(self.updater._resolve_safely(con, self._row()))


class PollFailureTest(unittest.TestCase):
    def setUp(self):
        settings.USE_FAILED_DOWNLOADS = True
        settings.NZB_METHOD = "sabnzbd"
        self.updater = download_status.DownloadStatusUpdater()

    def test_an_unreachable_client_fails_nothing(self):
        row = {"showid": 1, "season": 2, "episode": 3, "client_id": "a", "client": "sabnzbd", "state": "pending", "release_name": "R"}
        con = mock.Mock()
        con.select.return_value = [row]

        with mock.patch.object(download_status.db, "DBConnection", return_value=con):
            with mock.patch.object(self.updater, "_resolve", return_value=False):
                with mock.patch.object(download_status.sab, "get_job_states", side_effect=ValueError("boom")):
                    with mock.patch.object(self.updater, "_enqueue") as enqueue:
                        self.updater._reconcile()

        enqueue.assert_not_called()
        con.action.assert_not_called()


class MigrationTest(conftest.SickChillTestDBCase):
    """Runs the real AddPendingDownloads, rather than asserting things about a hand-written copy of its DDL."""

    filename = "pending_downloads_migration_test.db"

    def tearDown(self):
        # Evict the cached connection before unlinking. On Linux the unlink succeeds while db_cons still
        # holds the handle, and the next test's writes through it die with SQLITE_READONLY_DBMOVED
        # ("attempt to write a readonly database"). Windows cannot unlink an open file, which is why this
        # only ever failed in CI. Matched by substring because conftest.TestDBConnection prepends
        # TEST_DIR to the filename, so the cache key is the full path, not this bare name.
        for key in [k for k in db.db_cons if self.filename in k]:
            db.db_cons.pop(key).close()
            db.db_locks.pop(key, None)
        try:
            os.remove(os.path.join(settings.DATA_DIR, self.filename))
        except OSError:
            pass
        super().tearDown()

    def _fresh(self):
        """A database the migration has never touched, so test() must be False and execute() must do the work."""
        settings.NZB_METHOD = "sabnzbd"
        connection = db.DBConnection(self.filename)
        connection.action("DROP TABLE IF EXISTS pending_downloads")
        return connection

    def _migrate(self, connection):
        migration = main_db.AddPendingDownloads(connection)
        # backup_database wants a real db_version row, and inc_minor_version wants to write one. Neither is
        # what this test is about; the DDL is.
        with mock.patch.object(main_db, "backup_database"):
            with mock.patch.object(migration, "inc_minor_version"):
                migration.execute()
        return migration

    def test_execute_creates_the_table_and_test_flips(self):
        connection = self._fresh()
        migration = main_db.AddPendingDownloads(connection)
        self.assertFalse(migration.test(), "test() must be False before the migration runs")

        self._migrate(connection)

        self.assertTrue(migration.test(), "test() must be True once the table exists, so it never runs twice")
        self.assertTrue(connection.has_table("pending_downloads"))

    def test_the_real_ddl_accepts_the_real_upsert(self):
        """Guards the DDL and the UPSERT against drifting apart: the column list, the NOT NULLs and the
        conflict target all have to agree, and only running one against the other proves it."""
        connection = self._fresh()
        self._migrate(connection)

        # `name` is reserved on Mock's constructor, so every one of these has to be assigned after the fact.
        result = mock.Mock()
        result.show.indexerid = 1
        result.client_id = "job-a"
        result.name = "Some.Release"
        result.provider.name = "NZBGeek"
        result.size = 100

        query, args = search._pending_download_sql(result, mock.Mock(season=2, episode=3), composite(WANTED), 1000)
        connection.action(query, args)

        row = connection.select_one("SELECT * FROM pending_downloads WHERE showid = ? AND season = ? AND episode = ?", [1, 2, 3])
        self.assertEqual(row["client_id"], "job-a")
        self.assertEqual(row["state"], "pending")

    def test_the_primary_key_covers_every_lookup_so_no_index_is_needed(self):
        """There is deliberately no secondary index. Every WHERE is showid+season+episode, optionally AND
        client_id, which the primary key serves. An index named for another table's would silently no-op under
        CREATE INDEX IF NOT EXISTS, so the safest index is the one we do not create."""
        connection = self._fresh()
        self._migrate(connection)

        plan = connection.select(
            "EXPLAIN QUERY PLAN SELECT 1 FROM pending_downloads WHERE showid = ? AND season = ? AND episode = ? AND client_id = ?",
            [1, 2, 3, "job-a"],
        )
        detail = " ".join(str(row["detail"]) for row in plan)
        # assertNotIn alone would pass on an empty plan, which proves nothing.
        self.assertIn("SEARCH", detail, f"expected an indexed lookup, got: {detail!r}")
        self.assertNotIn("SCAN", detail, f"the primary key must serve this lookup, got: {detail!r}")

    def test_migration_created_the_table_on_the_real_database(self):
        # conftest upgrades the main db to the latest schema, so the table must be there.
        con = db.DBConnection()
        self.assertTrue(con.has_table("pending_downloads"))


class HistoryDatabaseTest(conftest.SickChillTestPostProcessorCase):
    """mark_failed / revert_episode against a real database."""

    def setUp(self):
        super().setUp()
        settings.USE_FAILED_DOWNLOADS = True
        self.history = History()
        self.failed_db = db.DBConnection("failed.db")
        self.main_db = db.DBConnection()
        self.failed_db.action("DELETE FROM history")
        self.failed_db.action("DELETE FROM failed")
        self.episode = self.show.get_episode(1, 1)

    def _stored_status(self):
        row = self.main_db.select_one(
            "SELECT status FROM tv_episodes WHERE showid = ? AND season = ? AND episode = ?",
            [self.show.indexerid, self.episode.season, self.episode.episode],
        )
        return row["status"]

    def _set_status(self, status):
        self.episode.status = status
        self.episode.save_to_db()

    def _log_snatch(self, release, old_status, date, size=1, provider="NZBGeek"):
        # log_snatch stores the *prepared* name (dots and dashes become underscores), and log_failed looks the
        # release up by that prepared form. A fixture holding the raw name would silently never match.
        self.failed_db.action(
            'INSERT INTO history (date, size, "release", provider, showid, season, episode, old_status) VALUES (?,?,?,?,?,?,?,?)',
            [date, size, self.history.prepare_failed_name(release), provider, self.show.indexerid, self.episode.season, self.episode.episode, old_status],
        )

    # ---- revert_episode -------------------------------------------------------------------------------

    def test_revert_persists_the_restored_status(self):
        """The old_status branch used to set the attribute and never save. Fails on master."""
        self._set_status(composite(FAILED))
        self.history.revert_episode(self.episode, old_status=composite(DOWNLOADED, FULLHDTV), expect_status=composite(FAILED))
        self.assertEqual(self._stored_status(), composite(DOWNLOADED, FULLHDTV))

    def test_revert_persists_without_an_expected_status(self):
        """The unconditional path taken by direct callers. Nothing else writes the row here, so dropping the
        save leaves the database holding the old status -- which is precisely the bug on master."""
        self._set_status(composite(FAILED))
        restored = composite(DOWNLOADED, FULLHDTV)
        self.history.revert_episode(self.episode, old_status=restored, expect_status=None)
        self.assertEqual(self._stored_status(), restored)
        self.assertEqual(self.episode.status, restored)

    def test_revert_never_restores_a_snatched_status(self):
        """Reverting into SNATCHED_BEST returns the episode to the state no searcher looks at."""
        self._set_status(composite(FAILED))
        self.history.revert_episode(self.episode, old_status=composite(SNATCHED_BEST), expect_status=composite(FAILED))
        self.assertEqual(self._stored_status(), WANTED)

    def test_revert_prefers_an_older_usable_history_row_over_a_snatched_hint(self):
        """The TTL case: the pending row was expired, so old_status is the stranded SNATCHED_BEST. The older
        DOWNLOADED snatch is still on record and must win, or a file already on disk is downloaded again."""
        downloaded = composite(DOWNLOADED, FULLHDTV)
        self._log_snatch("Old.Release", downloaded, "20260601120000")
        self._log_snatch("New.Release", composite(SNATCHED_BEST), "20260701120000")

        self._set_status(composite(FAILED))
        self.history.revert_episode(self.episode, old_status=composite(SNATCHED_BEST), expect_status=composite(FAILED))
        self.assertEqual(self._stored_status(), downloaded)

    def test_find_old_status_takes_the_newest_usable_row(self):
        self._log_snatch("A", composite(DOWNLOADED, HDTV), "20260601120000")
        self._log_snatch("B", composite(DOWNLOADED, FULLHDTV), "20260701120000")
        # ORDER BY DESC with a last-wins comprehension would pick the older row here.
        self.assertEqual(self.history.find_old_status(self.episode), composite(DOWNLOADED, FULLHDTV))

    def test_revert_falls_back_to_wanted_with_no_usable_history(self):
        self._set_status(composite(FAILED))
        self.history.revert_episode(self.episode, old_status=None, expect_status=composite(FAILED))
        self.assertEqual(self._stored_status(), WANTED)

    def test_revert_skips_when_memory_holds_a_newer_status(self):
        """The stamp-before-commit window. The database still says FAILED, but a re-snatch already owns the
        episode in memory. A database-only compare-and-swap would match and clobber it."""
        self._set_status(composite(FAILED))
        self.episode.status = composite(SNATCHED_BEST)  # in memory only; not persisted

        self.history.revert_episode(self.episode, old_status=WANTED, expect_status=composite(FAILED))

        self.assertEqual(self._stored_status(), composite(FAILED), "the revert must not have run")
        self.assertEqual(self.episode.status, composite(SNATCHED_BEST), "the newer writer keeps the episode")

    def test_revert_skips_when_the_database_changed_out_of_band(self):
        self._set_status(composite(FAILED))
        # Someone wrote the database directly (a UI setStatus) while our object still says FAILED.
        self.main_db.action(
            "UPDATE tv_episodes SET status = ? WHERE showid = ? AND season = ? AND episode = ?",
            [composite(DOWNLOADED), self.show.indexerid, self.episode.season, self.episode.episode],
        )
        self.history.revert_episode(self.episode, old_status=WANTED, expect_status=composite(FAILED))
        self.assertEqual(self._stored_status(), composite(DOWNLOADED))

    # ---- mark_failed ----------------------------------------------------------------------------------

    def test_mark_failed_declines_when_the_episode_is_no_longer_snatched(self):
        self._set_status(composite(DOWNLOADED))
        self.assertFalse(self.history.mark_failed(self.episode))
        self.assertEqual(self._stored_status(), composite(DOWNLOADED))
        self.assertEqual(self.failed_db.select("SELECT * FROM failed"), [])

    def test_force_overrides_the_status_guard_for_the_retry_button(self):
        self._set_status(composite(DOWNLOADED))
        self.assertTrue(self.history.mark_failed(self.episode, force=True))
        self.assertEqual(self._stored_status(), WANTED)

    def test_mark_failed_declines_when_the_episode_was_snatched_again(self):
        """download_status enqueued a retry for job A; job B was snatched before the queue drained."""
        self._set_status(composite(SNATCHED_BEST))
        self.episode.snatch_client_id = "job-b"

        self.assertFalse(self.history.mark_failed(self.episode, release="A.Release", client_id="job-a"))
        self.assertEqual(self._stored_status(), composite(SNATCHED_BEST), "job B's download must be left alone")
        self.assertEqual(self.failed_db.select("SELECT * FROM failed"), [], "job A's release must not be blocked here")

    def test_mark_failed_proceeds_when_the_token_matches(self):
        self._set_status(composite(SNATCHED_BEST))
        self.episode.snatch_client_id = "job-a"
        self.assertTrue(self.history.mark_failed(self.episode, release="A.Release", provider="NZBGeek", size=42, client_id="job-a"))
        self.assertEqual(self._stored_status(), WANTED)

    def test_mark_failed_proceeds_when_no_token_is_stamped(self):
        """After a restart the in-memory stamp is gone. Absence means no opinion, not a mismatch."""
        self._set_status(composite(SNATCHED_BEST))
        self.assertFalse(hasattr(self.episode, "snatch_client_id"))
        self.assertTrue(self.history.mark_failed(self.episode, release="A.Release", client_id="job-a"))

    def test_mark_failed_declines_when_a_newer_untracked_snatch_replaced_the_job(self):
        """Job A failed. Before the retry drained, the episode was snatched as a torrent, which reports no job
        id. That snatch is newer than A, so failing A here would revert an episode that is downloading now."""
        self._set_status(composite(SNATCHED_BEST))
        self.episode.snatch_client_id = search.UNTRACKED_SNATCH

        self.assertFalse(self.history.mark_failed(self.episode, release="A.Release", client_id="job-a"))
        self.assertEqual(self._stored_status(), composite(SNATCHED_BEST), "the torrent's download must be left alone")
        self.assertEqual(self.failed_db.select("SELECT * FROM failed"), [])

    def test_an_untracked_snatch_can_still_be_failed_by_a_caller_with_no_token(self):
        """The UI's retry passes no client_id (and is not anonymous -- the user is pointing at THIS
        episode deliberately). It must still be able to fail a torrent."""
        self._set_status(composite(SNATCHED_BEST))
        self.episode.snatch_client_id = search.UNTRACKED_SNATCH
        self.assertTrue(self.history.mark_failed(self.episode, release="A.Release", provider="NZBGeek", size=42))

    def test_anonymous_mark_failed_declines_when_any_snatch_is_stamped(self):
        """A _FAILED_ folder names some old job, but this process has since snatched the episode. The
        folder cannot tell its own download from the newer one, so it must not fail anyone's."""
        self._set_status(composite(SNATCHED_BEST))
        self.episode.snatch_client_id = "job-b"
        self.assertFalse(self.history.mark_failed(self.episode, release="A.Release", anonymous=True))
        self.assertEqual(self._stored_status(), composite(SNATCHED_BEST))
        self.assertEqual(self.failed_db.select("SELECT * FROM failed"), [])

    def test_anonymous_mark_failed_declines_on_an_untracked_stamp(self):
        """The torrent case a client_id-carrying caller cannot even express: the newer snatch is
        stamped "", which compares unequal to nothing. Anonymous callers decline on any stamp at all."""
        self._set_status(composite(SNATCHED))
        self.episode.snatch_client_id = search.UNTRACKED_SNATCH
        self.assertFalse(self.history.mark_failed(self.episode, release="A.Release", anonymous=True))
        self.assertEqual(self._stored_status(), composite(SNATCHED))

    def test_anonymous_mark_failed_proceeds_after_a_restart(self):
        """No stamp and a snatched status: memory has no evidence either way, and the durable layer for
        the restart case is FailedProcessor's pending-row check, not this one."""
        self._set_status(composite(SNATCHED))
        self.assertFalse(hasattr(self.episode, "snatch_client_id"))
        self.assertTrue(self.history.mark_failed(self.episode, release="A.Release", anonymous=True))
        self.assertEqual(self._stored_status(), WANTED)

    def test_mark_failed_reports_failure_when_the_revert_declines(self):
        """mark_failed's answer is what its caller uses to decide whether to search. Reporting success on an
        episode it could not revert would have FailedQueueItem snatch over whatever now owns it."""
        self._set_status(composite(SNATCHED_BEST))
        real_revert = History._revert_episode_locked

        def change_the_database_first(history, episode_object, old_status=None, expect_status=None):
            self.main_db.action(
                "UPDATE tv_episodes SET status = ? WHERE showid = ? AND season = ? AND episode = ?",
                [composite(DOWNLOADED), self.show.indexerid, episode_object.season, episode_object.episode],
            )
            return real_revert(history, episode_object, old_status, expect_status)

        with mock.patch.object(History, "_revert_episode_locked", change_the_database_first):
            self.assertFalse(self.history.mark_failed(self.episode, release="A.Release", provider="NZBGeek", size=42))

        self.assertEqual(self._stored_status(), composite(DOWNLOADED), "the out-of-band status must stand")

    def test_mark_failed_holds_one_lock_from_the_status_write_to_the_revert(self):
        """Releasing the lock in between lets a snatch land in the gap, and mark_failed then reports success on
        an episode it no longer owns."""
        self._set_status(composite(SNATCHED_BEST))
        held = []

        real_revert = History._revert_episode_locked

        def record(history, episode_object, old_status=None, expect_status=None):
            held.append(episode_object.lock.locked())
            return real_revert(history, episode_object, old_status, expect_status)

        with mock.patch.object(History, "_revert_episode_locked", record):
            self.assertTrue(self.history.mark_failed(self.episode, release="A.Release", provider="NZBGeek", size=42))

        self.assertEqual(held, [True], "the revert must run under the same lock that wrote FAILED")

    def test_mark_failed_resumes_from_a_crashed_mark(self):
        """FAILED means a previous mark_failed wrote the status and died before blocking or reverting."""
        self._set_status(composite(FAILED, HDTV))
        self.assertTrue(self.history.mark_failed(self.episode, release="A.Release", provider="NZBGeek", size=42, old_status=WANTED))
        self.assertEqual(self._stored_status(), WANTED)
        self.assertTrue(self.history.has_failed("A.Release", 42, "NZBGeek"))

    def test_mark_failed_blocks_the_exact_release_it_was_given(self):
        self._set_status(composite(SNATCHED_BEST))
        self.history.mark_failed(self.episode, release="The.Bad.Release", provider="NZBGeek", size=840226000)

        self.assertTrue(self.history.has_failed("The.Bad.Release", 840226000, "NZBGeek"))
        self.assertFalse(self.history.has_failed("The.Bad.Release", 999, "NZBGeek"), "has_failed matches size exactly")

    def test_mark_failed_restores_a_failed_upgrade_to_its_downloaded_status(self):
        """The file is already on disk. Reverting to WANTED downloads it a second time."""
        downloaded = composite(DOWNLOADED, FULLHDTV)
        self._set_status(composite(SNATCHED_BEST))
        self.history.mark_failed(self.episode, release="Upgrade.Release", provider="NZBGeek", size=1, old_status=downloaded)
        self.assertEqual(self._stored_status(), downloaded)

    def test_log_failed_with_a_known_size_does_not_derive_minus_one(self):
        """Two snatches of one release name at different sizes make log_failed record size=-1, which no real
        search result can ever match. The blocked row would block nothing."""
        self._set_status(composite(SNATCHED))
        self._log_snatch("Ambiguous.Release", WANTED, "20260601120000", size=100)
        self._log_snatch("Ambiguous.Release", WANTED, "20260602120000", size=200)

        self.history.log_failed(self.episode, "Ambiguous.Release", "NZBGeek", size=200)
        rows = self.failed_db.select('SELECT size FROM failed WHERE "release" = ?', ["Ambiguous_Release"])
        self.assertEqual([row["size"] for row in rows], [200])

    def test_log_failed_without_a_size_keeps_the_legacy_derivation(self):
        self._set_status(composite(SNATCHED))
        self._log_snatch("Single.Release", WANTED, "20260601120000", size=500)
        self.history.log_failed(self.episode, "Single.Release", "NZBGeek")
        rows = self.failed_db.select('SELECT size FROM failed WHERE "release" = ?', ["Single_Release"])
        self.assertEqual([row["size"] for row in rows], [500])

    # ---- find_release ---------------------------------------------------------------------------------

    def test_find_release_returns_the_most_recent_snatch(self):
        self._log_snatch("Old.Release", WANTED, "20260601120000")
        self._log_snatch("New.Release", WANTED, "20260701120000")
        self.assertEqual(self.history.find_release(self.episode)[0], "New_Release")

    def test_find_release_does_not_delete_anything(self):
        self._log_snatch("Old.Release", WANTED, "20260601120000")
        self._log_snatch("New.Release", WANTED, "20260701120000")
        self.history.find_release(self.episode)
        self.assertEqual(len(self.failed_db.select("SELECT * FROM history")), 2)

    def test_find_release_with_no_history(self):
        self.assertEqual(self.history.find_release(self.episode), (None, None))


class FailedQueueItemTest(unittest.TestCase):
    """mark_failed's verdict has to reach the search.

    Marking an episode failed and then searching for it regardless is the whole race in miniature: the episode
    declined because it is already downloading again, and we would snatch straight over the top of it.
    """

    def setUp(self):
        settings.USE_FAILED_DOWNLOADS = True

    def _episode(self, season, episode):
        return mock.Mock(season=season, episode=episode, pretty_name=f"S{season:02d}E{episode:02d}")

    def _item(self, segment, **kwargs):
        item = search_queue.FailedQueueItem(mock.Mock(indexerid=1, name="Show"), segment, **kwargs)
        item._forget_pending_download = mock.Mock()
        return item

    def test_a_declined_episode_is_never_searched(self):
        declined, accepted = self._episode(1, 1), self._episode(1, 2)
        item = self._item([declined, accepted])

        with mock.patch.object(search_queue, "History") as history:
            history.return_value.mark_failed.side_effect = lambda ep, **kw: ep is accepted
            self.assertEqual(item._episodes_to_retry(), [accepted])

    def test_the_search_never_runs_when_every_episode_declined(self):
        item = self._item([self._episode(1, 1)])

        with mock.patch.object(search_queue, "History") as history:
            history.return_value.mark_failed.return_value = False
            with mock.patch.object(search_queue.search, "search_providers") as search_providers:
                with mock.patch.object(search_queue.search, "snatch_episode") as snatch:
                    item.run()

        search_providers.assert_not_called()
        snatch.assert_not_called()

    def test_the_search_only_sees_the_episodes_that_were_marked(self):
        declined, accepted = self._episode(1, 1), self._episode(1, 2)
        item = self._item([declined, accepted])

        with mock.patch.object(search_queue, "History") as history:
            history.return_value.mark_failed.side_effect = lambda ep, **kw: ep is accepted
            with mock.patch.object(search_queue.search, "search_providers", return_value=[]) as search_providers:
                item.run()

        self.assertEqual(search_providers.call_args[0][1], [accepted])

    def test_a_declined_episode_keeps_its_pending_row(self):
        # The row belongs to the newer download now. Deleting it would stop us ever reconciling that one.
        episode = self._episode(1, 1)
        item = self._item([episode], failed_releases={(1, 1): {"client_id": "job-a", "release": "R"}})

        with mock.patch.object(search_queue, "History") as history:
            history.return_value.mark_failed.return_value = False
            item._episodes_to_retry()

        item._forget_pending_download.assert_not_called()

    def test_an_accepted_episode_stops_being_tracked(self):
        episode = self._episode(1, 1)
        item = self._item([episode], failed_releases={(1, 1): {"client_id": "job-a", "release": "R"}})

        with mock.patch.object(search_queue, "History") as history:
            history.return_value.mark_failed.return_value = True
            item._episodes_to_retry()

        item._forget_pending_download.assert_called_once_with(episode, "job-a")

    def test_retry_still_works_with_failed_downloads_turned_off(self):
        """mark_failed declines everything when the feature is off. Retry must not become a no-op button."""
        settings.USE_FAILED_DOWNLOADS = False
        episode = self._episode(1, 1)
        item = self._item([episode], user_initiated=True)

        with mock.patch.object(search_queue, "History") as history:
            self.assertEqual(item._episodes_to_retry(), [episode])
        history.return_value.mark_failed.assert_not_called()


class EnqueueTest(unittest.TestCase):
    """The reconciler decides a row has failed, then acts on it later. In between, snatch_episode may have
    replaced that row with a live download."""

    def setUp(self):
        settings.FAILED_DOWNLOAD_MAX_ENQUEUES = 3
        self.updater = download_status.DownloadStatusUpdater()

    def _row(self, **overrides):
        row = {
            "showid": 1,
            "season": 2,
            "episode": 3,
            "client_id": "job-a",
            "release_name": "Some.Release",
            "provider": "NZBGeek",
            "size": 1,
            "old_status": WANTED,
            "enqueue_count": 0,
        }
        row.update(overrides)
        return row

    def _enqueue(self, row, rowcount=1):
        con = mock.Mock()
        con.action.return_value = mock.Mock(rowcount=rowcount)
        show = mock.Mock()
        show.get_episode.return_value = mock.Mock(season=2, episode=3)
        queue = mock.Mock()
        queue.action.is_episode_in_queue.return_value = False

        with mock.patch.object(download_status.Show, "find", return_value=show):
            with mock.patch.object(settings, "searchQueueScheduler", queue):
                self.updater._enqueue(con, [row], now=1000)
        return con, queue

    def test_a_superseded_row_is_not_enqueued(self):
        """The state='failing' update matched no row, so the job we are holding no longer exists."""
        _, queue = self._enqueue(self._row(), rowcount=0)
        queue.action.add_item.assert_not_called()

    def test_a_live_row_is_enqueued(self):
        _, queue = self._enqueue(self._row(), rowcount=1)
        self.assertTrue(queue.action.add_item.called)

    def test_giving_up_deletes_only_this_job(self):
        """An episode past its retry limit may have been snatched again since. Deleting by episode would drop
        the new download from tracking forever."""
        con, queue = self._enqueue(self._row(enqueue_count=3))
        queue.action.add_item.assert_not_called()

        query, args = con.action.call_args[0]
        self.assertIn("DELETE", query)
        self.assertIn("client_id = ?", query)
        self.assertIn("job-a", args)


class ClientCleanupTest(unittest.TestCase):
    """Deleting a failed job out of the download client is destructive, so every guard earns a test."""

    def setUp(self):
        settings.NZB_METHOD = "sabnzbd"
        settings.FAILED_DOWNLOAD_POLL_FREQUENCY = 5
        settings.FAILED_DOWNLOAD_CLIENT_CLEANUP = True
        self.updater = download_status.DownloadStatusUpdater()
        self.con = mock.Mock()

    def tearDown(self):
        settings.FAILED_DOWNLOAD_CLIENT_CLEANUP = False

    def _cleanup(self, rows, states, now=100000):
        self.con.select.return_value = rows
        from sickchill.oldbeard import sab

        with mock.patch.object(sab, "delete_history_item", return_value=True) as delete:
            self.updater._cleanup_client_jobs(self.con, states, now)
        return delete

    @staticmethod
    def _row(state="failing", snatch_time=0):
        return {"state": state, "snatch_time": snatch_time}

    def test_a_fully_failing_job_is_deleted_exactly_once(self):
        delete = self._cleanup([self._row(), self._row()], {"job-a": "Failed"})
        delete.assert_called_once_with("job-a")

    def test_a_sibling_row_that_is_not_yet_failing_defers_the_delete(self):
        """Season packs share one client id across rows; deleting after the first row would strand the
        rest in the absent path."""
        delete = self._cleanup([self._row(), self._row(state="completed")], {"job-a": "Failed"})
        delete.assert_not_called()

    def test_only_jobs_the_client_calls_failed_are_touched(self):
        """A vanished job is not in states at all, and a queued or completed one must never be deleted."""
        delete = self._cleanup([self._row()], {"job-a": "queued", "job-b": "Completed"})
        delete.assert_not_called()

    def test_a_freshly_snatched_job_is_left_for_the_next_cycle(self):
        """snatch_episode registers a pack's rows one transaction at a time; a job younger than a poll
        interval may not have all its rows yet."""
        now = 100000
        fresh = now - settings.FAILED_DOWNLOAD_POLL_FREQUENCY * 60 + 10
        delete = self._cleanup([self._row(snatch_time=fresh)], {"job-a": "Failed"})
        delete.assert_not_called()

    def test_a_failed_job_with_no_rows_left_is_still_cleaned(self):
        """The rows resolved or hit the give-up path mid-cycle; the client still holds a failed job
        nobody owns."""
        delete = self._cleanup([], {"job-a": "Failed"})
        delete.assert_called_once_with("job-a")

    def test_a_failing_delete_call_does_not_take_the_cycle_down(self):
        self.con.select.return_value = [self._row()]
        from sickchill.oldbeard import sab

        with mock.patch.object(sab, "delete_history_item", side_effect=OSError("boom")):
            self.updater._cleanup_client_jobs(self.con, {"job-a": "Failed"}, 100000)

    def test_reconcile_honors_the_cleanup_setting(self):
        row = {
            "showid": 1,
            "season": 2,
            "episode": 3,
            "client_id": "job-a",
            "client": "sabnzbd",
            "release_name": "Some.Release",
            "state": "pending",
        }
        from sickchill.oldbeard import sab

        with mock.patch.object(download_status.db, "DBConnection") as dbc:
            dbc.return_value.select.return_value = [row]
            with mock.patch.object(self.updater, "_resolve_safely", return_value=False):
                with mock.patch.object(self.updater, "_classify", return_value=False):
                    with mock.patch.object(self.updater, "_enqueue"):
                        with mock.patch.object(sab, "get_job_states", return_value={}):
                            with mock.patch.object(self.updater, "_cleanup_client_jobs") as cleanup:
                                settings.FAILED_DOWNLOAD_CLIENT_CLEANUP = False
                                self.updater._reconcile()
                                cleanup.assert_not_called()

                                settings.FAILED_DOWNLOAD_CLIENT_CLEANUP = True
                                self.updater._reconcile()
                                cleanup.assert_called_once()


class LifecycleTest(conftest.SickChillTestPostProcessorCase):
    def test_deleting_an_episode_removes_its_pending_row(self):
        main_db_con = db.DBConnection()
        episode = self.show.get_episode(1, 2)
        main_db_con.action(
            "INSERT INTO pending_downloads (showid, season, episode, client_id, client, release_name, provider, old_status, snatch_time, state_time) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            [self.show.indexerid, 1, 2, "job-a", "sabnzbd", "R", "P", WANTED, 0, 0],
        )
        try:
            episode.delete_episode()
        except Exception:
            pass

        rows = main_db_con.select("SELECT * FROM pending_downloads WHERE showid = ? AND season = ? AND episode = ?", [self.show.indexerid, 1, 2])
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
