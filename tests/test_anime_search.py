"""
Tests for the additive anime search behavior (Part A of the anime-search improvement).

Covers:
  * GenericProvider.get_episode_search_strings emits an additive SxxEyy variant for anime
    (on top of the absolute-number strings), and leaves non-anime strings unchanged.
  * NewznabProvider._build_anime_variants builds the expected per-variant request dicts
    (structured season/ep, structured absolute, free-text), defines a safe Season-mode
    query (never tvdbid-only), gates structured queries off under torznab, and caps the
    number of free-text variants.
  * NewznabProvider._dedupe_results collapses duplicate pooled results.
"""
import types
import unittest

from sickchill.oldbeard import common
from sickchill.oldbeard.providers.newznab import NewznabProvider
from sickchill.tv import TVEpisode, TVShow
from tests import conftest


def _make_provider(use_tvdbid=True, torznab=False):
    provider = NewznabProvider("Test", "https://example.com/api", key="testkey")
    provider.use_tv_search = use_tvdbid
    provider.cap_tv_search = "tvdbid,season,ep" if use_tvdbid else ""
    provider.torznab = torznab
    return provider


class AnimeSearchStringTest(conftest.SickChillTestDBCase):
    """get_episode_search_strings: anime gets additive SxxEyy; non-anime unchanged."""

    def _episode_strings(self, anime, season, episode_number, absolute_number=None):
        show = TVShow(1, 74796 if anime else 121361)
        show.name = "Bleach" if anime else "Game of Thrones"
        show.anime = 1 if anime else 0
        show.quality = common.ANY | common.Quality.UNKNOWN
        show.save_to_db()

        episode = TVEpisode(show, season, episode_number)
        episode.status = common.WANTED
        episode.scene_season = season
        episode.scene_episode = episode_number
        if absolute_number is not None:
            episode.absolute_number = absolute_number
            episode.scene_absolute_number = absolute_number
        episode.save_to_db()

        provider = _make_provider()
        result = provider.get_episode_search_strings(episode)
        return set(result[0]["Episode"])

    def test_anime_includes_additive_season_episode_variant(self):
        strings = self._episode_strings(anime=True, season=11, episode_number=1, absolute_number=6)
        # The new additive normal season/episode form.
        self.assertIn("Bleach S11E01", strings)
        # The historical absolute-number forms are still present (additive, not replaced).
        self.assertIn("Bleach 006", strings)
        self.assertIn("Bleach 06", strings)

    def test_non_anime_unchanged(self):
        strings = self._episode_strings(anime=False, season=5, episode_number=10)
        # Exactly the normal season/episode string, with no absolute-number additions.
        self.assertEqual(strings, {"Game of Thrones S05E10"})


