"""
Tests for processTV.already_processed scoping.

Regression: a file was treated as "already processed" whenever ANY episode (any show, any status) had a
release_name matching the file basename. A stray/cross-season release_name then caused the real download to
be skipped and its folder reaped, leaving the target episode stuck Snatched. already_processed must only
short-circuit when the episode the file actually maps to is already Downloaded/Archived.
"""
import types
from unittest import mock

from sickchill.oldbeard import common, db, processTV
from sickchill.tv import TVEpisode, TVShow
from tests import conftest

SHOWID = 352408


def _parse(indexerid=SHOWID, season=None, episodes=None, absolute=None):
    """Minimal stand-in for guessit_findit's ParseResult."""
    return types.SimpleNamespace(
        show=types.SimpleNamespace(indexerid=indexerid),
        season_number=season,
        episode_numbers=list(episodes or []),
        ab_episode_numbers=list(absolute or []),
        is_anime=bool(absolute),
    )


class AlreadyProcessedScopeTests(conftest.SickChillTestDBCase):
    def setUp(self):
        super().setUp()
        self.show = TVShow(1, SHOWID)
        self.show.name = "That Time I Got Reincarnated as a Slime"
        self.show.anime = 1
        self.show.quality = common.ANY | common.Quality.UNKNOWN
        self.show.save_to_db()

        self.eps = {}
        # S1 downloaded (abs 1-3), S2 snatched (abs 25-27)
        for season, status in ((1, common.DOWNLOADED), (2, common.SNATCHED)):
            for epnum in (1, 2, 3):
                ep = TVEpisode(self.show, season, epnum)
                ep.absolute_number = (season - 1) * 24 + epnum
                ep.status = common.Quality.compositeStatus(status, common.Quality.FULLHDBLURAY)
                ep.release_name = ""
                ep.save_to_db()
                self.eps[(season, epnum)] = ep
        self.result = processTV.ProcessResult()

    def _set(self, season, epnum, status=None, release_name=None):
        ep = self.eps[(season, epnum)]
        if status is not None:
            ep.status = common.Quality.compositeStatus(status, common.Quality.FULLHDBLURAY)
        if release_name is not None:
            ep.release_name = release_name
        ep.save_to_db()

    def _ap(self, video_file, parse):
        with mock.patch("sickchill.oldbeard.processTV.postProcessor.guessit_findit", return_value=parse):
            return processTV.already_processed("/downloads/show", video_file, False, self.result)

    # --- the regression ---
    def test_release_name_on_other_episode_does_not_skip(self):
        # S01E02 (downloaded) wrongly carries the S2-02 file's release name; the file maps to S02E02 (snatched).
        self._set(1, 2, release_name="Show.S2-02")
        assert self._ap("Show.S2-02.mkv", _parse(season=2, episodes=[2])) is False

    # --- legitimate copy-mode guard preserved ---
    def test_same_episode_downloaded_under_release_name_skips(self):
        self._set(2, 2, status=common.DOWNLOADED, release_name="Show.S2-02")
        assert self._ap("Show.S2-02.mkv", _parse(season=2, episodes=[2])) is True

    # --- status gate: mapped episode holds the name but isn't downloaded ---
    def test_snatched_mapped_episode_does_not_skip(self):
        self._set(2, 2, release_name="Show.S2-02")  # still Snatched
        assert self._ap("Show.S2-02.mkv", _parse(season=2, episodes=[2])) is False

    # --- no scope -> never skip ---
    def test_unparseable_does_not_skip(self):
        assert self._ap("whatever.mkv", None) is False

    def test_show_without_episode_or_absolute_does_not_skip(self):
        # show resolved but no season/episode and no absolute -> cannot scope -> do not skip
        self._set(1, 2, release_name="Show.S2-02")
        assert self._ap("Show.S2-02.mkv", _parse(season=None, episodes=[])) is False

    # --- force bypasses everything (and does not even parse) ---
    def test_force_returns_false_without_parsing(self):
        with mock.patch("sickchill.oldbeard.processTV.postProcessor.guessit_findit") as guess:
            assert processTV.already_processed("/x", "f.mkv", True, self.result) is False
            guess.assert_not_called()

    # --- anime absolute scope ---
    def test_absolute_scope_skips_when_mapped_abs_downloaded(self):
        # abs 26 == S02E02; mark it downloaded under the release name, parse the file as abs 26
        self._set(2, 2, status=common.DOWNLOADED, release_name="Show.abs26")
        assert self._ap("Show.abs26.mkv", _parse(season=None, absolute=[26])) is True

    def test_absolute_scope_release_name_on_other_abs_does_not_skip(self):
        # downloaded S01E02 (abs 2) holds the name; file maps to abs 26 (snatched S02E02) -> do not skip
        self._set(1, 2, release_name="Show.abs26")
        assert self._ap("Show.abs26.mkv", _parse(season=None, absolute=[26])) is False

    # --- history-backed guard, scoped to the mapped episode ---
    def test_history_match_for_downloaded_mapped_episode_skips(self):
        self._set(2, 3, status=common.DOWNLOADED)  # S02E03 downloaded
        db.DBConnection().action(
            "INSERT INTO history (action, date, showid, season, episode, quality, resource, provider) VALUES (?,?,?,?,?,?,?,?)",
            [common.Quality.compositeStatus(common.DOWNLOADED, common.Quality.FULLHDBLURAY), 20260627000000, SHOWID, 2, 3,
             common.Quality.FULLHDBLURAY, "Show.S2-03.mkv", "test"],
        )
        assert self._ap("Show.S2-03.mkv", _parse(season=2, episodes=[3])) is True

    def test_history_match_for_other_episode_does_not_skip(self):
        # history row exists for S02E03 (downloaded), but the file maps to S02E02 (snatched) -> do not skip
        self._set(2, 3, status=common.DOWNLOADED)
        db.DBConnection().action(
            "INSERT INTO history (action, date, showid, season, episode, quality, resource, provider) VALUES (?,?,?,?,?,?,?,?)",
            [common.Quality.compositeStatus(common.DOWNLOADED, common.Quality.FULLHDBLURAY), 20260627000000, SHOWID, 2, 3,
             common.Quality.FULLHDBLURAY, "Show.S2-03.mkv", "test"],
        )
        assert self._ap("Show.S2-03.mkv", _parse(season=2, episodes=[2])) is False

    # --- multi-episode: require ALL mapped episodes, not just one ---
    def test_multi_episode_partial_does_not_skip(self):
        # File maps to S02E02+E03. Only E02 is downloaded under the release name; E03 still snatched.
        self._set(2, 2, status=common.DOWNLOADED, release_name="Show.S2-02-03")
        # E03 left Snatched
        assert self._ap("Show.S2-02-03.mkv", _parse(season=2, episodes=[2, 3])) is False

    def test_multi_episode_all_downloaded_skips(self):
        # Both mapped episodes downloaded under the release name -> already processed.
        self._set(2, 2, status=common.DOWNLOADED, release_name="Show.S2-02-03")
        self._set(2, 3, status=common.DOWNLOADED, release_name="Show.S2-02-03")
        assert self._ap("Show.S2-02-03.mkv", _parse(season=2, episodes=[2, 3])) is True

    # --- empty release_name must never match (defensive guard) ---
    def test_downloaded_episode_with_empty_release_name_does_not_skip(self):
        # mapped episode is downloaded but has no recorded release name and no history -> cannot confirm
        self._set(2, 2, status=common.DOWNLOADED, release_name="")
        assert self._ap("Show.S2-02.mkv", _parse(season=2, episodes=[2])) is False
