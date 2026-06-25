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

    def test_accepts_match_without_reasoning_field(self):
        # Default (reasoning off) responses omit the key entirely; it must still validate.
        out = self._validate([{"index": 0, "season": 1, "episode": 8, "confidence": 0.95}])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["reasoning"], "No reasoning provided")

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
        # Explicit: a bare MagicMock attribute is truthy, which would silently flip reasoning on.
        self.mock_settings.AI_SEARCH_MATCH_INCLUDE_REASONING = False
        # Effort drives the batch cap (low/medium -> 30, high/xhigh/max -> 20).
        self.mock_settings.AI_CLI_EFFORT = "low"

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
            return_value="S={show_name} A={aliases} W={wanted_json} R={releases_json}{reasoning_field}",
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

    def _sent_prompt(self):
        search_matcher.match_results(self.show, self.episodes, self.items)
        _, analyze_kwargs = self.client.analyze.call_args
        return analyze_kwargs["prompt"]

    def test_reasoning_excluded_from_prompt_by_default(self):
        # Default (flag off): the reasoning field must not be requested.
        self.assertNotIn('"reasoning"', self._sent_prompt())

    def test_reasoning_included_in_prompt_when_enabled(self):
        self.mock_settings.AI_SEARCH_MATCH_INCLUDE_REASONING = True
        self.assertIn('"reasoning": "<brief explanation>"', self._sent_prompt())

    def _many_items(self, n):
        return [
            {"item": {}, "title": f"[Fansub] Wagnaria!! - {i:02d}", "url": f"u{i}", "size": 300 * 1024 * 1024}
            for i in range(n)
        ]

    def test_caps_releases_low_effort_thirty(self):
        # 35 distinct-URL releases survive de-dup; low effort -> only 30 sent.
        self.mock_settings.AI_CLI_EFFORT = "low"
        self.items = self._many_items(35)
        # one "size_mb" entry per release in the serialized prompt payload
        self.assertEqual(self._sent_prompt().count('"size_mb"'), 30)

    def test_caps_releases_high_effort_twenty(self):
        # High effort costs far more thinking per release, so the cap tightens to 20.
        self.mock_settings.AI_CLI_EFFORT = "high"
        self.items = self._many_items(35)
        self.assertEqual(self._sent_prompt().count('"size_mb"'), 20)

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

    def test_scope_key_includes_provider_mode_season(self):
        # Two seasons in the episode set -> season suffix lists both, sorted.
        episodes = [_episode(1, 8, absolute=8), _episode(2, 3, absolute=27)]
        items = [
            {"item": {}, "title": "[Fansub] Working!! - 08", "url": "u0", "size": 1},
            {"item": {}, "title": "[Fansub] Working!! S2 - 03", "url": "u1", "size": 1},
        ]
        self.client.analyze.return_value = ({"matches": []}, False)
        search_matcher.match_results(self.show, episodes, items, provider_id="nzbgeek", search_mode="season")
        _, reserve_kwargs = self.throttle.reserve_search_attempt.call_args
        self.assertEqual(reserve_kwargs.get("scope_key"), "145211:nzbgeek:season:s1-2")

    def test_manual_search_bypasses_cooldown(self):
        self.client.analyze.return_value = ({"matches": []}, False)
        search_matcher.match_results(self.show, self.episodes, self.items, manual_search=True)
        _, reserve_kwargs = self.throttle.reserve_search_attempt.call_args
        self.assertEqual(reserve_kwargs.get("cooldown_seconds"), 0)

    def test_auto_search_uses_short_cooldown(self):
        self.client.analyze.return_value = ({"matches": []}, False)
        search_matcher.match_results(self.show, self.episodes, self.items)
        _, reserve_kwargs = self.throttle.reserve_search_attempt.call_args
        self.assertEqual(reserve_kwargs.get("cooldown_seconds"), search_matcher.SEARCH_MATCH_COOLDOWN_HOURS * 3600)

    def test_failed_retry_does_not_bypass_cooldown(self):
        # Failed retries arrive with manual_search=True but must stay throttled.
        self.client.analyze.return_value = ({"matches": []}, False)
        search_matcher.match_results(self.show, self.episodes, self.items, manual_search=True, is_failed_retry=True)
        _, reserve_kwargs = self.throttle.reserve_search_attempt.call_args
        self.assertEqual(reserve_kwargs.get("cooldown_seconds"), search_matcher.SEARCH_MATCH_COOLDOWN_HOURS * 3600)

    def test_reserve_commit_success_share_one_scope_key(self):
        search_matcher.match_results(self.show, self.episodes, self.items, provider_id="nzbgeek", search_mode="episode")
        scope = "145211:nzbgeek:episode:s1"
        self.assertEqual(self.throttle.reserve_search_attempt.call_args.kwargs.get("scope_key"), scope)
        self.assertEqual(self.throttle.commit_search_attempt.call_args.kwargs.get("scope_key"), scope)
        # this response has a match -> record_search_success must use the same keyed row
        self.assertEqual(self.throttle.record_search_success.call_args.kwargs.get("scope_key"), scope)


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