class AnimeNewznabVariantTest(conftest.SickChillTestDBCase):
    """NewznabProvider variant construction + dedupe (no network)."""

    def _provider_with_episode(self, use_tvdbid=True, torznab=False, absolute_number=206):
        provider = _make_provider(use_tvdbid=use_tvdbid, torznab=torznab)
        provider.show = types.SimpleNamespace(indexerid=74796, is_anime=True, air_by_date=False, sports=False)
        provider.current_episode_object = types.SimpleNamespace(scene_season=11, scene_episode=1, absolute_number=absolute_number)
        return provider

    def test_episode_mode_builds_structured_and_freetext_variants(self):
        provider = self._provider_with_episode()
        variants = provider._build_anime_variants("Episode", {"Bleach S11E01", "Bleach 206", "Bleach 06"})
        labels = [label for label, _params in variants]

        self.assertIn("structured-season-ep", labels)
        self.assertIn("structured-absolute", labels)
        self.assertEqual(labels.count("free-text"), 3)

        sxe = next(params for label, params in variants if label == "structured-season-ep")
        self.assertEqual(sxe["tvdbid"], 74796)
        self.assertEqual(sxe["season"], 11)
        self.assertEqual(sxe["ep"], 1)
        self.assertNotIn("q", sxe)

        absolute = next(params for label, params in variants if label == "structured-absolute")
        self.assertEqual(absolute["ep"], 206)
        self.assertNotIn("season", absolute)
        self.assertNotIn("q", absolute)

        for label, params in variants:
            if label == "free-text":
                self.assertIn("q", params)
                self.assertNotIn("tvdbid", params)

    def test_season_mode_is_never_tvdbid_only(self):
        provider = self._provider_with_episode()
        variants = provider._build_anime_variants("Season", {"Bleach Season"})
        labels = [label for label, _params in variants]

        self.assertIn("structured-season", labels)
        self.assertNotIn("structured-season-ep", labels)
        self.assertNotIn("structured-absolute", labels)

        season_params = next(params for label, params in variants if label == "structured-season")
        self.assertEqual(season_params["season"], 11)
        self.assertNotIn("ep", season_params)
        self.assertNotIn("q", season_params)
        # Guardrail [G2/RC3]: a structured anime season query must always be scoped by
        # season, never a bare tvdbid-only query that would match the whole show.
        self.assertIn("season", season_params)

    def test_torznab_skips_structured_variants(self):
        provider = self._provider_with_episode(torznab=True)
        variants = provider._build_anime_variants("Season", {"Bleach Season", "Bleach"})
        self.assertTrue(variants)
        for label, params in variants:
            # Under torznab, tvdbid/season/ep are ignored by the server, so we only issue
            # free-text queries and never a degenerate tvdbid-only request.
            self.assertEqual(label, "free-text")
            self.assertIn("q", params)
            self.assertNotIn("tvdbid", params)

    def test_no_structured_variants_without_tvdbid_caps(self):
        provider = self._provider_with_episode(use_tvdbid=False)
        variants = provider._build_anime_variants("Episode", {"Bleach 206"})
        for label, params in variants:
            self.assertEqual(label, "free-text")
            self.assertNotIn("tvdbid", params)

    def test_freetext_variants_are_capped(self):
        provider = self._provider_with_episode()
        many = {"Bleach {0}".format(i) for i in range(20)}
        variants = provider._build_anime_variants("Episode", many)
        free_text = [params for label, params in variants if label == "free-text"]
        self.assertEqual(len(free_text), provider.ANIME_MAX_FREETEXT_VARIANTS)

    def test_dedupe_by_link_then_title_size(self):
        items = [
            {"title": "Release A", "link": "http://x/1", "size": 100, "hash": ""},
            {"title": "Release A (mirror)", "link": "http://X/1", "size": 100, "hash": ""},  # same link, diff case
            {"title": "Release B", "link": "http://x/2", "size": 200, "hash": "abc"},
            {"title": "Release C", "link": "", "size": 300, "hash": ""},  # no link -> title+size key
            {"title": "Release C", "link": "", "size": 300, "hash": ""},  # duplicate of previous
            {"title": "Release C", "link": "", "size": 301, "hash": ""},  # different size -> kept
        ]
        deduped = NewznabProvider._dedupe_results(items)
        links = [item["link"] for item in deduped]
        self.assertEqual(len(deduped), 4)
        self.assertEqual(links.count("http://x/1"), 1)


def _stub_episode(season, episode, absolute_number=0, scene_season=None, is_anime=True):
    return types.SimpleNamespace(
        season=season,
        episode=episode,
        absolute_number=absolute_number,
        scene_season=scene_season,
        show=types.SimpleNamespace(is_anime=is_anime),
    )


def _stub_parse_result(season_number=None, episode_numbers=None, ab_episode_numbers=None):
    return types.SimpleNamespace(
        season_number=season_number,
        episode_numbers=episode_numbers or [],
        ab_episode_numbers=ab_episode_numbers or [],
    )


class ExplicitAnimeSeasonDetectorTests(unittest.TestCase):
    """GenericProvider._explicit_anime_season confidence contract (Fix A2 detector)."""

    def setUp(self):
        from sickchill.providers.GenericProvider import GenericProvider

        self.detect = GenericProvider._explicit_anime_season

    def test_confident_season_tokens(self):
        self.assertEqual(self.detect("[Moozzi2].K-ON.S2-01.[BD.1920x1080.x.264.FLACx3]"), 2)
        self.assertEqual(self.detect("[Moozzi2] K-ON!! S2 - 01 (BD 1920x1080 x.264 FLACx3)"), 2)
        self.assertEqual(self.detect("Show Name Season 3 - 04"), 3)
        self.assertEqual(self.detect("Show.S03.1080p.BluRay"), 3)

    def test_not_confident(self):
        # SxxExx is a full episode code, not a standalone season token.
        self.assertIsNone(self.detect("Mushoku.Tensei.Jobless.Reincarnation.S02E03.1080p.WEBRip"))
        self.assertIsNone(self.detect("K-ON! - S02E15 - Marathon Tournament!"))
        # Bare absolute-numbered anime, no season token.
        self.assertIsNone(self.detect("[Judas] Bleach - 206 [1080p]"))
        # Roman numerals are left to the parser's scene-exception handling, not A2.
        self.assertIsNone(self.detect("[Moozzi2].Mushoku.Tensei.II.Isekai.Ittara.Honki.Dasu-15"))
        # A release-group named like a season must not be mistaken for one (bracketed or not):
        # the token must be a standalone S<n>, with an alnum boundary on both sides.
        self.assertIsNone(self.detect("[S2Productions] Some Show - 05 [1080p]"))
        self.assertIsNone(self.detect("Show.Name.S2Productions.05.1080p"))
        self.assertIsNone(self.detect("Show.Name.S2x264-05"))
        self.assertIsNone(self.detect(""))
        self.assertIsNone(self.detect(None))


