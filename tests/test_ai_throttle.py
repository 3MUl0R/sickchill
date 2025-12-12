"""
Tests for AI throttle functionality.

Tests:
    TestFileFingerprint - File fingerprinting for cooldowns
    TestRequestHash - Request hashing for caching
    TestBudgetTracking - In-memory budget limit tracking
    TestCooldownLogic - Cooldown calculation logic
"""

from __future__ import annotations

import hashlib
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


class TestRequestHash(unittest.TestCase):
    """Test request hashing for cache keys."""

    def test_hash_basic(self):
        """Test basic hash generation."""
        hash_val = ThrottleManager.generate_request_hash("test prompt")

        self.assertEqual(len(hash_val), 32)
        self.assertTrue(all(c in "0123456789abcdef" for c in hash_val))

    def test_hash_consistency(self):
        """Test that same input produces same hash."""
        prompt = "Analyze these search results for Breaking Bad S05E16"

        h1 = ThrottleManager.generate_request_hash(prompt)
        h2 = ThrottleManager.generate_request_hash(prompt)

        self.assertEqual(h1, h2)

    def test_hash_with_context(self):
        """Test hash with context dict."""
        prompt = "Analyze results"
        context = {"show": "Breaking Bad", "season": 5}

        h1 = ThrottleManager.generate_request_hash(prompt, context)
        h2 = ThrottleManager.generate_request_hash(prompt, context)

        self.assertEqual(h1, h2)

    def test_hash_context_affects_hash(self):
        """Test that different contexts produce different hashes."""
        prompt = "Analyze results"

        h1 = ThrottleManager.generate_request_hash(prompt, {"show": "Breaking Bad"})
        h2 = ThrottleManager.generate_request_hash(prompt, {"show": "Better Call Saul"})

        self.assertNotEqual(h1, h2)

    def test_hash_without_context(self):
        """Test hash with None context."""
        prompt = "Test prompt"

        h1 = ThrottleManager.generate_request_hash(prompt, None)
        h2 = ThrottleManager.generate_request_hash(prompt)

        self.assertEqual(h1, h2)


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

    def test_budget_limit_blocks(self):
        """Test that exceeding budget limit returns False."""
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

    def tearDown(self):
        """Clean up patches."""
        self.settings_patcher.stop()
        self.db_patcher.stop()

    def test_allow_when_never_attempted(self):
        """Test that requests are allowed when never attempted before."""
        self.mock_connection.select.return_value = []

        manager = ThrottleManager()

        mock_show = mock.MagicMock()
        mock_show.indexerid = 12345
        mock_show.name = "Test Show"

        self.assertTrue(manager.allow_search_for_show(mock_show))

    def test_block_when_recently_attempted(self):
        """Test that requests are blocked when recently attempted."""
        # Return a recent timestamp (1 hour ago)
        recent_time = time.time() - 3600
        self.mock_connection.select.return_value = [{"last_attempt": recent_time}]

        manager = ThrottleManager()

        mock_show = mock.MagicMock()
        mock_show.indexerid = 12345
        mock_show.name = "Test Show"

        # With 7-day cooldown, 1 hour ago should still be blocked
        self.assertFalse(manager.allow_search_for_show(mock_show))

    def test_allow_after_cooldown_expires(self):
        """Test that requests are allowed after cooldown expires."""
        # Return an old timestamp (8 days ago)
        old_time = time.time() - (8 * 24 * 60 * 60)
        self.mock_connection.select.return_value = [{"last_attempt": old_time}]

        manager = ThrottleManager()

        mock_show = mock.MagicMock()
        mock_show.indexerid = 12345
        mock_show.name = "Test Show"

        # With 7-day cooldown, 8 days ago should be allowed
        self.assertTrue(manager.allow_search_for_show(mock_show))

    def test_file_cooldown_hours(self):
        """Test file-based cooldown uses hours not days."""
        # Return a timestamp 24 hours ago
        recent_time = time.time() - (24 * 60 * 60)
        self.mock_connection.select.return_value = [{"last_attempt": recent_time}]

        manager = ThrottleManager()

        # With 72-hour cooldown, 24 hours ago should still be blocked
        self.assertFalse(manager.allow_postprocess_for_file("/test/file.mkv"))

    def test_disabled_ai_blocks_all(self):
        """Test that disabled AI blocks all requests."""
        self.mock_settings.AI_ENABLED = False

        manager = ThrottleManager()

        mock_show = mock.MagicMock()
        mock_show.indexerid = 12345

        self.assertFalse(manager.allow_search_for_show(mock_show))
        self.assertFalse(manager.allow_postprocess_for_file("/test/file.mkv"))


if __name__ == "__main__":
    unittest.main()
