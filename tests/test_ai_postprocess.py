"""
Tests for AI Post-Processing functionality.

Tests:
    TestPostprocessMatcher - AI-powered file matching
    TestPostprocessAnalyzer - AI-powered quality verification
"""

from __future__ import annotations

import unittest
from unittest import mock


class MockTVShow:
    """Mock TVShow for testing."""

    def __init__(
        self,
        name="Test Show",
        indexerid=12345,
        indexer=1,
    ):
        self.name = name
        self.indexerid = indexerid
        self.indexer = indexer


class MockTVEpisode:
    """Mock TVEpisode for testing."""

    def __init__(self, show=None, season=1, episode=1):
        self.show = show or MockTVShow()
        self.season = season
        self.episode = episode


class TestMatchFileFunction(unittest.TestCase):
    """Test the match_file function in postprocess_matcher."""

    def setUp(self):
        """Set up test with mocked dependencies."""
        self.settings_patcher = mock.patch("sickchill.oldbeard.ai.postprocess_matcher.settings")
        self.mock_settings = self.settings_patcher.start()

        self.mock_settings.AI_ENABLED = True
        self.mock_settings.AI_POSTPROCESS_MATCH_ENABLED = True
        self.mock_settings.AI_POSTPROCESS_MATCH_ONLY_ON_FAILURE = True
        self.mock_settings.AI_POSTPROCESS_MATCH_MIN_CONFIDENCE = 0.85
        self.mock_settings.AI_NOTIFY_ON_FALLBACK_FAILURE = False

        self.ai_available_patcher = mock.patch("sickchill.oldbeard.ai.postprocess_matcher.is_ai_available")
        self.mock_ai_available = self.ai_available_patcher.start()
        self.mock_ai_available.return_value = True

        self.get_throttle_patcher = mock.patch("sickchill.oldbeard.ai.postprocess_matcher.get_throttle")
        self.mock_get_throttle = self.get_throttle_patcher.start()
        self.mock_throttle = mock.MagicMock()
        # Use the new atomic reservation method
        self.mock_throttle.reserve_postprocess_attempt.return_value = True
        self.mock_get_throttle.return_value = self.mock_throttle

        self.get_client_patcher = mock.patch("sickchill.oldbeard.ai.postprocess_matcher.get_client")
        self.mock_get_client = self.get_client_patcher.start()
        self.mock_client = mock.MagicMock()
        self.mock_get_client.return_value = self.mock_client

        # analyze() supports return_meta=True -> (response, was_cached). Adapt each test's
        # configured return_value (a bare dict, or an explicit (response, cached) tuple) to it.
        def _analyze_side_effect(*args, **kwargs):
            rv = self.mock_client.analyze.return_value
            if kwargs.get("return_meta"):
                return rv if isinstance(rv, tuple) else (rv, False)
            return rv[0] if isinstance(rv, tuple) else rv

        self.mock_client.analyze.side_effect = _analyze_side_effect

        # Mock candidate shows
        self.candidates_patcher = mock.patch("sickchill.oldbeard.ai.postprocess_matcher._get_candidate_shows")
        self.mock_get_candidates = self.candidates_patcher.start()
        self.mock_get_candidates.return_value = [
            {"indexer_id": 12345, "indexer": 1, "name": "Test Show", "aliases": [], "score": 0.9},
            {"indexer_id": 67890, "indexer": 1, "name": "Other Show", "aliases": [], "score": 0.5},
        ]

        # Mock prompt template
        self.template_patcher = mock.patch("sickchill.oldbeard.ai.postprocess_matcher._load_prompt_template")
        self.mock_template = self.template_patcher.start()
        self.mock_template.return_value = (
            "Test prompt: {filename} {folder_name} {relative_path} {release_name} {file_size_mb} {duration} {candidate_shows_json}"
        )

    def tearDown(self):
        """Clean up patches."""
        self.settings_patcher.stop()
        self.ai_available_patcher.stop()
        self.get_throttle_patcher.stop()
        self.get_client_patcher.stop()
        self.candidates_patcher.stop()
        self.template_patcher.stop()

    def test_returns_none_when_ai_not_available(self):
        """Test that None is returned when AI is not available."""
        from sickchill.oldbeard.ai.postprocess_matcher import match_file

        self.mock_ai_available.return_value = False

        result = match_file("/path/to/file.mkv", "file.mkv", "folder")

        self.assertIsNone(result)

    def test_returns_none_when_disabled(self):
        """Test that None is returned when post-process matching is disabled."""
        from sickchill.oldbeard.ai.postprocess_matcher import match_file

        self.mock_settings.AI_POSTPROCESS_MATCH_ENABLED = False

        result = match_file("/path/to/file.mkv", "file.mkv", "folder")

        self.assertIsNone(result)

    def test_returns_none_when_throttled(self):
        """Test that None is returned when throttled."""
        from sickchill.oldbeard.ai.postprocess_matcher import match_file

        self.mock_throttle.reserve_postprocess_attempt.return_value = False

        result = match_file("/path/to/file.mkv", "file.mkv", "folder")

        self.assertIsNone(result)

    def test_returns_none_when_no_candidates(self):
        """Test that None is returned when there are no candidate shows."""
        from sickchill.oldbeard.ai.postprocess_matcher import match_file

        self.mock_get_candidates.return_value = []

        result = match_file("/path/to/file.mkv", "file.mkv", "folder")

        self.assertIsNone(result)

    def test_returns_match_on_success(self):
        """Test that match result is returned on successful AI match."""
        from sickchill.oldbeard.ai.postprocess_matcher import match_file

        self.mock_client.analyze.return_value = {
            "show_indexer_id": 12345,
            "season": 1,
            "episodes": [5],
            "confidence": 0.95,
            "reasoning": "Clear match",
        }

        result = match_file("/path/to/file.mkv", "Test.Show.S01E05.mkv", "Test Show")

        self.assertIsNotNone(result)
        self.assertEqual(result["show_indexer_id"], 12345)
        self.assertEqual(result["season"], 1)
        self.assertEqual(result["episodes"], [5])
        # A confident match must NOT cooldown-lock the file (so a later post-processing
        # failure can be retried immediately).
        self.mock_throttle.commit_postprocess_attempt.assert_called_once()
        self.assertFalse(self.mock_throttle.commit_postprocess_attempt.call_args.kwargs["set_cooldown"])

    def test_cache_hit_does_not_bill_budget(self):
        """A cached match still commits the cooldown but with count_budget=False."""
        from sickchill.oldbeard.ai.postprocess_matcher import match_file

        # Explicit (response, was_cached=True) tuple simulates a response-cache hit.
        self.mock_client.analyze.return_value = (
            {"show_indexer_id": 12345, "season": 1, "episodes": [5], "confidence": 0.95, "reasoning": "cached"},
            True,
        )

        result = match_file("/path/to/file.mkv", "Test.Show.S01E05.mkv", "Test Show")

        self.assertIsNotNone(result)
        self.mock_throttle.commit_postprocess_attempt.assert_called_once()
        self.assertFalse(self.mock_throttle.commit_postprocess_attempt.call_args.kwargs["count_budget"])

    def test_returns_none_when_ai_cant_match(self):
        """Test that None is returned when AI can't identify file."""
        from sickchill.oldbeard.ai.postprocess_matcher import match_file

        self.mock_client.analyze.return_value = {
            "show_indexer_id": -1,
            "season": None,
            "episodes": [],
            "confidence": 0.0,
            "reasoning": "Cannot identify",
        }

        result = match_file("/path/to/file.mkv", "random_file.mkv", "random")

        self.assertIsNone(result)
        # A genuine no-match imposes the per-file cooldown to avoid hammering.
        self.assertTrue(self.mock_throttle.commit_postprocess_attempt.call_args.kwargs["set_cooldown"])

    def test_returns_none_when_confidence_too_low(self):
        """Test that None is returned when confidence is below threshold."""
        from sickchill.oldbeard.ai.postprocess_matcher import match_file

        self.mock_client.analyze.return_value = {
            "show_indexer_id": 12345,
            "season": 1,
            "episodes": [5],
            "confidence": 0.50,  # Below 0.85 threshold
            "reasoning": "Uncertain match",
        }

        result = match_file("/path/to/file.mkv", "file.mkv", "folder")

        self.assertIsNone(result)
        # Below-threshold confidence counts as no usable match -> cooldown imposed.
        self.assertTrue(self.mock_throttle.commit_postprocess_attempt.call_args.kwargs["set_cooldown"])

    def test_returns_none_when_invalid_show_id(self):
        """Test that None is returned when AI returns invalid show ID."""
        from sickchill.oldbeard.ai.postprocess_matcher import match_file

        self.mock_client.analyze.return_value = {
            "show_indexer_id": 99999,  # Not in candidates
            "season": 1,
            "episodes": [5],
            "confidence": 0.95,
            "reasoning": "Match",
        }

        result = match_file("/path/to/file.mkv", "file.mkv", "folder")

        self.assertIsNone(result)
        # An invalid show id (not in candidates) is not a usable match -> cooldown imposed.
        self.assertTrue(self.mock_throttle.commit_postprocess_attempt.call_args.kwargs["set_cooldown"])


