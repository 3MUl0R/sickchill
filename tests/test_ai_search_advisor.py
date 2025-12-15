"""
Tests for AI Search Advisor functionality.

Tests:
    TestResultFormatting - Formatting search results for AI prompts
    TestAnalyzeResults - AI analysis of search results
    TestAIFallbackDecision - Decision logic for when to use AI fallback
    TestValidation - Re-validation of AI-selected results
"""

from __future__ import annotations

import unittest
from unittest import mock

from sickchill.oldbeard.ai.search_advisor import (
    _format_results_for_prompt,
    _get_failed_releases_for_show,
    _get_preferred_words,
    _validate_ai_selection,
    analyze_search_results,
    should_use_ai_fallback,
)


class MockSearchResult:
    """Mock SearchResult for testing."""

    def __init__(
        self,
        name="Test.Show.S01E01.720p.HDTV.x264-GROUP",
        quality=4,  # HDTV
        size=350 * 1024 * 1024,  # 350 MB
        provider_name="TestProvider",
        release_group="GROUP",
        episodes=None,
        show=None,
    ):
        self.name = name
        self.quality = quality
        self.size = size
        self.release_group = release_group
        self.is_torrent = True

        # Mock provider
        self.provider = mock.MagicMock()
        self.provider.name = provider_name

        # Mock episodes
        if episodes is None:
            episode = mock.MagicMock()
            episode.season = 1
            episode.episode = 1
            self.episodes = [episode]
        else:
            self.episodes = episodes

        # Show reference (for validation)
        self.show = show


class MockTVShow:
    """Mock TVShow for testing."""

    def __init__(
        self,
        name="Test Show",
        indexerid=12345,
        quality=4,  # HDTV
        rls_require_words="",
        rls_ignore_words="",
        rls_prefer_words="",
        is_anime=False,
    ):
        self.name = name
        self.indexerid = indexerid
        self.quality = quality
        self.rls_require_words = rls_require_words
        self.rls_ignore_words = rls_ignore_words
        self.rls_prefer_words = rls_prefer_words
        self.is_anime = is_anime
        self.release_groups = mock.MagicMock()
        self.release_groups.is_valid.return_value = True


class MockTVEpisode:
    """Mock TVEpisode for testing."""

    def __init__(self, show=None, season=1, episode=1):
        self.show = show or MockTVShow()
        self.season = season
        self.episode = episode


class TestResultFormatting(unittest.TestCase):
    """Test formatting search results for AI prompts."""

    def test_format_single_result(self):
        """Test formatting a single search result."""
        results = [MockSearchResult()]

        formatted = _format_results_for_prompt(results)

        # Should be valid JSON
        import json
        data = json.loads(formatted)

        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["index"], 0)
        self.assertIn("name", data[0])
        self.assertIn("quality", data[0])
        self.assertIn("size_mb", data[0])

    def test_format_multiple_results(self):
        """Test formatting multiple search results."""
        results = [
            MockSearchResult(name="Show.S01E01.720p-A", release_group="A"),
            MockSearchResult(name="Show.S01E01.1080p-B", release_group="B"),
            MockSearchResult(name="Show.S01E01.480p-C", release_group="C"),
        ]

        formatted = _format_results_for_prompt(results)

        import json
        data = json.loads(formatted)

        self.assertEqual(len(data), 3)
        self.assertEqual(data[0]["index"], 0)
        self.assertEqual(data[1]["index"], 1)
        self.assertEqual(data[2]["index"], 2)

    def test_format_result_with_unknown_provider(self):
        """Test formatting result when provider is None."""
        result = MockSearchResult()
        result.provider = None

        formatted = _format_results_for_prompt([result])

        import json
        data = json.loads(formatted)

        self.assertEqual(data[0]["provider"], "Unknown")

    def test_format_result_size_conversion(self):
        """Test that file size is converted to MB."""
        result = MockSearchResult(size=500 * 1024 * 1024)  # 500 MB

        formatted = _format_results_for_prompt([result])

        import json
        data = json.loads(formatted)

        self.assertEqual(data[0]["size_mb"], 500)

    def test_format_result_negative_size(self):
        """Test handling of unknown file size (-1)."""
        result = MockSearchResult(size=-1)

        formatted = _format_results_for_prompt([result])

        import json
        data = json.loads(formatted)

        self.assertEqual(data[0]["size_mb"], -1)


