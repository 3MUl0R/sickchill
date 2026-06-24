"""
Tests for the AI search result -> episode matcher (Part B of the anime-search improvement).

Covers:
  * _validate_matches: strict validation of AI mappings (RC5) - accept valid, reject
    out-of-range index, non-wanted episode, low confidence, duplicate index, bad shapes.
  * match_results: gating (AI unavailable / disabled / per-show / throttled) and that the
    search-match throttle context is used for reserve/commit.
  * ThrottleManager: the search and search_match contexts keep independent cooldowns (B4/G1).
  * BaseAIClient._store_cache: search_match is cached show-scoped, postprocess file-scoped (B4).
"""
from __future__ import annotations

import types
import unittest
from unittest import mock

from sickchill.oldbeard.ai import search_matcher
from sickchill.oldbeard.ai.base_client import BaseAIClient
from sickchill.oldbeard.ai.throttle import ThrottleManager
from tests import conftest


def _episode(season, episode, absolute=None, name=""):
    return types.SimpleNamespace(season=season, episode=episode, absolute_number=absolute, name=name)


def _show(name="Wagnaria!!", indexerid=145211):
    return types.SimpleNamespace(name=name, indexerid=indexerid)


class TestValidateMatches(unittest.TestCase):
    def setUp(self):
        self.episodes = [_episode(1, 8, absolute=8, name="Yamada and First Job"), _episode(1, 9, absolute=9)]
        self.items = [
            {"item": {}, "title": "[Fansub] Wagnaria!! - 08", "url": "u0", "size": 300 * 1024 * 1024},
            {"item": {}, "title": "[Fansub] Working!! - 09", "url": "u1", "size": 300 * 1024 * 1024},
        ]
        self.show = _show()

        prefs_patcher = mock.patch("sickchill.oldbeard.ai.search_matcher.get_preferences_manager")
        mock_get_prefs = prefs_patcher.start()
        self.addCleanup(prefs_patcher.stop)
        self.prefs = mock.MagicMock()
        self.prefs.get_confidence_threshold.return_value = 0.7
        mock_get_prefs.return_value = self.prefs

    def _validate(self, matches):
        return search_matcher._validate_matches({"matches": matches}, self.items, self.episodes, self.show)

    def test_accepts_valid_match_and_carries_record(self):
        out = self._validate([{"index": 0, "season": 1, "episode": 8, "confidence": 0.95, "reasoning": "abs 8"}])
        self.assertEqual(len(out), 1)
        self.assertEqual((out[0]["season"], out[0]["episode"]), (1, 8))
        self.assertEqual(out[0]["url"], "u0")  # original record fields carried through
        self.assertEqual(out[0]["confidence"], 0.95)

    def test_rejects_out_of_range_index(self):
        self.assertEqual(self._validate([{"index": 5, "season": 1, "episode": 8, "confidence": 0.95}]), [])

    def test_rejects_non_wanted_episode(self):
        # S2E2 is not in the wanted set
        self.assertEqual(self._validate([{"index": 0, "season": 2, "episode": 2, "confidence": 0.95}]), [])

    def test_rejects_low_confidence(self):
        self.assertEqual(self._validate([{"index": 0, "season": 1, "episode": 8, "confidence": 0.5}]), [])

    def test_rejects_duplicate_index(self):
        out = self._validate(
            [
                {"index": 0, "season": 1, "episode": 8, "confidence": 0.95},
                {"index": 0, "season": 1, "episode": 9, "confidence": 0.95},
            ]
        )
        self.assertEqual(len(out), 1)

    def test_rejects_non_integer_season_episode(self):
        self.assertEqual(self._validate([{"index": 0, "season": "1", "episode": 8, "confidence": 0.95}]), [])

    def test_rejects_boolean_season_or_index(self):
        # bool is an int subclass in Python; must not slip through as season/episode/index.
        self.assertEqual(self._validate([{"index": 0, "season": True, "episode": 8, "confidence": 0.95}]), [])
        self.assertEqual(self._validate([{"index": True, "season": 1, "episode": 8, "confidence": 0.95}]), [])

    def test_rejects_missing_confidence(self):
        self.assertEqual(self._validate([{"index": 0, "season": 1, "episode": 8}]), [])

    def test_rejects_boolean_confidence(self):
        self.assertEqual(self._validate([{"index": 0, "season": 1, "episode": 8, "confidence": True}]), [])

    def test_rejects_out_of_range_confidence(self):
        self.assertEqual(self._validate([{"index": 0, "season": 1, "episode": 8, "confidence": 1.5}]), [])
        self.assertEqual(self._validate([{"index": 0, "season": 1, "episode": 8, "confidence": -0.1}]), [])

    def test_rejects_missing_confidence_even_if_threshold_zero(self):
        self.prefs.get_confidence_threshold.return_value = 0.0
        self.assertEqual(self._validate([{"index": 0, "season": 1, "episode": 8}]), [])
        # but an explicit valid confidence still passes at threshold 0.0
        out = self._validate([{"index": 0, "season": 1, "episode": 8, "confidence": 0.0}])
        self.assertEqual(len(out), 1)

    def test_handles_bad_response_shapes(self):
        self.assertEqual(search_matcher._validate_matches(None, self.items, self.episodes, self.show), [])
        self.assertEqual(search_matcher._validate_matches({"matches": "nope"}, self.items, self.episodes, self.show), [])
        self.assertEqual(search_matcher._validate_matches({}, self.items, self.episodes, self.show), [])