class TestShouldUseAIMatch(unittest.TestCase):
    """Test the should_use_ai_match function."""

    def setUp(self):
        """Set up test with mocked settings and preferences."""
        self.settings_patcher = mock.patch("sickchill.oldbeard.ai.postprocess_matcher.settings")
        self.mock_settings = self.settings_patcher.start()

        self.mock_settings.AI_ENABLED = True
        self.mock_settings.AI_POSTPROCESS_MATCH_ENABLED = True
        self.mock_settings.AI_POSTPROCESS_MATCH_ONLY_ON_FAILURE = True

        # Mock preferences manager
        self.prefs_patcher = mock.patch("sickchill.oldbeard.ai.postprocess_matcher.get_preferences_manager")
        self.mock_get_prefs = self.prefs_patcher.start()
        self.mock_prefs_manager = mock.MagicMock()
        self.mock_get_prefs.return_value = self.mock_prefs_manager
        # Default: preferences manager allows AI postprocess
        self.mock_prefs_manager.should_use_ai_postprocess.return_value = True

    def tearDown(self):
        """Clean up patches."""
        self.settings_patcher.stop()
        self.prefs_patcher.stop()

    def test_should_not_use_when_disabled(self):
        """Test that AI is not used when disabled."""
        from sickchill.oldbeard.ai.postprocess_matcher import should_use_ai_match

        self.mock_settings.AI_ENABLED = False

        result = should_use_ai_match(None, None, [])

        self.assertFalse(result)

    def test_should_use_when_show_missing(self):
        """Test that AI is used when show is not identified."""
        from sickchill.oldbeard.ai.postprocess_matcher import should_use_ai_match

        result = should_use_ai_match(None, 1, [1])

        self.assertTrue(result)

    def test_should_use_when_season_missing(self):
        """Test that AI is used when season is missing."""
        from sickchill.oldbeard.ai.postprocess_matcher import should_use_ai_match

        show = MockTVShow()

        result = should_use_ai_match(show, None, [1])

        self.assertTrue(result)

    def test_should_use_when_episodes_missing(self):
        """Test that AI is used when episodes are missing."""
        from sickchill.oldbeard.ai.postprocess_matcher import should_use_ai_match

        show = MockTVShow()

        result = should_use_ai_match(show, 1, [])

        self.assertTrue(result)

    def test_should_not_use_when_complete(self):
        """Test that AI is not used when complete info available."""
        from sickchill.oldbeard.ai.postprocess_matcher import should_use_ai_match

        # When complete info is available, preferences manager returns False
        self.mock_prefs_manager.should_use_ai_postprocess.return_value = False

        show = MockTVShow()

        result = should_use_ai_match(show, 1, [1])

        self.assertFalse(result)


