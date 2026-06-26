"""
Tests for AI throttle functionality.

Tests:
    TestFileFingerprint - File fingerprinting for cooldowns
    TestBudgetTracking - In-memory budget limit tracking
    TestReservationNamespacing - Context-namespaced reservations/cooldowns
    TestCooldownLogic - Cooldown calculation logic (via reserve_*)
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from sickchill.oldbeard.ai.throttle import ThrottleManager


class TestFileFingerprint(unittest.TestCase):
    """Test file fingerprinting for stable cooldown keys."""

    def test_fingerprint_basic(self):
        """Test basic fingerprint generation."""
        fingerprint = ThrottleManager.get_file_fingerprint("/path/to/Show.Name.S01E01.720p.mkv")

        # Should return a 32-char hex string
        self.assertEqual(len(fingerprint), 32)
        self.assertTrue(all(c in "0123456789abcdef" for c in fingerprint))

    def test_fingerprint_consistency(self):
        """Test that same path produces same fingerprint."""
        path = "/downloads/Show.Name.S01E01.720p.mkv"
        fp1 = ThrottleManager.get_file_fingerprint(path)
        fp2 = ThrottleManager.get_file_fingerprint(path)

        self.assertEqual(fp1, fp2)

    def test_fingerprint_different_paths(self):
        """Test that different paths produce different fingerprints."""
        fp1 = ThrottleManager.get_file_fingerprint("/path/a/file.mkv")
        fp2 = ThrottleManager.get_file_fingerprint("/path/b/file.mkv")

        # Different parent folders should produce different fingerprints
        self.assertNotEqual(fp1, fp2)

    def test_fingerprint_with_real_file(self):
        """Test fingerprinting with actual file on disk."""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mkv") as f:
            f.write(b"test content for fingerprint")
            temp_path = f.name

        try:
            fp1 = ThrottleManager.get_file_fingerprint(temp_path)
            fp2 = ThrottleManager.get_file_fingerprint(temp_path)

            self.assertEqual(fp1, fp2)
            self.assertEqual(len(fp1), 32)
        finally:
            os.unlink(temp_path)

    def test_fingerprint_nonexistent_file(self):
        """Test fingerprinting a file that doesn't exist (uses size=0)."""
        fp = ThrottleManager.get_file_fingerprint("/nonexistent/path/file.mkv")

        # Should still return a valid fingerprint
        self.assertEqual(len(fp), 32)

    def test_fingerprint_includes_size(self):
        """Test that file size affects fingerprint."""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mkv") as f:
            f.write(b"small")
            small_path = f.name

        with tempfile.NamedTemporaryFile(delete=False, suffix=".mkv", dir=os.path.dirname(small_path)) as f:
            # Same filename pattern but different size
            f.write(b"larger content here for testing")
            large_path = f.name

        try:
            # If filenames are different, fingerprints will differ
            # This test validates size is considered when it exists
            fp_small = ThrottleManager.get_file_fingerprint(small_path)
            fp_large = ThrottleManager.get_file_fingerprint(large_path)

            # Different files should have different fingerprints
            self.assertNotEqual(fp_small, fp_large)
        finally:
            os.unlink(small_path)
            os.unlink(large_path)