class TestDedupeUnmatched(unittest.TestCase):
    def test_collapses_same_url_preserving_order(self):
        items = [
            {"title": "Working S1-02 [BD]", "url": "http://x/2", "size": 100},
            {"title": "Working S1-03 [BD]", "url": "http://x/3", "size": 100},
            {"title": "Working!! 02 (BD)", "url": "http://x/2", "size": 999},  # dup url, different title/size
        ]
        out = search_matcher._dedupe_unmatched(items)
        self.assertEqual([i["url"] for i in out], ["http://x/2", "http://x/3"])

    def test_keeps_distinct_urls(self):
        items = [{"title": "a", "url": "u1"}, {"title": "b", "url": "u2"}]
        self.assertEqual(len(search_matcher._dedupe_unmatched(items)), 2)

    def test_url_less_falls_back_to_normalized_title_and_size(self):
        items = [
            {"title": "Working!! - 02", "url": "", "size": 100},
            {"title": "working   02", "url": None, "size": 100},  # same normalized title + size -> dup
            {"title": "Working!! - 02", "url": "", "size": 200},  # same title, different size -> kept
        ]
        out = search_matcher._dedupe_unmatched(items)
        self.assertEqual(len(out), 2)

    def test_normalize_title(self):
        self.assertEqual(search_matcher._normalize_title("[Moozzi2] Working!! - 02"), "moozzi2working02")
        self.assertEqual(search_matcher._normalize_title(None), "")