class TestValidateMatchResult(unittest.TestCase):
    """Test the _validate_match_result function."""

    def test_valid_result_passes(self):
        """Test that a valid result passes validation."""
        from sickchill.oldbeard.ai.postprocess_matcher import _validate_match_result

        result = {
            "show_indexer_id": 12345,
            "season": 1,
            "episodes": [5],
            "confidence": 0.95,
        }
        candidates = [{"indexer_id": 12345}]

        is_valid, reason = _validate_match_result(result, candidates)

        self.assertTrue(is_valid)
        self.assertIsNone(reason)

    def test_negative_one_is_valid(self):
        """Test that -1 (no match) is a valid response."""
        from sickchill.oldbeard.ai.postprocess_matcher import _validate_match_result

        result = {
            "show_indexer_id": -1,
            "season": None,
            "episodes": [],
            "confidence": 0.0,
        }
        candidates = [{"indexer_id": 12345}]

        is_valid, reason = _validate_match_result(result, candidates)

        self.assertTrue(is_valid)
        self.assertIsNone(reason)

    def test_invalid_show_id_fails(self):
        """Test that show ID not in candidates fails."""
        from sickchill.oldbeard.ai.postprocess_matcher import _validate_match_result

        result = {
            "show_indexer_id": 99999,
            "season": 1,
            "episodes": [5],
            "confidence": 0.95,
        }
        candidates = [{"indexer_id": 12345}]

        is_valid, reason = _validate_match_result(result, candidates)

        self.assertFalse(is_valid)
        self.assertIn("not in candidate list", reason)

    def test_negative_season_fails(self):
        """Test that negative season fails validation."""
        from sickchill.oldbeard.ai.postprocess_matcher import _validate_match_result

        result = {
            "show_indexer_id": 12345,
            "season": -5,
            "episodes": [5],
            "confidence": 0.95,
        }
        candidates = [{"indexer_id": 12345}]

        is_valid, reason = _validate_match_result(result, candidates)

        self.assertFalse(is_valid)
        self.assertIn("Invalid season", reason)

    def test_missing_season_fails(self):
        """Test that match without season fails validation."""
        from sickchill.oldbeard.ai.postprocess_matcher import _validate_match_result

        result = {
            "show_indexer_id": 12345,
            "season": None,
            "episodes": [5],
            "confidence": 0.95,
        }
        candidates = [{"indexer_id": 12345}]

        is_valid, reason = _validate_match_result(result, candidates)

        self.assertFalse(is_valid)
        self.assertIn("season is missing", reason)

    def test_missing_episodes_fails(self):
        """Test that match without episodes fails validation."""
        from sickchill.oldbeard.ai.postprocess_matcher import _validate_match_result

        result = {
            "show_indexer_id": 12345,
            "season": 1,
            "episodes": [],
            "confidence": 0.95,
        }
        candidates = [{"indexer_id": 12345}]

        is_valid, reason = _validate_match_result(result, candidates)

        self.assertFalse(is_valid)
        self.assertIn("episodes list is empty", reason)