class TestPreferredWords(unittest.TestCase):
    """Test getting preferred words for a show."""

    def setUp(self):
        """Set up test with mocked settings."""
        self.settings_patcher = mock.patch("sickchill.oldbeard.ai.search_advisor.settings")
        self.mock_settings = self.settings_patcher.start()
        self.mock_settings.PREFER_WORDS = ""

    def tearDown(self):
        """Clean up patches."""
        self.settings_patcher.stop()

    def test_show_prefer_words_only(self):
        """Test with only show-level preferred words."""
        show = MockTVShow(rls_prefer_words="x264, bluray")

        result = _get_preferred_words(show)

        self.assertIn("x264", result)
        self.assertIn("bluray", result)

    def test_global_prefer_words_only(self):
        """Test with only global preferred words."""
        self.mock_settings.PREFER_WORDS = "hevc, hdr"
        show = MockTVShow()

        result = _get_preferred_words(show)

        self.assertIn("hevc", result)
        self.assertIn("hdr", result)

    def test_combined_prefer_words(self):
        """Test with both show and global preferred words."""
        self.mock_settings.PREFER_WORDS = "hevc"
        show = MockTVShow(rls_prefer_words="x264")

        result = _get_preferred_words(show)

        self.assertIn("x264", result)
        self.assertIn("hevc", result)

    def test_empty_prefer_words(self):
        """Test with no preferred words."""
        show = MockTVShow()

        result = _get_preferred_words(show)

        self.assertEqual(result, "")


class TestShouldUseAIFallback(unittest.TestCase):
    """Test the decision logic for when to use AI fallback."""

    def setUp(self):
        """Set up test with mocked settings and preferences."""
        self.settings_patcher = mock.patch("sickchill.oldbeard.ai.search_advisor.settings")
        self.mock_settings = self.settings_patcher.start()

        self.mock_settings.AI_ENABLED = True
        self.mock_settings.AI_SEARCH_ENABLED = True
        self.mock_settings.AI_SEARCH_ONLY_ON_FAILURE = True
        self.mock_settings.AI_SEARCH_MIN_RESULTS = 1

        # Mock preferences manager
        self.prefs_patcher = mock.patch("sickchill.oldbeard.ai.search_advisor.get_preferences_manager")
        self.mock_get_prefs = self.prefs_patcher.start()
        self.mock_prefs_manager = mock.MagicMock()
        self.mock_get_prefs.return_value = self.mock_prefs_manager
        # Default: preferences manager allows AI search
        self.mock_prefs_manager.should_use_ai_search.return_value = True

    def tearDown(self):
        """Clean up patches."""
        self.settings_patcher.stop()
        self.prefs_patcher.stop()

    def test_should_not_use_when_result_found(self):
        """Test that AI is not used when pick_best_result returned a result."""
        # When we have a result, preferences manager should return False
        self.mock_prefs_manager.should_use_ai_search.return_value = False

        results = [MockSearchResult()]
        show = MockTVShow()
        picked_result = MockSearchResult()

        self.assertFalse(should_use_ai_fallback(results, show, picked_result))

    def test_should_use_when_no_result_picked(self):
        """Test that AI is used when pick_best_result returned None."""
        results = [MockSearchResult()]
        show = MockTVShow()
        picked_result = None

        self.assertTrue(should_use_ai_fallback(results, show, picked_result))

    def test_should_not_use_when_no_results(self):
        """Test that AI is not used when there are no results to analyze."""
        results = []
        show = MockTVShow()
        picked_result = None

        self.assertFalse(should_use_ai_fallback(results, show, picked_result))

    def test_should_not_use_when_ai_disabled(self):
        """Test that AI is not used when AI is disabled."""
        self.mock_prefs_manager.should_use_ai_search.return_value = False

        results = [MockSearchResult()]
        show = MockTVShow()
        picked_result = None

        self.assertFalse(should_use_ai_fallback(results, show, picked_result))

    def test_should_not_use_when_search_ai_disabled(self):
        """Test that AI is not used when AI search specifically is disabled."""
        self.mock_prefs_manager.should_use_ai_search.return_value = False

        results = [MockSearchResult()]
        show = MockTVShow()
        picked_result = None

        self.assertFalse(should_use_ai_fallback(results, show, picked_result))

    def test_should_not_use_when_below_min_results(self):
        """Test that AI is not used when results count is below minimum."""
        self.mock_settings.AI_SEARCH_MIN_RESULTS = 5

        results = [MockSearchResult(), MockSearchResult()]  # Only 2 results
        show = MockTVShow()
        picked_result = None

        self.assertFalse(should_use_ai_fallback(results, show, picked_result))


