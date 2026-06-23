"""
Tests for AI Phase 4 features: Enhancement.

Tests cost tracking, caching, per-show preferences, feedback loop, and batch processing.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

# ============================================================================
# Cost Tracker Tests
# ============================================================================


class TestCostTracker:
    """Tests for the CostTracker class."""

    @pytest.fixture
    def mock_db(self):
        """Create a mock database connection."""
        with patch("sickchill.oldbeard.ai.cost_tracker.db") as mock_db:
            mock_conn = MagicMock()
            mock_db.DBConnection.return_value = mock_conn
            mock_conn.has_table.return_value = True
            mock_conn.select.return_value = []
            yield mock_conn

    @pytest.fixture
    def cost_tracker(self, mock_db):
        """Create a CostTracker instance."""
        from sickchill.oldbeard.ai.cost_tracker import CostTracker

        return CostTracker()

    def test_record_usage(self, cost_tracker, mock_db):
        """Test recording API usage."""
        record = cost_tracker.record_usage(
            model="claude-sonnet-4-20250514",
            context="search",
            input_tokens=500,
            output_tokens=100,
            scope_key="12345",
        )

        assert record.model == "claude-sonnet-4-20250514"
        assert record.context == "search"
        assert record.input_tokens == 500
        assert record.output_tokens == 100
        assert record.estimated_cost_usd > 0
        mock_db.action.assert_called()

    def test_estimated_cost_calculation(self, cost_tracker, mock_db):
        """Test cost calculation for different models."""
        # Sonnet pricing: $3/M input, $15/M output
        sonnet_record = cost_tracker.record_usage(
            model="claude-sonnet-4-20250514",
            context="search",
            input_tokens=1_000_000,  # 1M input tokens
            output_tokens=0,
        )
        assert abs(sonnet_record.estimated_cost_usd - 3.0) < 0.01

        # Haiku 3.5 pricing (corrected): $0.80/M input, $4.00/M output
        haiku_record = cost_tracker.record_usage(
            model="claude-haiku-3-5-20241022",
            context="search",
            input_tokens=1_000_000,
            output_tokens=0,
        )
        assert abs(haiku_record.estimated_cost_usd - 0.80) < 0.01

    def test_estimated_cost_family_prefix_lookup(self, cost_tracker, mock_db):
        """Full/CLI-reported model ids resolve via longest-prefix family pricing."""

        def cost(model):
            return cost_tracker.record_usage(model=model, context="search", input_tokens=1_000_000, output_tokens=0).estimated_cost_usd

        # Opus 4.8 -> $5/M (longest prefix wins over the $15/M claude-opus-4 entry)
        assert abs(cost("claude-opus-4-8-20251101") - 5.00) < 0.01
        # Opus 4.1 -> $15/M (only the shorter claude-opus-4 prefix matches)
        assert abs(cost("claude-opus-4-1-20250805") - 15.00) < 0.01
        # Haiku 4.5 -> $1/M
        assert abs(cost("claude-haiku-4-5-20251001") - 1.00) < 0.01
        # Fable/Mythos -> $10/M (CLI can pass these even though not in the API dropdown)
        assert abs(cost("claude-fable-5") - 10.00) < 0.01
        assert abs(cost("claude-mythos-5") - 10.00) < 0.01
        # Truly unknown -> DEFAULT ($3/M)
        assert abs(cost("some-other-model") - 3.00) < 0.01

    def test_get_usage_summary(self, cost_tracker, mock_db):
        """Test getting usage summary."""
        mock_db.select.return_value = [
            {
                "input_tokens": 500,
                "output_tokens": 100,
                "estimated_cost_usd": 0.003,
                "context": "search",
                "model": "claude-sonnet-4-20250514",
            },
            {
                "input_tokens": 300,
                "output_tokens": 50,
                "estimated_cost_usd": 0.002,
                "context": "postprocess",
                "model": "claude-sonnet-4-20250514",
            },
        ]

        summary = cost_tracker.get_usage_summary(period="day")

        assert summary.total_requests == 2
        assert summary.total_input_tokens == 800
        assert summary.total_output_tokens == 150
        assert summary.total_estimated_cost_usd == 0.005
        assert "search" in summary.by_context
        assert "postprocess" in summary.by_context

    def test_get_recent_usage(self, cost_tracker, mock_db):
        """Test getting recent usage records."""
        mock_db.select.return_value = [
            {
                "timestamp": time.time(),
                "model": "claude-sonnet-4-20250514",
                "context": "search",
                "input_tokens": 500,
                "output_tokens": 100,
                "estimated_cost_usd": 0.003,
                "scope_key": "12345",
            }
        ]

        records = cost_tracker.get_recent_usage(limit=10)

        assert len(records) == 1
        assert records[0].context == "search"

    def test_format_summary(self, cost_tracker, mock_db):
        """Test summary formatting."""
        from sickchill.oldbeard.ai.cost_tracker import UsageSummary

        summary = UsageSummary(
            period="day",
            total_requests=10,
            total_input_tokens=5000,
            total_output_tokens=1000,
            total_estimated_cost_usd=0.05,
            by_context={"search": {"requests": 5, "estimated_cost_usd": 0.03}},
            by_model={"claude-sonnet-4-20250514": {"requests": 10, "estimated_cost_usd": 0.05}},
        )

        formatted = cost_tracker.format_summary(summary)

        assert "AI Usage Summary" in formatted
        assert "10" in formatted
        assert "$0.05" in formatted


# ============================================================================
# Show Preferences Tests
# ============================================================================


class TestShowPreferences:
    """Tests for per-show AI preferences."""

    @pytest.fixture
    def mock_db(self):
        """Create a mock database connection."""
        with patch("sickchill.oldbeard.ai.show_preferences.db") as mock_db:
            mock_conn = MagicMock()
            mock_db.DBConnection.return_value = mock_conn
            mock_conn.has_table.return_value = True
            mock_conn.select.return_value = []
            yield mock_conn

    @pytest.fixture
    def mock_settings(self):
        """Mock settings."""
        with patch("sickchill.oldbeard.ai.show_preferences.settings") as mock:
            mock.AI_ENABLED = True
            mock.AI_SEARCH_ENABLED = True
            mock.AI_POSTPROCESS_MATCH_ENABLED = True
            mock.AI_SEARCH_ONLY_ON_FAILURE = True
            mock.AI_POSTPROCESS_MATCH_ONLY_ON_FAILURE = True
            mock.AI_CONFIDENCE_THRESHOLD = 0.8
            mock.AI_SEARCH_COOLDOWN_DAYS_PER_SHOW = 7
            yield mock

    @pytest.fixture
    def prefs_manager(self, mock_db, mock_settings):
        """Create a ShowPreferencesManager instance."""
        from sickchill.oldbeard.ai.show_preferences import ShowPreferencesManager

        return ShowPreferencesManager()

    def test_get_default_preferences(self, prefs_manager, mock_db):
        """Test getting default preferences for a show."""
        mock_db.select.return_value = []  # No stored prefs

        prefs = prefs_manager.get_preferences(12345)

        assert prefs.ai_enabled is None
        assert prefs.ai_search_always is False
        assert prefs.ai_search_disabled is False

    def test_set_preferences(self, prefs_manager, mock_db):
        """Test setting preferences for a show."""
        from sickchill.oldbeard.ai.show_preferences import ShowAIPreferences

        prefs = ShowAIPreferences(
            ai_enabled=True,
            ai_search_always=True,
        )

        prefs_manager.set_preferences(12345, prefs)

        mock_db.action.assert_called()
        # Verify cache is updated
        cached = prefs_manager.get_preferences(12345)
        assert cached.ai_search_always is True

    def test_is_ai_enabled_for_show(self, prefs_manager, mock_db, mock_settings):
        """Test checking if AI is enabled for a show."""
        mock_show = MagicMock()
        mock_show.indexerid = 12345

        # Default: should follow global setting
        assert prefs_manager.is_ai_enabled_for_show(mock_show) is True

        # Global disabled
        mock_settings.AI_ENABLED = False
        assert prefs_manager.is_ai_enabled_for_show(mock_show) is False

    def test_should_use_ai_search_with_preference(self, prefs_manager, mock_db, mock_settings):
        """Test AI search decision with per-show preference."""
        from sickchill.oldbeard.ai.show_preferences import ShowAIPreferences

        mock_show = MagicMock()
        mock_show.indexerid = 12345

        # Set preference to always use AI
        prefs = ShowAIPreferences(ai_search_always=True)
        prefs_manager.set_preferences(12345, prefs)

        # Should use AI even when has_result is True
        assert prefs_manager.should_use_ai_search(mock_show, has_result=True) is True

    def test_should_use_ai_search_disabled(self, prefs_manager, mock_db, mock_settings):
        """Test AI search is skipped when disabled for show."""
        from sickchill.oldbeard.ai.show_preferences import ShowAIPreferences

        mock_show = MagicMock()
        mock_show.indexerid = 12345

        # Disable AI search for this show
        prefs = ShowAIPreferences(ai_search_disabled=True)
        prefs_manager.set_preferences(12345, prefs)

        assert prefs_manager.should_use_ai_search(mock_show, has_result=False) is False

    def test_custom_confidence_threshold(self, prefs_manager, mock_db, mock_settings):
        """Test custom confidence threshold per show."""
        from sickchill.oldbeard.ai.show_preferences import ShowAIPreferences

        mock_show = MagicMock()
        mock_show.indexerid = 12345

        # Default: use global
        assert prefs_manager.get_confidence_threshold(mock_show) == 0.8

        # Custom threshold
        prefs = ShowAIPreferences(ai_confidence_threshold=0.9)
        prefs_manager.set_preferences(12345, prefs)

        assert prefs_manager.get_confidence_threshold(mock_show) == 0.9


# ============================================================================
# Feedback Tests
# ============================================================================


class TestFeedback:
    """Tests for the AI feedback system."""

    @pytest.fixture
    def mock_db(self):
        """Create a mock database connection."""
        with patch("sickchill.oldbeard.ai.feedback.db") as mock_db:
            mock_conn = MagicMock()
            mock_db.DBConnection.return_value = mock_conn
            mock_conn.has_table.return_value = True
            mock_conn.select.return_value = []
            yield mock_conn

    @pytest.fixture
    def feedback_manager(self, mock_db):
        """Create a FeedbackManager instance."""
        from sickchill.oldbeard.ai.feedback import FeedbackManager

        return FeedbackManager()

    def test_record_decision(self, feedback_manager, mock_db):
        """Test recording an AI decision."""
        from sickchill.oldbeard.ai.feedback import DecisionType

        decision_id = feedback_manager.record_decision(
            decision_type=DecisionType.SEARCH_SELECTION,
            input_summary="10 results for Show X S01E05",
            output_summary="Selected #3: Release.Name",
            confidence=0.85,
            reasoning="Good seeder count and quality",
            raw_response={"selected_index": 3, "confidence": 0.85},
            show_id=12345,
        )

        assert decision_id is not None
        assert len(decision_id) == 16
        mock_db.action.assert_called()

    def test_record_feedback(self, feedback_manager, mock_db):
        """Test recording user feedback on a decision."""
        from sickchill.oldbeard.ai.feedback import DecisionType, FeedbackType

        # First record a decision
        decision_id = feedback_manager.record_decision(
            decision_type=DecisionType.SEARCH_SELECTION,
            input_summary="Test",
            output_summary="Test",
            confidence=0.8,
            reasoning="Test",
            raw_response={},
        )

        # Mock that decision exists
        mock_db.select.return_value = [{"decision_id": decision_id}]

        # Record feedback
        result = feedback_manager.record_feedback(
            decision_id=decision_id,
            feedback_type=FeedbackType.CORRECT,
            notes="AI made the right choice",
        )

        assert result is True

    def test_record_feedback_invalid_decision(self, feedback_manager, mock_db):
        """Test recording feedback for non-existent decision."""
        from sickchill.oldbeard.ai.feedback import FeedbackType

        mock_db.select.return_value = []  # Decision not found

        result = feedback_manager.record_feedback(
            decision_id="nonexistent",
            feedback_type=FeedbackType.INCORRECT,
        )

        assert result is False

    def test_get_feedback_stats(self, feedback_manager, mock_db):
        """Test getting feedback statistics."""
        # Mock decision count
        mock_db.select.side_effect = [
            [{"count": 100}],  # Total decisions
            [
                {"feedback_type": "correct", "count": 80},
                {"feedback_type": "incorrect", "count": 10},
                {"feedback_type": "partially_correct", "count": 10},
            ],  # Feedback counts
        ]

        stats = feedback_manager.get_feedback_stats(days=30)

        assert stats["total_decisions"] == 100
        assert stats["correct"] == 80
        assert stats["incorrect"] == 10
        assert stats["partially_correct"] == 10
        # Accuracy: (80 + 10*0.5) / 100 = 0.85
        assert abs(stats["accuracy"] - 0.85) < 0.01


# ============================================================================
# Batch Processor Tests
# ============================================================================


class TestBatchProcessor:
    """Tests for the batch processor."""

    @pytest.fixture
    def batch_processor(self):
        """Create a BatchProcessor instance."""
        from sickchill.oldbeard.ai.batch import BatchProcessor

        return BatchProcessor(rate_limit_per_minute=60, max_concurrent=1)

    def test_create_job(self, batch_processor):
        """Test creating a batch job."""
        items = [
            {"type": "search", "show_id": 1},
            {"type": "search", "show_id": 2},
            {"type": "search", "show_id": 3},
        ]

        job_id = batch_processor.create_job(items)

        assert job_id is not None
        status = batch_processor.get_job_status(job_id)
        assert status["status"] == "pending"
        assert status["total_items"] == 3

    def test_get_job_status(self, batch_processor):
        """Test getting job status."""
        items = [{"data": "test"}]
        job_id = batch_processor.create_job(items)

        status = batch_processor.get_job_status(job_id)

        assert status is not None
        assert status["job_id"] == job_id
        assert status["status"] == "pending"
        assert status["total_items"] == 1
        assert status["progress"] == 0

    def test_get_nonexistent_job(self, batch_processor):
        """Test getting status of non-existent job."""
        status = batch_processor.get_job_status("nonexistent")
        assert status is None

    def test_start_job(self, batch_processor):
        """Test starting a batch job."""
        items = [{"value": 1}]
        job_id = batch_processor.create_job(items)

        def processor(item):
            return {"processed": item["value"] * 2}

        result = batch_processor.start_job(job_id, processor)

        assert result is True

        # Wait for processing
        time.sleep(0.2)

        status = batch_processor.get_job_status(job_id)
        assert status["status"] in ("running", "completed")

    def test_job_results(self, batch_processor):
        """Test getting job results."""
        items = [{"value": 1}, {"value": 2}]
        job_id = batch_processor.create_job(items)

        def processor(item):
            return {"result": item["value"] * 2}

        batch_processor.start_job(job_id, processor)

        # Wait for processing
        time.sleep(0.3)

        results = batch_processor.get_job_results(job_id)

        assert results is not None
        assert len(results) == 2

    def test_cancel_job(self, batch_processor):
        """Test cancelling a job."""
        items = [{"value": i} for i in range(100)]  # Many items
        job_id = batch_processor.create_job(items)

        def slow_processor(item):
            time.sleep(0.1)
            return {"result": item["value"]}

        batch_processor.start_job(job_id, slow_processor)
        time.sleep(0.05)  # Let it start

        result = batch_processor.cancel_job(job_id)

        assert result is True

    def test_cleanup_old_jobs(self, batch_processor):
        """Test cleaning up old jobs."""
        # Create a job
        items = [{"value": 1}]
        job_id = batch_processor.create_job(items)

        # Manually mark as completed and old
        with batch_processor._lock:
            job = batch_processor._jobs[job_id]
            job.status = batch_processor._jobs[job_id].status.__class__.COMPLETED
            job.created_at = time.time() - (25 * 3600)  # 25 hours ago

        removed = batch_processor.cleanup_old_jobs(max_age_hours=24)

        assert removed == 1
        assert batch_processor.get_job_status(job_id) is None


# ============================================================================
# Response Caching Tests
# ============================================================================


class TestResponseCaching:
    """Tests for response caching in the client."""

    @pytest.fixture
    def mock_throttle(self):
        """Mock the throttle manager."""
        with patch("sickchill.oldbeard.ai.get_throttle") as mock:
            throttle = MagicMock()
            mock.return_value = throttle
            throttle.get_cached_response.return_value = None
            yield throttle

    @pytest.fixture
    def client(self):
        """Create an AnthropicClient instance."""
        from sickchill.oldbeard.ai.anthropic_client import AnthropicClient

        return AnthropicClient(api_key="test-key")

    def test_cache_check_on_analyze(self, client, mock_throttle):
        """Test that cache is checked before making API request."""
        # Return cached response
        mock_throttle.get_cached_response.return_value = {
            "selected_index": 0,
            "confidence": 0.9,
        }

        with patch.object(client, "_make_request") as mock_request:
            result = client.analyze(
                prompt="Test prompt",
                use_cache=True,
                cost_context="search",
            )

            # Should not call _make_request if cache hit
            mock_request.assert_not_called()
            assert result["selected_index"] == 0

    def test_cache_store_after_request(self, client, mock_throttle):
        """Test that response is cached after successful request."""
        mock_throttle.get_cached_response.return_value = None

        with patch.object(client, "_make_request") as mock_request:
            mock_request.return_value = (
                {"selected_index": 0, "confidence": 0.9},
                None,  # No usage info
            )

            result = client.analyze(
                prompt="Test prompt",
                use_cache=True,
                cost_context="search",
                cost_scope_key="12345",
            )

            # Should store in cache
            mock_throttle.cache_response.assert_called_once()
            assert result["selected_index"] == 0

    def test_cache_disabled(self, client, mock_throttle):
        """Test that cache can be disabled."""
        with patch.object(client, "_make_request") as mock_request:
            mock_request.return_value = (
                {"selected_index": 0},
                None,
            )

            client.analyze(
                prompt="Test prompt",
                use_cache=False,
            )

            # Should not check or store cache
            mock_throttle.get_cached_response.assert_not_called()
            mock_throttle.cache_response.assert_not_called()


# ============================================================================
# Integration Tests
# ============================================================================


class TestPhase4Integration:
    """Integration tests for Phase 4 features."""

    @pytest.fixture
    def mock_settings(self):
        """Mock all required settings."""
        with patch("sickchill.settings") as mock:
            mock.AI_ENABLED = True
            mock.AI_SEARCH_ENABLED = True
            mock.AI_POSTPROCESS_MATCH_ENABLED = True
            mock.AI_MAX_CALLS_PER_HOUR = 20
            mock.AI_MAX_CALLS_PER_DAY = 200
            mock.AI_SEARCH_COOLDOWN_DAYS_PER_SHOW = 7
            mock.AI_SEARCH_ONLY_ON_FAILURE = True
            mock.AI_POSTPROCESS_MATCH_ONLY_ON_FAILURE = True
            mock.AI_CONFIDENCE_THRESHOLD = 0.8
            yield mock

    def test_module_exports(self):
        """Test that all Phase 4 modules are exported."""
        from sickchill.oldbeard import ai

        assert hasattr(ai, "get_cost_tracker")
        assert hasattr(ai, "get_preferences_manager")
        assert hasattr(ai, "get_feedback_manager")
        assert hasattr(ai, "get_batch_processor")

    def test_cost_tracker_dataclasses(self):
        """Test cost tracker dataclasses."""
        from sickchill.oldbeard.ai.cost_tracker import UsageRecord, UsageSummary

        record = UsageRecord(
            timestamp=time.time(),
            model="test",
            context="search",
            input_tokens=100,
            output_tokens=50,
            estimated_cost_usd=0.001,
        )
        assert record.model == "test"

        summary = UsageSummary(
            period="day",
            total_requests=10,
            total_input_tokens=1000,
            total_output_tokens=500,
            total_estimated_cost_usd=0.01,
            by_context={},
            by_model={},
        )
        assert summary.total_requests == 10

    def test_feedback_enums(self):
        """Test feedback enums."""
        from sickchill.oldbeard.ai.feedback import DecisionType, FeedbackType

        assert DecisionType.SEARCH_SELECTION.value == "search_selection"
        assert DecisionType.FILE_MATCH.value == "file_match"
        assert FeedbackType.CORRECT.value == "correct"
        assert FeedbackType.INCORRECT.value == "incorrect"

    def test_batch_status_enum(self):
        """Test batch status enum."""
        from sickchill.oldbeard.ai.batch import BatchStatus

        assert BatchStatus.PENDING.value == "pending"
        assert BatchStatus.COMPLETED.value == "completed"
        assert BatchStatus.FAILED.value == "failed"

    def test_preferences_dataclass(self):
        """Test preferences dataclass."""
        from sickchill.oldbeard.ai.show_preferences import ShowAIPreferences

        prefs = ShowAIPreferences(
            ai_enabled=True,
            ai_search_always=True,
            ai_confidence_threshold=0.9,
        )

        data = prefs.to_dict()
        assert data["ai_enabled"] is True
        assert data["ai_search_always"] is True
        assert data["ai_confidence_threshold"] == 0.9

        restored = ShowAIPreferences.from_dict(data)
        assert restored.ai_enabled is True
        assert restored.ai_search_always is True