class TestGetCandidateShows(unittest.TestCase):
    """Test the _get_candidate_shows function directly."""

    def setUp(self):
        """Set up test with mocked settings."""
        self.settings_patcher = mock.patch("sickchill.oldbeard.ai.postprocess_matcher.settings")
        self.mock_settings = self.settings_patcher.start()

        # Create mock shows
        self.mock_show1 = MockTVShow(name="Breaking Bad", indexerid=1001, indexer=1)
        self.mock_show2 = MockTVShow(name="Game of Thrones", indexerid=1002, indexer=1)
        self.mock_show3 = MockTVShow(name="The Office", indexerid=1003, indexer=1)

        self.mock_settings.show_list = [self.mock_show1, self.mock_show2, self.mock_show3]

    def tearDown(self):
        """Clean up patches."""
        self.settings_patcher.stop()

    @mock.patch("sickchill.oldbeard.ai.postprocess_matcher._load_alias_map")
    def test_returns_candidates_sorted_by_score(self, mock_alias_map):
        """Test that candidates are returned sorted by similarity score."""
        from sickchill.oldbeard.ai.postprocess_matcher import _get_candidate_shows

        mock_alias_map.return_value = {}

        candidates = _get_candidate_shows("Breaking.Bad.S01E01.mkv", "Breaking Bad")

        self.assertTrue(len(candidates) > 0)
        # Breaking Bad should be first due to exact name match
        self.assertEqual(candidates[0]["name"], "Breaking Bad")

    @mock.patch("sickchill.oldbeard.ai.postprocess_matcher._load_alias_map")
    def test_includes_release_name_in_matching(self, mock_alias_map):
        """Test that release_name is used for matching."""
        from sickchill.oldbeard.ai.postprocess_matcher import _get_candidate_shows

        mock_alias_map.return_value = {}

        candidates = _get_candidate_shows("some_file.mkv", "downloads", release_name="Game.of.Thrones.S05E10")

        # Game of Thrones should score higher due to release_name
        got_found = any(c["name"] == "Game of Thrones" for c in candidates)
        self.assertTrue(got_found)

    @mock.patch("sickchill.oldbeard.ai.postprocess_matcher._load_alias_map")
    def test_handles_scene_exceptions_correctly(self, mock_alias_map):
        """Test that scene exceptions (aliases) are properly handled and surfaced in the candidate."""
        from sickchill.oldbeard.ai.postprocess_matcher import _get_candidate_shows

        # Aliases keyed by indexer_id (the new bulk-loaded format).
        mock_alias_map.return_value = {1002: ["GoT", "Game.of.Thrones"]}

        candidates = _get_candidate_shows("Game.S05E10.GoT.mkv", "Game of Thrones Season 5")

        # Find Game of Thrones candidate
        got_candidate = next((c for c in candidates if c["indexer_id"] == 1002), None)
        self.assertIsNotNone(got_candidate)
        # Should carry its aliases for the AI prompt.
        self.assertTrue(len(got_candidate["aliases"]) > 0)

    @mock.patch("sickchill.oldbeard.ai.postprocess_matcher._load_alias_map")
    def test_returns_empty_when_no_shows(self, mock_alias_map):
        """Test that empty list is returned when no shows in library."""
        from sickchill.oldbeard.ai.postprocess_matcher import _get_candidate_shows

        self.mock_settings.show_list = []
        mock_alias_map.return_value = {}

        candidates = _get_candidate_shows("some_file.mkv", "folder")

        self.assertEqual(candidates, [])

    @mock.patch("sickchill.oldbeard.ai.postprocess_matcher._load_alias_map")
    def test_limits_candidates_to_specified_limit(self, mock_alias_map):
        """Test that candidates are limited to the specified limit."""
        from sickchill.oldbeard.ai.postprocess_matcher import _get_candidate_shows

        mock_alias_map.return_value = {}

        candidates = _get_candidate_shows("file.mkv", "folder", limit=2)

        self.assertLessEqual(len(candidates), 2)

    @mock.patch("sickchill.oldbeard.ai.postprocess_matcher._load_alias_map")
    def test_alias_only_match_surfaces_show_with_unrelated_name(self, mock_alias_map):
        """Core regression: a show whose English library name does NOT resemble the release, but whose
        scene-exception alias (the romaji title) DOES, must still appear -- and rank at the top. Before
        the fix the show was dropped from the candidate list before its aliases were ever consulted."""
        from sickchill.oldbeard.ai.postprocess_matcher import _get_candidate_shows

        romaji_show = MockTVShow(name="I'm Standing on a Million Lives", indexerid=2001, indexer=1)
        self.mock_settings.show_list = [self.mock_show1, self.mock_show2, self.mock_show3, romaji_show]
        mock_alias_map.return_value = {2001: ["100-man no Inochi no Ue ni Ore wa Tatte Iru"]}

        candidates = _get_candidate_shows(
            "100-man.no.Inochi.no.Ue.ni.Ore.wa.Tatte.Iru.E04.MULTi.1080p.WEB.x264-AMB3R.mkv",
            "100-man no Inochi no Ue ni Ore wa Tatte Iru E04 MULTi 1080p WEB x264-AMB3R",
        )
        ids = [c["indexer_id"] for c in candidates]
        self.assertIn(2001, ids)
        self.assertEqual(candidates[0]["indexer_id"], 2001)

    @mock.patch("sickchill.oldbeard.ai.postprocess_matcher._load_alias_map")
    def test_alias_match_included_even_with_many_unrelated_shows(self, mock_alias_map):
        """A target whose English name ranks far down among many shows is still returned when its alias
        matches, because aliases are folded into the preliminary score (not gated behind a top-N
        name-only pre-filter as before)."""
        from sickchill.oldbeard.ai.postprocess_matcher import _get_candidate_shows

        decoys = [MockTVShow(name=f"Decoy Show Title {i}", indexerid=3000 + i, indexer=1) for i in range(40)]
        target = MockTVShow(name="Completely Unrelated English Title", indexerid=9999, indexer=1)
        self.mock_settings.show_list = decoys + [target]
        mock_alias_map.return_value = {9999: ["Zzz Romaji Alias Match"]}

        candidates = _get_candidate_shows("Zzz.Romaji.Alias.Match.E01.mkv", "Zzz Romaji Alias Match E01", limit=5)

        ids = [c["indexer_id"] for c in candidates]
        self.assertIn(9999, ids)