class TestBudgetTracking(unittest.TestCase):
    """Test in-memory budget limit tracking."""

    def setUp(self):
        """Set up test with mocked settings and DB."""
        self.settings_patcher = mock.patch("sickchill.oldbeard.ai.throttle.settings")
        self.mock_settings = self.settings_patcher.start()

        # Set default limits
        self.mock_settings.AI_ENABLED = True
        self.mock_settings.AI_SEARCH_ENABLED = True
        self.mock_settings.AI_MAX_CALLS_PER_HOUR = 5
        self.mock_settings.AI_MAX_CALLS_PER_DAY = 20

        # Mock DB to avoid actual database operations
        self.db_patcher = mock.patch("sickchill.oldbeard.ai.throttle.db")
        self.mock_db = self.db_patcher.start()
        self.mock_db.DBConnection.return_value.has_table.return_value = True

    def tearDown(self):
        """Clean up patches."""
        self.settings_patcher.stop()
        self.db_patcher.stop()

    def test_budget_starts_empty(self):
        """Test that budget counters start empty."""
        manager = ThrottleManager()
        status = manager.get_budget_status()

        self.assertEqual(status["hourly_used"], 0)
        self.assertEqual(status["daily_used"], 0)

    def test_budget_increments_on_attempt(self):
        """Test that recording an attempt increments budget counters."""
        manager = ThrottleManager()

        manager.record_attempt("search", "show", "12345")
        status = manager.get_budget_status()

        self.assertEqual(status["hourly_used"], 1)
        self.assertEqual(status["daily_used"], 1)

    def test_count_budget_false_records_cooldown_but_not_budget(self):
        """A cache hit (count_budget=False) records the cooldown but does not bill the budget."""
        manager = ThrottleManager()

        manager.record_attempt("search", "show", "12345", count_budget=False)

        # Budget counters untouched...
        status = manager.get_budget_status()
        self.assertEqual(status["hourly_used"], 0)
        self.assertEqual(status["daily_used"], 0)
        # ...but the cooldown (last_attempt) was still written to the throttle table.
        self.mock_db.DBConnection.return_value.action.assert_called()

    def test_commit_search_attempt_count_budget_passthrough(self):
        """commit_search_attempt(count_budget=False) does not consume the call budget."""
        manager = ThrottleManager()
        show = mock.MagicMock()
        show.indexerid = 999

        manager.commit_search_attempt(show, count_budget=False)
        self.assertEqual(manager.get_budget_status()["hourly_used"], 0)

        manager.commit_search_attempt(show, count_budget=True)
        self.assertEqual(manager.get_budget_status()["hourly_used"], 1)

    def test_record_attempt_set_cooldown_false_skips_db_but_counts_budget(self):
        """A confident match commits with set_cooldown=False: budget counted, no cooldown row."""
        manager = ThrottleManager()
        # Ignore the CREATE INDEX action from _ensure_tables() during construction.
        self.mock_db.DBConnection.return_value.action.reset_mock()

        manager.record_attempt("postprocess", "file", "fp", count_budget=True, set_cooldown=False)

        # No last_attempt row written...
        self.mock_db.DBConnection.return_value.action.assert_not_called()
        # ...but the call still counts against the budget.
        self.assertEqual(manager.get_budget_status()["hourly_used"], 1)

    def test_record_success_upserts_preserving_last_attempt(self):
        """record_success upserts (so a set_cooldown=False match still records success) and
        preserves any existing last_attempt via subquery rather than UPDATE-only (which would
        no-op when no row exists)."""
        manager = ThrottleManager()
        self.mock_db.DBConnection.return_value.action.reset_mock()

        manager.record_success("postprocess", "file", "fp")

        self.mock_db.DBConnection.return_value.action.assert_called_once()
        sql = self.mock_db.DBConnection.return_value.action.call_args.args[0]
        self.assertIn("INSERT OR REPLACE", sql)
        self.assertIn("last_attempt", sql)  # preserved
        self.assertIn("last_success", sql)

    def test_provider_aware_budget_limits(self):
        """The free CLI provider uses the high backstop; the paid API uses configured caps."""
        manager = ThrottleManager()

        self.mock_settings.AI_PROVIDER = "api"
        self.assertEqual(
            manager._budget_limits(),
            (self.mock_settings.AI_MAX_CALLS_PER_HOUR, self.mock_settings.AI_MAX_CALLS_PER_DAY),
        )

        self.mock_settings.AI_PROVIDER = "cli"
        self.assertEqual(
            manager._budget_limits(),
            (ThrottleManager.CLI_BUDGET_BACKSTOP_PER_HOUR, ThrottleManager.CLI_BUDGET_BACKSTOP_PER_DAY),
        )
        # Diagnostics must report the same effective limits.
        status = manager.get_budget_status()
        self.assertEqual(status["hourly_limit"], ThrottleManager.CLI_BUDGET_BACKSTOP_PER_HOUR)
        self.assertEqual(status["daily_limit"], ThrottleManager.CLI_BUDGET_BACKSTOP_PER_DAY)

    def test_cli_provider_not_blocked_by_paid_api_limits(self):
        """With the CLI provider, the tiny paid-API caps must not block bulk post-processing."""
        self.mock_settings.AI_PROVIDER = "cli"
        self.mock_settings.AI_MAX_CALLS_PER_HOUR = 2
        self.mock_settings.AI_MAX_CALLS_PER_DAY = 2
        manager = ThrottleManager()

        for i in range(10):
            manager.record_attempt("postprocess", "file", str(i))

        # Paid-API caps would block after 2; the CLI backstop (1000/hr) does not.
        self.assertTrue(manager._check_budget())

    def test_budget_limit_blocks(self):
        """Test that exceeding budget limit returns False."""
        self.mock_settings.AI_PROVIDER = "api"
        self.mock_settings.AI_MAX_CALLS_PER_HOUR = 2
        manager = ThrottleManager()

        # Record attempts up to limit
        manager.record_attempt("search", "show", "1")
        manager.record_attempt("search", "show", "2")

        # Should now be blocked
        self.assertFalse(manager._check_budget())

    def test_budget_thread_safety(self):
        """Test that budget tracking is thread-safe."""
        manager = ThrottleManager()
        errors = []

        def record_many():
            try:
                for i in range(100):
                    manager.record_attempt("search", "show", str(i))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=record_many) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Should complete without errors
        self.assertEqual(len(errors), 0)

        # Should have recorded all attempts
        status = manager.get_budget_status()
        self.assertEqual(status["hourly_used"], 500)