class TestMatchResults(unittest.TestCase):
    def setUp(self):
        self.show = _show()
        self.episodes = [_episode(1, 8, absolute=8)]
        self.items = [{"item": {}, "title": "[Fansub] Wagnaria!! - 08", "url": "u0", "size": 300 * 1024 * 1024}]

        self.available_patcher = mock.patch("sickchill.oldbeard.ai.search_matcher.is_ai_available", return_value=True)
        self.available_patcher.start()
        self.addCleanup(self.available_patcher.stop)

        self.settings_patcher = mock.patch("sickchill.oldbeard.ai.search_matcher.settings")
        self.mock_settings = self.settings_patcher.start()
        self.addCleanup(self.settings_patcher.stop)
        self.mock_settings.AI_SEARCH_ENABLED = True

        self.prefs_patcher = mock.patch("sickchill.oldbeard.ai.search_matcher.get_preferences_manager")
        mock_get_prefs = self.prefs_patcher.start()
        self.addCleanup(self.prefs_patcher.stop)
        self.prefs = mock.MagicMock()
        self.prefs.should_use_ai_search.return_value = True
        self.prefs.get_confidence_threshold.return_value = 0.7
        mock_get_prefs.return_value = self.prefs

        self.throttle_patcher = mock.patch("sickchill.oldbeard.ai.search_matcher.get_throttle")
        mock_get_throttle = self.throttle_patcher.start()
        self.addCleanup(self.throttle_patcher.stop)
        self.throttle = mock.MagicMock()
        self.throttle.reserve_search_attempt.return_value = True
        mock_get_throttle.return_value = self.throttle

        self.client_patcher = mock.patch("sickchill.oldbeard.ai.search_matcher.get_client")
        mock_get_client = self.client_patcher.start()
        self.addCleanup(self.client_patcher.stop)
        self.client = mock.MagicMock()
        self.client.analyze.return_value = (
            {"matches": [{"index": 0, "season": 1, "episode": 8, "confidence": 0.95, "reasoning": "abs 8"}]},
            False,
        )
        mock_get_client.return_value = self.client

        self.aliases_patcher = mock.patch("sickchill.oldbeard.ai.search_matcher._get_aliases", return_value=[])
        self.aliases_patcher.start()
        self.addCleanup(self.aliases_patcher.stop)

        self.template_patcher = mock.patch(
            "sickchill.oldbeard.ai.search_matcher._load_prompt_template",
            return_value="S={show_name} A={aliases} W={wanted_json} R={releases_json}",
        )
        self.template_patcher.start()
        self.addCleanup(self.template_patcher.stop)

        self.feedback_patcher = mock.patch("sickchill.oldbeard.ai.search_matcher.get_feedback_manager")
        self.feedback_patcher.start()
        self.addCleanup(self.feedback_patcher.stop)

    def test_returns_validated_match_and_uses_search_match_context(self):
        out = search_matcher.match_results(self.show, self.episodes, self.items)
        self.assertEqual(len(out), 1)
        self.assertEqual((out[0]["season"], out[0]["episode"]), (1, 8))

        # reserve + commit must use the dedicated search-match context (B4/G1).
        _, reserve_kwargs = self.throttle.reserve_search_attempt.call_args
        self.assertEqual(reserve_kwargs.get("context"), ThrottleManager.CONTEXT_SEARCH_MATCH)
        _, commit_kwargs = self.throttle.commit_search_attempt.call_args
        self.assertEqual(commit_kwargs.get("context"), ThrottleManager.CONTEXT_SEARCH_MATCH)
        # cost_context passed to the client must be search_match (drives show-scoped cache).
        _, analyze_kwargs = self.client.analyze.call_args
        self.assertEqual(analyze_kwargs.get("cost_context"), "search_match")

    def test_cache_hit_does_not_bill_budget(self):
        self.client.analyze.return_value = (
            {"matches": [{"index": 0, "season": 1, "episode": 8, "confidence": 0.95}]},
            True,  # was_cached
        )
        search_matcher.match_results(self.show, self.episodes, self.items)
        _, commit_kwargs = self.throttle.commit_search_attempt.call_args
        self.assertFalse(commit_kwargs.get("count_budget"))

    def test_returns_empty_when_throttled(self):
        self.throttle.reserve_search_attempt.return_value = False
        self.assertEqual(search_matcher.match_results(self.show, self.episodes, self.items), [])
        self.client.analyze.assert_not_called()

    def test_returns_empty_when_ai_unavailable(self):
        with mock.patch("sickchill.oldbeard.ai.search_matcher.is_ai_available", return_value=False):
            self.assertEqual(search_matcher.match_results(self.show, self.episodes, self.items), [])

    def test_returns_empty_when_search_disabled(self):
        self.mock_settings.AI_SEARCH_ENABLED = False
        self.assertEqual(search_matcher.match_results(self.show, self.episodes, self.items), [])

    def test_returns_empty_when_per_show_disabled(self):
        self.prefs.should_use_ai_search.return_value = False
        self.assertEqual(search_matcher.match_results(self.show, self.episodes, self.items), [])

    def test_returns_empty_with_no_inputs(self):
        self.assertEqual(search_matcher.match_results(self.show, [], self.items), [])
        self.assertEqual(search_matcher.match_results(self.show, self.episodes, []), [])