class TestLoadAliasMap(unittest.TestCase):
    """Test the bulk scene-exception alias loader used by candidate scoring."""

    def test_groups_rows_by_indexer_id_with_one_query(self):
        from sickchill.oldbeard.ai import postprocess_matcher

        rows = [
            {"indexer_id": 1, "show_name": "Alpha"},
            {"indexer_id": 1, "show_name": "Beta"},
            {"indexer_id": 2, "show_name": "Gamma"},
            {"indexer_id": "not-an-int", "show_name": "Skipped"},  # non-int id -> skipped
            {"indexer_id": 3, "show_name": ""},  # empty name -> skipped
        ]
        fake_db = mock.MagicMock()
        fake_db.select.return_value = rows

        with mock.patch("sickchill.oldbeard.db.DBConnection", return_value=fake_db):
            result = postprocess_matcher._load_alias_map()

        self.assertEqual(result, {1: ["Alpha", "Beta"], 2: ["Gamma"]})
        # One bulk query, not per-show.
        fake_db.select.assert_called_once()

    def test_returns_empty_on_error(self):
        from sickchill.oldbeard.ai import postprocess_matcher

        with mock.patch("sickchill.oldbeard.db.DBConnection", side_effect=Exception("db down")):
            self.assertEqual(postprocess_matcher._load_alias_map(), {})