class TestThrottleScopeOverride(conftest.SickChillTestDBCase):
    def setUp(self):
        super().setUp()
        s_patcher = mock.patch("sickchill.oldbeard.ai.throttle.settings")
        s = s_patcher.start()
        self.addCleanup(s_patcher.stop)
        s.AI_ENABLED = True
        s.AI_SEARCH_ENABLED = True
        s.AI_MAX_CALLS_PER_HOUR = 10000
        s.AI_MAX_CALLS_PER_DAY = 10000
        # Default-cooldown path reads per-show prefs; pin it so these tests don't depend on settings.
        cd_patcher = mock.patch.object(ThrottleManager, "_get_cooldown_days_for_show", return_value=7)
        cd_patcher.start()
        self.addCleanup(cd_patcher.stop)
        # cache.db is shared across the whole test session (teardown does not delete it), so clear
        # the throttle table to isolate cooldown assertions from rows left by earlier AI tests.
        ThrottleManager()  # ensures ai_throttle exists
        from sickchill.oldbeard import db as _db

        _db.DBConnection("cache.db").action("DELETE FROM ai_throttle")

    def test_custom_scope_keys_isolate_cooldowns(self):
        throttle = ThrottleManager()
        show = types.SimpleNamespace(name="Show", indexerid=777)
        ctx = ThrottleManager.CONTEXT_SEARCH_MATCH
        long_cd = 6 * 3600
        # Reserve+commit season 1 with a long cooldown.
        self.assertTrue(throttle.reserve_search_attempt(show, context=ctx, scope_key="777:p:episode:s1", cooldown_seconds=long_cd))
        throttle.commit_search_attempt(show, context=ctx, scope_key="777:p:episode:s1")
        # Season 1 again is now blocked...
        self.assertFalse(throttle.reserve_search_attempt(show, context=ctx, scope_key="777:p:episode:s1", cooldown_seconds=long_cd))
        # ...but season 2 (different scope key) is free.
        self.assertTrue(throttle.reserve_search_attempt(show, context=ctx, scope_key="777:p:episode:s2", cooldown_seconds=long_cd))

    def test_zero_cooldown_never_blocks_but_records_attempt(self):
        throttle = ThrottleManager()
        show = types.SimpleNamespace(name="Show", indexerid=771)
        ctx = ThrottleManager.CONTEXT_SEARCH_MATCH
        key = "771:p:episode:s1"
        self.assertTrue(throttle.reserve_search_attempt(show, context=ctx, scope_key=key, cooldown_seconds=0))
        throttle.commit_search_attempt(show, context=ctx, scope_key=key)
        # last_attempt was recorded...
        self.assertIsNotNone(throttle._get_last_attempt(ctx, ThrottleManager.SCOPE_SHOW, key))
        # ...yet a zero cooldown still allows an immediate re-reserve.
        self.assertTrue(throttle.reserve_search_attempt(show, context=ctx, scope_key=key, cooldown_seconds=0))

    def test_default_scope_and_cooldown_unchanged_for_advisor(self):
        throttle = ThrottleManager()
        show = types.SimpleNamespace(name="Show", indexerid=770)
        # No overrides -> advisor path keys on the bare indexerid under CONTEXT_SEARCH.
        self.assertTrue(throttle.reserve_search_attempt(show))
        throttle.commit_search_attempt(show)
        self.assertIsNotNone(throttle._get_last_attempt(ThrottleManager.CONTEXT_SEARCH, ThrottleManager.SCOPE_SHOW, "770"))


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


class TestPromptTemplate(unittest.TestCase):
    """Format the REAL search_match.txt to catch brace-escaping / dangling-comma regressions."""

    def _format(self, reasoning_field):
        template = search_matcher._load_prompt_template()
        return template.format(
            show_name="Bleach",
            aliases="None",
            wanted_json="[]",
            releases_json="[]",
            reasoning_field=reasoning_field,
        )

    def test_formats_without_reasoning_no_dangling_comma(self):
        out = self._format("")  # default (off)
        # The instructional text may mention the word "reasoning"; what must be absent is the
        # response *field*.
        self.assertNotIn('"reasoning":', out)
        self.assertIn("<0.0-1.0>", out)
        self.assertNotIn("<0.0-1.0>,", out)  # confidence is the last field; no trailing comma

    def test_formats_with_reasoning_when_enabled(self):
        out = self._format(',\n      "reasoning": "<brief explanation>"')
        self.assertIn('"reasoning": "<brief explanation>"', out)
        self.assertIn("<0.0-1.0>,", out)

    def test_template_has_no_unexpected_format_fields(self):
        # If a literal { were left unescaped the .format above would already KeyError; this also
        # guards the exact placeholder name the matcher passes.
        self.assertIn("{reasoning_field}", search_matcher._load_prompt_template())


class TestReleaseCap(unittest.TestCase):
    def _cap_for(self, effort):
        with mock.patch.object(search_matcher.settings, "AI_CLI_EFFORT", effort, create=True):
            return search_matcher._max_releases_per_request()

    def test_low_and_medium_effort_cap_thirty(self):
        self.assertEqual(self._cap_for("low"), 30)
        self.assertEqual(self._cap_for("medium"), 30)

    def test_high_efforts_cap_twenty(self):
        for effort in ("high", "xhigh", "max"):
            self.assertEqual(self._cap_for(effort), 20)

    def test_unknown_effort_defaults_to_thirty(self):
        self.assertEqual(self._cap_for("bogus"), 30)


if __name__ == "__main__":
    unittest.main()
