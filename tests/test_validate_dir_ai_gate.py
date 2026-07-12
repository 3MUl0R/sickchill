"""
Tests for the AI-aware folder gate in processTV.validate_dir.

When rule-based name parsing cannot identify any media file in a folder, validate_dir
should still admit the folder (return True) so the AI fallback matcher inside PostProcessor
gets a chance -- but only when AI post-process matching is enabled. With AI disabled, or
when the folder holds no media, behavior is unchanged (return False).
"""

from __future__ import annotations

import unittest
from unittest import mock

from sickchill.oldbeard.processTV import ProcessResult, validate_dir


class TestValidateDirAIGate(unittest.TestCase):
    def setUp(self):
        self.path = r"X:\downloads\complete\[Moozzi2] Working S3-09 [BD 1920x1080 x 264 FLACx2]"
        self.media = "[Moozzi2] Working S3 - 09 (BD 1920x1080 x.264 FLACx2).mkv"

        self.settings_patcher = mock.patch("sickchill.oldbeard.processTV.settings")
        self.mock_settings = self.settings_patcher.start()
        # TV_DOWNLOAD_DIR == path so the hidden-folder guard short-circuits without touching FS
        self.mock_settings.TV_DOWNLOAD_DIR = self.path
        self.mock_settings.PROCESSOR_FOLLOW_SYMLINKS = False
        self.mock_settings.POSTPONE_IF_SYNC_FILES = True
        self.mock_settings.UNPACK = 0
        self.mock_settings.UNPACK_PROCESS_CONTENTS = 1  # differs from UNPACK -> no rar handling
        self.mock_settings.AI_ENABLED = True
        self.mock_settings.AI_POSTPROCESS_MATCH_ENABLED = True

        # No shows in DB -> the "already inside a show dir" guard is a no-op
        self.db_patcher = mock.patch("sickchill.oldbeard.processTV.db.DBConnection")
        mock_db = self.db_patcher.start()
        mock_db.return_value.select.return_value = []

        self.walk_patcher = mock.patch("sickchill.oldbeard.processTV.os.walk")
        self.mock_walk = self.walk_patcher.start()
        self.mock_walk.return_value = [(self.path, [], [self.media])]

        self.sync_patcher = mock.patch("sickchill.oldbeard.processTV.is_sync_file", return_value=False)
        self.sync_patcher.start()
        self.media_patcher = mock.patch("sickchill.oldbeard.processTV.is_media_file", return_value=True)
        self.media_patcher.start()

        # Rule-based parse always fails for these releases
        self.pp_patcher = mock.patch("sickchill.oldbeard.processTV.postProcessor.guessit_findit", return_value=False)
        self.pp_patcher.start()

    def tearDown(self):
        mock.patch.stopall()

    def test_defers_to_ai_when_parse_fails_and_ai_enabled(self):
        result = ProcessResult()
        self.assertTrue(validate_dir(self.path, "", False, result))
        self.assertIn("deferring to AI post-process matcher", result.output)

    def test_rejects_when_parse_fails_and_match_disabled(self):
        self.mock_settings.AI_POSTPROCESS_MATCH_ENABLED = False
        result = ProcessResult()
        self.assertFalse(validate_dir(self.path, "", False, result))
        self.assertIn("No processable items found", result.output)

    def test_rejects_when_parse_fails_and_ai_globally_disabled(self):
        self.mock_settings.AI_ENABLED = False
        result = ProcessResult()
        self.assertFalse(validate_dir(self.path, "", False, result))
        self.assertIn("No processable items found", result.output)

    def test_rejects_when_no_media_even_if_ai_enabled(self):
        # A folder with files but none recognized as media must not be admitted by AI defer
        self.mock_walk.return_value = [(self.path, [], ["readme.txt", "poster.jpg"])]
        with mock.patch("sickchill.oldbeard.processTV.is_media_file", return_value=False):
            result = ProcessResult()
            self.assertFalse(validate_dir(self.path, "", False, result))
            self.assertIn("No processable items found", result.output)

    def test_parseable_release_still_admitted_without_ai_defer(self):
        # When rule-based parsing succeeds, validate_dir returns True via the normal path
        with mock.patch("sickchill.oldbeard.processTV.postProcessor.guessit_findit", return_value=True):
            result = ProcessResult()
            self.assertTrue(validate_dir(self.path, "", False, result))
            self.assertNotIn("deferring to AI post-process matcher", result.output)


if __name__ == "__main__":
    unittest.main()