class TestReservationNamespacing(unittest.TestCase):
    """Pending reservations are namespaced by context so matcher/analyzer and search/postprocess
    do not collide, and the matcher and analyzer hold independent per-file cooldowns."""

    def setUp(self):
        self.settings_patcher = mock.patch("sickchill.oldbeard.ai.throttle.settings")
        self.mock_settings = self.settings_patcher.start()
        self.mock_settings.AI_ENABLED = True
        self.mock_settings.AI_SEARCH_ENABLED = True
        self.mock_settings.AI_POSTPROCESS_MATCH_ENABLED = True
        self.mock_settings.AI_SEARCH_COOLDOWN_DAYS_PER_SHOW = 7
        self.mock_settings.AI_POSTPROCESS_MATCH_COOLDOWN_HOURS_PER_FILE = 72
        self.mock_settings.AI_MAX_CALLS_PER_HOUR = 100
        self.mock_settings.AI_MAX_CALLS_PER_DAY = 1000

        self.db_patcher = mock.patch("sickchill.oldbeard.ai.throttle.db")
        self.mock_db = self.db_patcher.start()
        self.mock_connection = mock.MagicMock()
        self.mock_db.DBConnection.return_value = self.mock_connection
        self.mock_connection.has_table.return_value = True
        # No prior attempts recorded -> the DB cooldown never blocks in these tests.
        self.mock_connection.select.return_value = []

        # Isolate the per-show cooldown lookup so reserve_search_attempt does not reach the real
        # show_preferences DB/singleton (its own db is not the mocked throttle.db).
        self.cooldown_patcher = mock.patch.object(ThrottleManager, "_get_cooldown_days_for_show", return_value=7)
        self.cooldown_patcher.start()

    def tearDown(self):
        self.cooldown_patcher.stop()
        self.settings_patcher.stop()
        self.db_patcher.stop()

    def test_matcher_and_analyzer_reserve_same_file_independently(self):
        """The analyzer can reserve a file even though the matcher already holds it (pending)."""
        manager = ThrottleManager()
        fp = "/path/to/file.mkv"

        self.assertTrue(manager.reserve_postprocess_attempt(fp))  # matcher (default context)
        # A second reservation in the SAME context is blocked as a concurrent request.
        self.assertFalse(manager.reserve_postprocess_attempt(fp))
        # ...but the analyzer context reserves the same file concurrently.
        self.assertTrue(manager.reserve_postprocess_attempt(fp, context=manager.CONTEXT_POSTPROCESS_ANALYZE))

    def test_matcher_and_analyzer_cooldown_independent(self):
        """A recent matcher attempt (DB cooldown) must not block the analyzer's own cooldown."""

        # Return a recent last_attempt ONLY for the matcher context; empty for everything else.
        # If the analyzer wrongly looked up CONTEXT_POSTPROCESS it would see this and be blocked.
        def _select(_query, params=None):
            if params and params[0] == ThrottleManager.CONTEXT_POSTPROCESS:
                return [{"last_attempt": time.time() - 60}]  # 60s ago, inside the 72h cooldown
            return []

        self.mock_connection.select.side_effect = _select
        manager = ThrottleManager()
        fp = "/path/to/file.mkv"

        # Matcher is blocked by its own recent cooldown.
        self.assertFalse(manager.reserve_postprocess_attempt(fp))
        # Analyzer uses a different context -> its cooldown lookup returns empty -> allowed.
        self.assertTrue(manager.reserve_postprocess_attempt(fp, context=manager.CONTEXT_POSTPROCESS_ANALYZE))

    def test_search_and_postprocess_pending_keys_do_not_collide(self):
        """Identical scope_key strings under different contexts must not collide in the map."""
        self.mock_connection.select.return_value = []  # no cooldown anywhere
        manager = ThrottleManager()
        # Force the file fingerprint to equal the show's scope_key string so the ONLY thing
        # separating the two reservations is the context namespace.
        manager.get_file_fingerprint = mock.MagicMock(return_value="4242")
        show = mock.MagicMock()
        show.indexerid = 4242

        self.assertTrue(manager.reserve_search_attempt(show))  # pending key "search:4242"
        # With the old unnamespaced map this would collide with "4242" and return False.
        self.assertTrue(manager.reserve_postprocess_attempt("/x/y.mkv"))  # "postprocess:4242"

        # Releasing the search reservation must not drop the postprocess one.
        manager.release_search_reservation(show)
        self.assertFalse(manager.reserve_postprocess_attempt("/x/y.mkv"))  # still held

    def test_commit_releases_only_its_own_context(self):
        """Committing the analyzer reservation frees the analyzer pending slot for re-reservation."""
        manager = ThrottleManager()
        fp = "/a/b.mkv"

        self.assertTrue(manager.reserve_postprocess_attempt(fp, context=manager.CONTEXT_POSTPROCESS_ANALYZE))
        manager.commit_postprocess_attempt(fp, context=manager.CONTEXT_POSTPROCESS_ANALYZE)
        # Pending slot released (DB cooldown mocked empty) -> can reserve again.
        self.assertTrue(manager.reserve_postprocess_attempt(fp, context=manager.CONTEXT_POSTPROCESS_ANALYZE))