class CrossSeasonMismatchTests(unittest.TestCase):
    """GenericProvider._is_cross_season_mismatch (Fix A2 guard): reject-only cross-season detection."""

    def setUp(self):
        from sickchill.providers.GenericProvider import GenericProvider

        self.mismatch = GenericProvider._is_cross_season_mismatch

    def test_rejects_wrong_season_token(self):
        # "K-ON.S2-01" resolves to S1E01 (absolute collision) but its name says S2 -> reject for S1.
        episodes = [_stub_episode(1, 1, absolute_number=1, scene_season=1)]
        parse_result = _stub_parse_result(season_number=1, episode_numbers=[1], ab_episode_numbers=[1])
        self.assertTrue(self.mismatch("[Moozzi2].K-ON.S2-01.[BD.1920x1080]", parse_result, episodes))

    def test_allows_matching_season_token(self):
        episodes = [_stub_episode(2, 1, absolute_number=15, scene_season=2)]
        parse_result = _stub_parse_result(season_number=2, episode_numbers=[1], ab_episode_numbers=[15])
        self.assertFalse(self.mismatch("[Moozzi2].K-ON.S2-01.[BD.1920x1080]", parse_result, episodes))

    def test_no_token_never_rejects(self):
        episodes = [_stub_episode(1, 1, absolute_number=1, scene_season=1)]
        parse_result = _stub_parse_result(season_number=1, episode_numbers=[1], ab_episode_numbers=[1])
        self.assertFalse(self.mismatch("[Judas] Bleach - 1 [1080p]", parse_result, episodes))

    def test_no_matched_episode_never_rejects(self):
        # Token present but the result matched none of the wanted episodes -> not our concern.
        episodes = [_stub_episode(5, 9, absolute_number=99, scene_season=5)]
        parse_result = _stub_parse_result(season_number=1, episode_numbers=[1], ab_episode_numbers=[1])
        self.assertFalse(self.mismatch("[Moozzi2].K-ON.S2-01.[BD.1920x1080]", parse_result, episodes))

    def test_scene_season_counts_as_allowed(self):
        # Matched episode's scene_season equals the token even though its TVDB season differs.
        episodes = [_stub_episode(1, 5, absolute_number=5, scene_season=2)]
        parse_result = _stub_parse_result(season_number=1, episode_numbers=[5], ab_episode_numbers=[5])
        self.assertFalse(self.mismatch("Show.S2-05.1080p", parse_result, episodes))


class CacheCrossSeasonGuardTests(conftest.SickChillTestPostProcessorCase):
    """The cache-read guard must drop a cached anime result whose release name confidently names a
    different season than the row it's filed under (covers RSS- and search-populated cache that the
    fresh-search guard cannot see)."""

    _COLUMNS = ("provider", "name", "season", "episodes", "indexerid", "url", "time", "quality", "release_group", "version", "seeders", "leechers", "size")

    def setUp(self):
        super().setUp()
        self.show.anime = 1
        self.show.quality = common.Quality.combineQualities([common.Quality.FULLHDBLURAY], [])
        self.show.save_to_db()
        self.episode = self.show.get_episode(1, 1)
        self.episode.status = common.WANTED
        self.episode.save_to_db()
        self.provider = _make_provider()

    def _cache_row(self, name):
        cache_db = self.provider.cache.get_db()
        values = {
            "provider": self.provider.cache.provider_id,
            "name": name,
            "season": 1,
            "episodes": "|1|",
            "indexerid": 1,
            "url": "http://example.com/" + name,
            "time": 1,
            "quality": common.Quality.FULLHDBLURAY,
            "release_group": "",
            "version": -1,
            "seeders": -1,
            "leechers": -1,
            "size": -1,
        }
        cache_db.action(
            "INSERT INTO results ({0}) VALUES ({1})".format(", ".join(self._COLUMNS), ", ".join("?" for _ in self._COLUMNS)),
            [values[column] for column in self._COLUMNS],
        )

    def test_cross_season_cached_result_is_filtered(self):
        # Filed under S1E1 but the name says S2 -> must not be returned for wanted S1E1.
        self._cache_row("[Moozzi2].K-ON.S2-01.[BD.1920x1080.x.264.FLACx3]")
        needed = self.provider.cache.find_needed_episodes(self.episode)
        self.assertEqual(needed.get(self.episode, []), [])

    def test_matching_cached_result_is_returned(self):
        # Control: no conflicting season token -> the cached result is returned as usual.
        self._cache_row("[Judas] show name - 1 [1080p]")
        needed = self.provider.cache.find_needed_episodes(self.episode)
        self.assertTrue(needed.get(self.episode))


if __name__ == "__main__":
    unittest.main()