class TestAnalyzeFile(unittest.TestCase):
    """Test the analyze_file function in postprocess_analyzer."""

    def setUp(self):
        """Set up test with mocked dependencies."""
        self.settings_patcher = mock.patch("sickchill.oldbeard.ai.postprocess_analyzer.settings")
        self.mock_settings = self.settings_patcher.start()

        self.mock_settings.AI_POSTPROCESS_ANALYZE_ENABLED = True
        self.mock_settings.AI_NOTIFY_ON_FALLBACK_FAILURE = False

        self.ai_available_patcher = mock.patch("sickchill.oldbeard.ai.postprocess_analyzer.is_ai_available")
        self.mock_ai_available = self.ai_available_patcher.start()
        self.mock_ai_available.return_value = True

        # Mock throttle with atomic reservation
        self.get_throttle_patcher = mock.patch("sickchill.oldbeard.ai.postprocess_analyzer.get_throttle")
        self.mock_get_throttle = self.get_throttle_patcher.start()
        self.mock_throttle = mock.MagicMock()
        self.mock_throttle.reserve_postprocess_attempt.return_value = True
        self.mock_get_throttle.return_value = self.mock_throttle

        self.get_client_patcher = mock.patch("sickchill.oldbeard.ai.postprocess_analyzer.get_client")
        self.mock_get_client = self.get_client_patcher.start()
        self.mock_client = mock.MagicMock()
        self.mock_get_client.return_value = self.mock_client

        # analyze() supports return_meta=True -> (response, was_cached). Adapt each test's
        # configured return_value (a bare dict, or an explicit (response, cached) tuple) to it.
        def _analyze_side_effect(*args, **kwargs):
            rv = self.mock_client.analyze.return_value
            if kwargs.get("return_meta"):
                return rv if isinstance(rv, tuple) else (rv, False)
            return rv[0] if isinstance(rv, tuple) else rv

        self.mock_client.analyze.side_effect = _analyze_side_effect

        # Mock template
        self.template_patcher = mock.patch("sickchill.oldbeard.ai.postprocess_analyzer._load_prompt_template")
        self.mock_template = self.template_patcher.start()
        self.mock_template.return_value = "Analysis template: {filename}"

        # Mock media info
        self.media_info_patcher = mock.patch("sickchill.oldbeard.ai.postprocess_analyzer._get_media_info")
        self.mock_media_info = self.media_info_patcher.start()
        self.mock_media_info.return_value = {
            "resolution": "1920x1080",
            "width": 1920,
            "height": 1080,
            "video_codec": "h264",
            "audio_codec": "aac",
            "duration": "45 minutes",
            "container": "MKV",
        }

    def tearDown(self):
        """Clean up patches."""
        self.settings_patcher.stop()
        self.ai_available_patcher.stop()
        self.get_throttle_patcher.stop()
        self.get_client_patcher.stop()
        self.template_patcher.stop()
        self.media_info_patcher.stop()

    def test_returns_none_when_disabled(self):
        """Test that None is returned when analysis is disabled."""
        from sickchill.oldbeard.ai.postprocess_analyzer import analyze_file

        self.mock_settings.AI_POSTPROCESS_ANALYZE_ENABLED = False

        show = MockTVShow()
        episode = MockTVEpisode(show=show)

        result = analyze_file("/path/to/file.mkv", episode, 4)

        self.assertIsNone(result)

    def test_returns_analysis_on_success(self):
        """Test that analysis result is returned on success."""
        from sickchill.oldbeard.ai.postprocess_analyzer import analyze_file

        self.mock_client.analyze.return_value = {
            "quality_verified": True,
            "quality_assessment": "Good 1080p encode",
            "issues": [],
            "proceed_with_processing": True,
            "confidence": 0.95,
            "notes": "",
        }

        show = MockTVShow()
        episode = MockTVEpisode(show=show)

        result = analyze_file("/path/to/file.mkv", episode, 4)

        self.assertIsNotNone(result)
        self.assertTrue(result["quality_verified"])
        self.assertTrue(result["proceed_with_processing"])

    def test_cache_hit_does_not_bill_budget(self):
        """A cached analysis still commits the cooldown but with count_budget=False."""
        from sickchill.oldbeard.ai.postprocess_analyzer import analyze_file

        # Explicit (response, was_cached=True) tuple simulates a response-cache hit.
        self.mock_client.analyze.return_value = (
            {
                "quality_verified": True,
                "quality_assessment": "cached",
                "issues": [],
                "proceed_with_processing": True,
                "confidence": 0.95,
                "notes": "",
            },
            True,
        )

        show = MockTVShow()
        episode = MockTVEpisode(show=show)

        result = analyze_file("/path/to/file.mkv", episode, 4)

        self.assertIsNotNone(result)
        self.mock_throttle.commit_postprocess_attempt.assert_called_once()
        self.assertFalse(self.mock_throttle.commit_postprocess_attempt.call_args.kwargs["count_budget"])

    def test_returns_issues_when_detected(self):
        """Test that issues are returned when detected."""
        from sickchill.oldbeard.ai.postprocess_analyzer import analyze_file

        self.mock_client.analyze.return_value = {
            "quality_verified": False,
            "quality_assessment": "Quality mismatch",
            "issues": ["File size too small", "Wrong resolution"],
            "proceed_with_processing": False,
            "confidence": 0.90,
            "notes": "Likely fake or corrupted",
        }

        show = MockTVShow()
        episode = MockTVEpisode(show=show)

        result = analyze_file("/path/to/file.mkv", episode, 4)

        self.assertIsNotNone(result)
        self.assertFalse(result["quality_verified"])
        self.assertEqual(len(result["issues"]), 2)