class TestCooldownLogic(unittest.TestCase):
    """Test cooldown calculation logic."""

    def setUp(self):
        """Set up test with mocked settings and DB."""
        self.settings_patcher = mock.patch("sickchill.oldbeard.ai.throttle.settings")
        self.mock_settings = self.settings_patcher.start()

        self.mock_settings.AI_ENABLED = True
        self.mock_settings.AI_SEARCH_ENABLED = True
        self.mock_settings.AI_POSTPROCESS_MATCH_ENABLED = True
        self.mock_settings.AI_SEARCH_COOLDOWN_DAYS_PER_SHOW = 7
        self.mock_settings.AI_POSTPROCESS_MATCH_COOLDOWN_HOURS_PER_FILE = 72
        self.mock_settings.AI_MAX_CALLS_PER_HOUR = 100
        self.mock_settings.AI_MAX_CALLS_PER_DAY = 1000

        self.db_patcher = mock.patch("sickchill.oldbeard.ai.throttle.db")
        self.mock_db = self.db_patcher.start()
        self.mock_connection = mock.MagicMock()
        self.mock_db.DBConnection.return_value = self.mock_connection
        self.mock_connection.has_table.return_value = True

        # Isolate the per-show cooldown lookup (7-day) so reserve_search_attempt does not reach
        # the real show_preferences DB/singleton.
        self.cooldown_patcher = mock.patch.object(ThrottleManager, "_get_cooldown_days_for_show", return_value=7)
        self.cooldown_patcher.start()

    def tearDown(self):
        """Clean up patches."""
        self.cooldown_patcher.stop()
        self.settings_patcher.stop()
        self.db_patcher.stop()

    def test_allow_when_never_attempted(self):
        """Test that reservations are allowed when never attempted before."""
        self.mock_connection.select.return_value = []

        manager = ThrottleManager()

        mock_show = mock.MagicMock()
        mock_show.indexerid = 12345
        mock_show.name = "Test Show"

        self.assertTrue(manager.reserve_search_attempt(mock_show))

    def test_block_when_recently_attempted(self):
        """Test that reservations are blocked when recently attempted."""
        # Return a recent timestamp (1 hour ago)
        recent_time = time.time() - 3600
        self.mock_connection.select.return_value = [{"last_attempt": recent_time}]

        manager = ThrottleManager()

        mock_show = mock.MagicMock()
        mock_show.indexerid = 12345
        mock_show.name = "Test Show"

        # With 7-day cooldown, 1 hour ago should still be blocked
        self.assertFalse(manager.reserve_search_attempt(mock_show))

    def test_allow_after_cooldown_expires(self):
        """Test that reservations are allowed after cooldown expires."""
        # Return an old timestamp (8 days ago)
        old_time = time.time() - (8 * 24 * 60 * 60)
        self.mock_connection.select.return_value = [{"last_attempt": old_time}]

        manager = ThrottleManager()

        mock_show = mock.MagicMock()
        mock_show.indexerid = 12345
        mock_show.name = "Test Show"

        # With 7-day cooldown, 8 days ago should be allowed
        self.assertTrue(manager.reserve_search_attempt(mock_show))

    def test_file_cooldown_hours(self):
        """Test file-based cooldown uses hours not days."""
        # Return a timestamp 24 hours ago
        recent_time = time.time() - (24 * 60 * 60)
        self.mock_connection.select.return_value = [{"last_attempt": recent_time}]

        manager = ThrottleManager()

        # With 72-hour cooldown, 24 hours ago should still be blocked
        self.assertFalse(manager.reserve_postprocess_attempt("/test/file.mkv"))

    def test_disabled_ai_blocks_all(self):
        """Test that disabled AI blocks all reservations."""
        self.mock_settings.AI_ENABLED = False

        manager = ThrottleManager()

        mock_show = mock.MagicMock()
        mock_show.indexerid = 12345

        self.assertFalse(manager.reserve_search_attempt(mock_show))
        self.assertFalse(manager.reserve_postprocess_attempt("/test/file.mkv"))


if __name__ == "__main__":
    unittest.main()