class TestValidateAISelection(unittest.TestCase):
    """Test re-validation of AI-selected results."""

    def setUp(self):
        """Set up test with mocked dependencies."""
        self.settings_patcher = mock.patch("sickchill.oldbeard.ai.search_advisor.settings")
        self.mock_settings = self.settings_patcher.start()
        self.mock_settings.USE_FAILED_DOWNLOADS = False

        self.filter_patcher = mock.patch("sickchill.oldbeard.ai.search_advisor.show_name_helpers.filter_bad_releases")
        self.mock_filter = self.filter_patcher.start()
        self.mock_filter.return_value = True  # Default: passes filter

    def tearDown(self):
        """Clean up patches."""
        self.settings_patcher.stop()
        self.filter_patcher.stop()

    def test_valid_result_passes(self):
        """Test that a valid result passes validation."""
        show = MockTVShow(quality=4)  # HDTV
        episode = MockTVEpisode(show=show)
        result = MockSearchResult(quality=4, show=show)

        is_valid, reason = _validate_ai_selection(result, show, episode)

        self.assertTrue(is_valid)
        self.assertIsNone(reason)

    def test_wrong_quality_fails(self):
        """Test that wrong quality fails validation."""
        show = MockTVShow(quality=4)  # Only HDTV allowed
        episode = MockTVEpisode(show=show)
        result = MockSearchResult(quality=128, show=show)  # HDBLURAY

        is_valid, reason = _validate_ai_selection(result, show, episode)

        self.assertFalse(is_valid)
        self.assertIn("Quality", reason)

    def test_anime_invalid_release_group_fails(self):
        """Test that invalid anime release group fails validation."""
        show = MockTVShow(quality=4, is_anime=True)
        show.release_groups.is_valid.return_value = False
        episode = MockTVEpisode(show=show)
        result = MockSearchResult(quality=4, show=show)

        is_valid, reason = _validate_ai_selection(result, show, episode)

        self.assertFalse(is_valid)
        self.assertIn("release group", reason.lower())

    def test_filter_bad_releases_fails(self):
        """Test that result failing filter_bad_releases is rejected."""
        self.mock_filter.return_value = False

        show = MockTVShow(quality=4)
        episode = MockTVEpisode(show=show)
        result = MockSearchResult(quality=4, show=show)

        is_valid, reason = _validate_ai_selection(result, show, episode)

        self.assertFalse(is_valid)
        self.assertIn("ignored words", reason.lower())

    @mock.patch("sickchill.show.History.History")
    def test_failed_download_history_fails(self, mock_history_class):
        """Test that result in failed history is rejected."""
        self.mock_settings.USE_FAILED_DOWNLOADS = True
        mock_history = mock.MagicMock()
        mock_history.has_failed.return_value = True
        mock_history_class.return_value = mock_history

        show = MockTVShow(quality=4)
        episode = MockTVEpisode(show=show)
        result = MockSearchResult(quality=4, show=show)

        is_valid, reason = _validate_ai_selection(result, show, episode)

        self.assertFalse(is_valid)
        self.assertIn("failed", reason.lower())

    def test_wrong_show_fails(self):
        """Test that result for wrong show fails validation."""
        show = MockTVShow(name="Show A")
        other_show = MockTVShow(name="Show B")
        episode = MockTVEpisode(show=show)
        result = MockSearchResult(quality=4, show=other_show)

        is_valid, reason = _validate_ai_selection(result, show, episode)

        self.assertFalse(is_valid)
        self.assertIn("different show", reason.lower())