class TestThrottleContextIndependence(conftest.SickChillTestDBCase):
    def test_search_and_search_match_have_independent_cooldowns(self):
        throttle = ThrottleManager()
        throttle.record_attempt(ThrottleManager.CONTEXT_SEARCH, ThrottleManager.SCOPE_SHOW, "999")

        self.assertIsNotNone(throttle._get_last_attempt(ThrottleManager.CONTEXT_SEARCH, ThrottleManager.SCOPE_SHOW, "999"))
        # The search-match context must NOT see the search context's attempt.
        self.assertIsNone(throttle._get_last_attempt(ThrottleManager.CONTEXT_SEARCH_MATCH, ThrottleManager.SCOPE_SHOW, "999"))

    def test_record_search_success_routes_to_context(self):
        throttle = ThrottleManager()
        show = types.SimpleNamespace(indexerid=888)
        throttle.record_attempt(ThrottleManager.CONTEXT_SEARCH_MATCH, ThrottleManager.SCOPE_SHOW, "888")
        # Should not raise and should target the search_match row.
        throttle.record_search_success(show, context=ThrottleManager.CONTEXT_SEARCH_MATCH)


class TestCacheScopeGate(unittest.TestCase):
    def _scope_for(self, cost_context):
        with mock.patch("sickchill.oldbeard.ai.get_throttle") as mock_get_throttle:
            throttle = mock.MagicMock()
            mock_get_throttle.return_value = throttle
            BaseAIClient._store_cache(mock.MagicMock(), "hash", {"x": 1}, cost_context, "scope-key")
            _, kwargs = throttle.cache_response.call_args
            return kwargs["scope"]

    def test_search_match_is_show_scoped(self):
        self.assertEqual(self._scope_for("search_match"), "show")

    def test_search_is_show_scoped(self):
        self.assertEqual(self._scope_for("search"), "show")

    def test_postprocess_is_file_scoped(self):
        self.assertEqual(self._scope_for("postprocess"), "file")


if __name__ == "__main__":
    unittest.main()