class TestShouldBlockProcessing(unittest.TestCase):
    """Test the should_block_processing function."""

    def setUp(self):
        """Set up test with mocked settings."""
        self.settings_patcher = mock.patch("sickchill.oldbeard.ai.postprocess_analyzer.settings")
        self.mock_settings = self.settings_patcher.start()

        self.mock_settings.AI_POSTPROCESS_DETECT_ISSUES = True
        self.mock_settings.AI_CONFIDENCE_THRESHOLD = 0.80

    def tearDown(self):
        """Clean up patches."""
        self.settings_patcher.stop()

    def test_should_not_block_when_none(self):
        """Test that processing is not blocked when no analysis."""
        from sickchill.oldbeard.ai.postprocess_analyzer import should_block_processing

        result = should_block_processing(None)

        self.assertFalse(result)

    def test_should_not_block_when_proceed_true(self):
        """Test that processing is not blocked when AI says proceed."""
        from sickchill.oldbeard.ai.postprocess_analyzer import should_block_processing

        analysis = {
            "proceed_with_processing": True,
            "confidence": 0.95,
        }

        result = should_block_processing(analysis)

        self.assertFalse(result)

    def test_should_block_when_proceed_false_high_confidence(self):
        """Test that processing is blocked when AI says don't proceed with high confidence."""
        from sickchill.oldbeard.ai.postprocess_analyzer import should_block_processing

        analysis = {
            "proceed_with_processing": False,
            "confidence": 0.95,
        }

        result = should_block_processing(analysis)

        self.assertTrue(result)

    def test_should_not_block_when_proceed_false_low_confidence(self):
        """Test that processing is not blocked when AI says don't proceed but low confidence."""
        from sickchill.oldbeard.ai.postprocess_analyzer import should_block_processing

        analysis = {
            "proceed_with_processing": False,
            "confidence": 0.50,
        }

        result = should_block_processing(analysis)

        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