class TestAnalyzeResults(unittest.TestCase):
    """Test AI analysis of search results."""

    def setUp(self):
        """Set up test with mocked dependencies."""
        self.settings_patcher = mock.patch("sickchill.oldbeard.ai.search_advisor.settings")
        self.mock_settings = self.settings_patcher.start()

        self.mock_settings.AI_ENABLED = True
        self.mock_settings.AI_SEARCH_ENABLED = True
        self.mock_settings.AI_CONFIDENCE_THRESHOLD = 0.80
        self.mock_settings.AI_NOTIFY_ON_FALLBACK_FAILURE = False
        self.mock_settings.PREFER_WORDS = ""
        self.mock_settings.USE_FAILED_DOWNLOADS = False
        self.mock_settings.AI_SEARCH_ALLOW_RELAX_FILTERS = False

        self.ai_available_patcher = mock.patch("sickchill.oldbeard.ai.search_advisor.is_ai_available")
        self.mock_ai_available = self.ai_available_patcher.start()
        self.mock_ai_available.return_value = True

        self.get_throttle_patcher = mock.patch("sickchill.oldbeard.ai.search_advisor.get_throttle")
        self.mock_get_throttle = self.get_throttle_patcher.start()
        self.mock_throttle = mock.MagicMock()
        self.mock_throttle.reserve_search_attempt.return_value = True
        self.mock_get_throttle.return_value = self.mock_throttle

        self.get_client_patcher = mock.patch("sickchill.oldbeard.ai.search_advisor.get_client")
        self.mock_get_client = self.get_client_patcher.start()
        self.mock_client = mock.MagicMock()
        self.mock_get_client.return_value = self.mock_client

        # Mock prompt template loading
        self.load_template_patcher = mock.patch("sickchill.oldbeard.ai.search_advisor._load_prompt_template")
        self.mock_load_template = self.load_template_patcher.start()
        self.mock_load_template.return_value = "Test prompt: {show_name} {season} {episode} {quality} {preferred_words} {ignored_words} {results_json} {failed_releases}"

        # Mock failed releases
        self.failed_patcher = mock.patch("sickchill.oldbeard.ai.search_advisor._get_failed_releases_for_show")
        self.mock_failed = self.failed_patcher.start()
        self.mock_failed.return_value = []

        # Mock validation (default: passes)
        self.validate_patcher = mock.patch("sickchill.oldbeard.ai.search_advisor._validate_ai_selection")
        self.mock_validate = self.validate_patcher.start()
        self.mock_validate.return_value = (True, None)

    def tearDown(self):
        """Clean up patches."""
        self.settings_patcher.stop()
        self.ai_available_patcher.stop()
        self.get_throttle_patcher.stop()
        self.get_client_patcher.stop()
        self.load_template_patcher.stop()
        self.failed_patcher.stop()
        self.validate_patcher.stop()

    def test_returns_none_when_no_results(self):
        """Test that None is returned when results list is empty."""
        episode = MockTVEpisode()

        result = analyze_search_results([], episode)

        self.assertIsNone(result)

    def test_returns_none_when_ai_not_available(self):
        """Test that None is returned when AI is not available."""
        self.mock_ai_available.return_value = False
        results = [MockSearchResult()]
        episode = MockTVEpisode()

        result = analyze_search_results(results, episode)

        self.assertIsNone(result)

    def test_returns_none_when_throttled(self):
        """Test that None is returned when AI is throttled."""
        self.mock_throttle.reserve_search_attempt.return_value = False
        results = [MockSearchResult()]
        episode = MockTVEpisode()

        result = analyze_search_results(results, episode)

        self.assertIsNone(result)

    def test_returns_selected_result_on_success(self):
        """Test that the selected result is returned on successful analysis."""
        show = MockTVShow()
        results = [
            MockSearchResult(name="Result0", show=show),
            MockSearchResult(name="Result1", show=show),
            MockSearchResult(name="Result2", show=show),
        ]
        episode = MockTVEpisode(show=show)

        # AI selects index 1 with high confidence
        self.mock_client.analyze.return_value = {
            "selected_index": 1,
            "confidence": 0.95,
            "reasoning": "Best quality and seeders",
        }

        result = analyze_search_results(results, episode)

        self.assertIsNotNone(result)
        self.assertEqual(result.name, "Result1")

    def test_returns_none_when_ai_rejects_all(self):
        """Test that None is returned when AI finds no acceptable result."""
        results = [MockSearchResult()]
        episode = MockTVEpisode()

        # AI returns -1 to indicate no acceptable result
        self.mock_client.analyze.return_value = {
            "selected_index": -1,
            "confidence": 0.0,
            "reasoning": "All results have issues",
        }

        result = analyze_search_results(results, episode)

        self.assertIsNone(result)

    def test_returns_none_when_confidence_too_low(self):
        """Test that None is returned when AI confidence is below threshold."""
        results = [MockSearchResult()]
        episode = MockTVEpisode()

        # AI selects index 0 but with low confidence
        self.mock_client.analyze.return_value = {
            "selected_index": 0,
            "confidence": 0.50,  # Below 0.80 threshold
            "reasoning": "Uncertain about this result",
        }

        result = analyze_search_results(results, episode)

        self.assertIsNone(result)

    def test_returns_none_when_invalid_index(self):
        """Test that None is returned when AI returns an invalid index."""
        results = [MockSearchResult()]  # Only 1 result (index 0)
        episode = MockTVEpisode()

        # AI returns invalid index
        self.mock_client.analyze.return_value = {
            "selected_index": 5,  # Invalid - only 1 result
            "confidence": 0.95,
            "reasoning": "Selected result 5",
        }

        result = analyze_search_results(results, episode)

        self.assertIsNone(result)

    def test_returns_none_when_validation_fails(self):
        """Test that None is returned when AI selection fails validation."""
        self.mock_validate.return_value = (False, "Quality not allowed")

        results = [MockSearchResult()]
        episode = MockTVEpisode()

        self.mock_client.analyze.return_value = {
            "selected_index": 0,
            "confidence": 0.95,
            "reasoning": "Good result",
        }

        result = analyze_search_results(results, episode)

        self.assertIsNone(result)

    def test_records_attempt_after_api_call(self):
        """Test that attempt is committed after successful API call."""
        show = MockTVShow()
        results = [MockSearchResult(show=show)]
        episode = MockTVEpisode(show=show)

        self.mock_client.analyze.return_value = {
            "selected_index": 0,
            "confidence": 0.95,
            "reasoning": "Good result",
        }

        analyze_search_results(results, episode)

        # Should have committed attempt and recorded success
        self.mock_throttle.commit_search_attempt.assert_called_once()
        self.mock_throttle.record_search_success.assert_called_once()

    def test_records_attempt_but_not_success_on_failure(self):
        """Test that attempt is committed but success is not recorded when AI rejects all."""
        results = [MockSearchResult()]
        episode = MockTVEpisode()

        self.mock_client.analyze.return_value = {
            "selected_index": -1,
            "confidence": 0.0,
            "reasoning": "No acceptable result",
        }

        analyze_search_results(results, episode)

        # API call succeeded so attempt was committed, but AI found no result
        self.mock_throttle.commit_search_attempt.assert_called_once()
        self.mock_throttle.record_search_success.assert_not_called()

    def test_passes_is_failed_retry_to_system_prompt(self):
        """Test that failed retry flag affects the system prompt."""
        show = MockTVShow()
        results = [MockSearchResult(show=show)]
        episode = MockTVEpisode(show=show)

        self.mock_client.analyze.return_value = {
            "selected_index": 0,
            "confidence": 0.95,
            "reasoning": "Good result",
        }

        # Call with is_failed_retry=True
        analyze_search_results(results, episode, is_failed_retry=True)

        # Check that analyze was called with a system_prompt
        call_args = self.mock_client.analyze.call_args
        self.assertIsNotNone(call_args.kwargs.get("system_prompt"))
        self.assertIn("failed downloads", call_args.kwargs["system_prompt"])


