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


if __name__ == "__main__":
    unittest.main()
