"""
Tests for season-aware download-folder reaping in processTV.process_dir.

After a `move`, a release folder is reaped (deleted, including leftover junk) ONLY when every
video in it was handled (moved / matched-existing / already-processed) -- i.e. process_media
reports no failures -- AND the folder is a leaf (no unprocessed subdirectories). Folders with a
failed/AI-cooldown file, copy method, manual-without-delete, or pending child dirs are retained.

These run against a real temp filesystem; process_media and validate_dir are mocked so no real
post-processing happens, but the actual reaping decision and real delete_folder run.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock

from sickchill.oldbeard import processTV


class TestProcessDirReaping(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.settings_patcher = mock.patch("sickchill.oldbeard.processTV.settings")
        s = self.settings_patcher.start()
        self.addCleanup(self.settings_patcher.stop)
        s.TV_DOWNLOAD_DIR = self.tmp
        s.PROCESS_METHOD = "move"
        s.PROCESSOR_FOLLOW_SYMLINKS = False

        # Bypass validate_dir's DB/parse logic; treat every folder as processable.
        vdir = mock.patch("sickchill.oldbeard.processTV.validate_dir", return_value=True)
        vdir.start()
        self.addCleanup(vdir.stop)

        # Treat .mkv as media.
        media = mock.patch("sickchill.oldbeard.processTV.is_media_file", side_effect=lambda f: str(f).lower().endswith(".mkv"))
        media.start()
        self.addCleanup(media.stop)

    def _touch(self, *relparts):
        p = os.path.join(self.tmp, *relparts)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(b"x")
        return p

    @staticmethod
    def _pm_success(current_directory, video_files, *args, **kwargs):
        """Simulate a successful move: remove the source videos, report no failures."""
        for vf in video_files:
            try:
                os.remove(os.path.join(current_directory, vf))
            except OSError:
                pass
        return []

    @staticmethod
    def _pm_skip(current_directory, video_files, *args, **kwargs):
        """Simulate already-processed: leave files in place, report no failures."""
        return []

    @staticmethod
    def _pm_fail(current_directory, video_files, *args, **kwargs):
        """Simulate failure (e.g. AI cooldown): leave files, report them as failed."""
        return list(video_files)

    def test_reaps_leaf_folder_and_junk_on_success(self):
        rel = os.path.join(self.tmp, "Release.S01E01")
        self._touch("Release.S01E01", "ep.mkv")
        self._touch("Release.S01E01", "leftover.nfo")
        with mock.patch("sickchill.oldbeard.processTV.process_media", side_effect=self._pm_success):
            processTV.process_dir(self.tmp, process_method="move", delete_on=True, mode="auto")
        self.assertFalse(os.path.exists(rel), "leaf folder (and its junk) should be reaped")

    def test_retains_folder_when_a_file_failed(self):
        rel = os.path.join(self.tmp, "Release.S01E02")
        self._touch("Release.S01E02", "ep.mkv")
        with mock.patch("sickchill.oldbeard.processTV.process_media", side_effect=self._pm_fail):
            processTV.process_dir(self.tmp, process_method="move", delete_on=True, mode="auto")
        self.assertTrue(os.path.exists(rel), "folder with a failed/cooldown file must be retained")
        self.assertTrue(os.path.exists(os.path.join(rel, "ep.mkv")), "the uncaptured file must survive")

    def test_copy_method_never_reaps(self):
        rel = os.path.join(self.tmp, "Release.S01E03")
        self._touch("Release.S01E03", "ep.mkv")
        with mock.patch("sickchill.oldbeard.processTV.process_media", side_effect=self._pm_success):
            processTV.process_dir(self.tmp, process_method="copy", delete_on=True, mode="auto")
        self.assertTrue(os.path.exists(rel), "copy leaves the source in place")

    def test_manual_without_delete_never_reaps(self):
        rel = os.path.join(self.tmp, "Release.Manual")
        self._touch("Release.Manual", "ep.mkv")
        with mock.patch("sickchill.oldbeard.processTV.process_media", side_effect=self._pm_success):
            processTV.process_dir(self.tmp, process_method="move", delete_on=False, mode="manual")
        self.assertTrue(os.path.exists(rel), "manual PP without delete must not reap")

    def test_mixed_direct_and_nested_retains_parent_reaps_leaf_child(self):
        parent = os.path.join(self.tmp, "Release.Mixed")
        child = os.path.join(parent, "Extras")
        self._touch("Release.Mixed", "ep1.mkv")
        self._touch("Release.Mixed", "Extras", "ep2.mkv")
        with mock.patch("sickchill.oldbeard.processTV.process_media", side_effect=self._pm_success):
            processTV.process_dir(self.tmp, process_method="move", delete_on=True, mode="auto")
        self.assertTrue(os.path.exists(parent), "parent with a child dir is retained (leaf rule)")
        self.assertFalse(os.path.exists(child), "leaf child folder is reaped")

    def test_reaps_already_processed_leftovers(self):
        # The existing copy-leftover pile: files are already-processed (no failures) -> reap.
        rel = os.path.join(self.tmp, "Release.Old")
        self._touch("Release.Old", "ep.mkv")
        with mock.patch("sickchill.oldbeard.processTV.process_media", side_effect=self._pm_skip):
            processTV.process_dir(self.tmp, process_method="move", delete_on=True, mode="auto")
        self.assertFalse(os.path.exists(rel), "already-processed leftover folder should be reaped")


if __name__ == "__main__":
    unittest.main()