class TestGetFailedReleases(unittest.TestCase):
    """Test getting failed releases for a show."""

    @mock.patch("sickchill.oldbeard.ai.search_advisor.db.DBConnection")
    def test_returns_matching_releases(self, mock_db_class):
        """Test that matching failed releases are returned."""
        mock_db = mock.MagicMock()
        mock_db_class.return_value = mock_db

        # Mock database results
        mock_db.select.return_value = [
            {"release": "Test_Show_S01E01_720p"},
            {"release": "Other_Show_S01E01_720p"},
            {"release": "Test_Show_S01E02_1080p"},
        ]

        show = MockTVShow(name="Test Show")

        releases = _get_failed_releases_for_show(show)

        # Should only return releases matching the show name
        self.assertEqual(len(releases), 2)
        self.assertIn("Test_Show_S01E01_720p", releases)
        self.assertIn("Test_Show_S01E02_1080p", releases)

    @mock.patch("sickchill.oldbeard.ai.search_advisor.db.DBConnection")
    def test_returns_empty_list_on_error(self, mock_db_class):
        """Test that empty list is returned on database error."""
        mock_db_class.side_effect = Exception("Database error")

        show = MockTVShow()

        releases = _get_failed_releases_for_show(show)

        self.assertEqual(releases, [])

    @mock.patch("sickchill.oldbeard.ai.search_advisor.db.DBConnection")
    def test_limits_to_ten_releases(self, mock_db_class):
        """Test that results are limited to 10 releases."""
        mock_db = mock.MagicMock()
        mock_db_class.return_value = mock_db

        # Return 15 matching releases
        mock_db.select.return_value = [
            {"release": f"Test_Show_S01E{i:02d}_720p"} for i in range(1, 16)
        ]

        show = MockTVShow(name="Test Show")

        releases = _get_failed_releases_for_show(show)

        # Should be limited to 10
        self.assertLessEqual(len(releases), 10)


if __name__ == "__main__":
    unittest.main()
