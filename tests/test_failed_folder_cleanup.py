"""Tests for the failed-download folder path: marker stripping, deferral to the download status
checker, and the cleanup of the litter SAB leaves in the completed directory.

SAB pre-creates a job's destination folder at post-processing start and renames it _FAILED_<jobname>
when repair fails, so the folder is usually empty and its name unparseable -- the failure marker
itself used to break failed processing, and the folder was warned about every auto-processing cycle
forever.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sickchill import settings
from sickchill.helper.exceptions import FailedPostProcessingFailedException
from sickchill.oldbeard import failedProcessor, processTV
from tests import conftest  # noqa: F401  pulls in the test bootstrap

assert conftest


class StripFailureMarkersTest(unittest.TestCase):
    def test_leading_markers_are_stripped(self):
        self.assertEqual(failedProcessor.strip_failure_markers("_FAILED_Show.S01E01.720p-GRP"), "Show.S01E01.720p-GRP")
        self.assertEqual(failedProcessor.strip_failure_markers("_UNDERSIZED_Show.S01E01"), "Show.S01E01")

    def test_trailing_markers_are_stripped(self):
        self.assertEqual(failedProcessor.strip_failure_markers("Show.S01E01_FAILED_"), "Show.S01E01")

    def test_markers_are_case_insensitive(self):
        self.assertEqual(failedProcessor.strip_failure_markers("_failed_Show.S01E01"), "Show.S01E01")

    def test_stacked_markers_all_come_off(self):
        self.assertEqual(failedProcessor.strip_failure_markers("_FAILED__UNDERSIZED_Show.S01E01"), "Show.S01E01")

    def test_interior_tokens_are_part_of_the_name(self):
        self.assertEqual(failedProcessor.strip_failure_markers("Show._FAILED_.S01E01"), "Show._FAILED_.S01E01")

    def test_unpack_is_not_a_failure_marker(self):
        """validate_dir classifies _UNPACK folders as still unpacking; they never reach this module."""
        self.assertEqual(failedProcessor.strip_failure_markers("_UNPACK_Show.S01E01"), "_UNPACK_Show.S01E01")


class FailedProcessorTest(unittest.TestCase):
    """The folder path defers to the download status checker and never claims a tracked download."""

    RELEASE = "Show.S01E01.720p-GRP"

    def setUp(self):
        settings.NZB_METHOD = "sabnzbd"
        self.base = tempfile.mkdtemp(prefix="sc_failed_folder_")
        self.directory = os.path.join(self.base, f"_FAILED_{self.RELEASE}")
        os.mkdir(self.directory)

    def _process(self, rows, release_name=None, directory=None):
        parsed = mock.Mock()
        parsed.show.indexerid = 1
        parsed.season_number = 1
        parsed.episode_numbers = [1]
        segment = mock.Mock(season=1, episode=1)
        segment.pretty_name = "Show - S01E01"
        parsed.show.get_episode.return_value = segment

        queue = mock.Mock()
        with mock.patch.object(failedProcessor, "NameParser") as name_parser:
            name_parser.return_value.parse.return_value = parsed
            with mock.patch.object(failedProcessor, "DBConnection") as db_connection:
                db_connection.return_value.select.return_value = rows
                with mock.patch.object(settings, "searchQueueScheduler", queue):
                    processor = failedProcessor.FailedProcessor(directory or self.directory, release_name)
                    outcome = processor.process()

        return processor, outcome, queue, name_parser.return_value.parse

    def test_a_tracked_episode_is_deferred_and_the_folder_kept(self):
        """A pending row for the current client means the checker owns the download -- maybe this very
        job, maybe a newer one. Either way the folder must not enqueue and must not report success."""
        processor, outcome, queue, _ = self._process(rows=[{"client": "sabnzbd"}])

        queue.action.add_item.assert_not_called()
        self.assertTrue(processor.deferred)
        self.assertFalse(outcome, "success would let DELETE_FAILED remove the folder while it is still evidence")

    def test_a_row_from_an_abandoned_client_does_not_defer(self):
        """A reconciler that cannot talk to the row's client can never resolve it; parking the failure
        signal behind it would strand the episode until the row ages out."""
        processor, outcome, queue, _ = self._process(rows=[{"client": "nzbget"}])

        self.assertTrue(queue.action.add_item.called)
        self.assertFalse(processor.deferred)
        self.assertTrue(outcome)

    def test_an_untracked_episode_takes_the_legacy_path_with_the_folder_release_and_no_identity(self):
        processor, outcome, queue, _ = self._process(rows=[])

        item = queue.action.add_item.call_args[0][0]
        self.assertEqual(item.failed_releases, {(1, 1): {"release": self.RELEASE, "anonymous": True}})
        self.assertTrue(outcome)

    def test_the_marker_comes_off_a_folder_derived_name(self):
        _, _, _, parse = self._process(rows=[])
        parse.assert_called_once_with(self.RELEASE)

    def test_an_explicit_release_name_is_never_stripped(self):
        """Provenance: only the marker folder's own basename is cleaned. A downloader-supplied name is
        used exactly as given, marker and all."""
        explicit = f"_FAILED_{self.RELEASE}"
        _, _, _, parse = self._process(rows=[], release_name=explicit)
        parse.assert_called_once_with(explicit)


class RemoveEmptyFailedMarkerDirTest(unittest.TestCase):
    """The rmdir fallback for unparseable litter: atomic, marker-only, opt-in, never the root."""

    def setUp(self):
        settings.USE_FAILED_DOWNLOADS = True
        settings.DELETE_FAILED = True
        self.base = tempfile.mkdtemp(prefix="sc_failed_marker_")
        settings.TV_DOWNLOAD_DIR = self.base

    def tearDown(self):
        settings.DELETE_FAILED = False
        settings.TV_DOWNLOAD_DIR = ""

    def _marker_dir(self, name="_FAILED_[YE] Pocket Monsters (2023)-035 (TVO 1280x720)"):
        path = os.path.join(self.base, name)
        os.mkdir(path)
        return path

    def _process_failed(self, path, release_name=None):
        """Run process_failed with a FailedProcessor that cannot parse, the state the litter is in."""
        result = processTV.ProcessResult()
        instance = mock.Mock()
        instance.process.side_effect = FailedPostProcessingFailedException()
        instance.log = ""
        with mock.patch.object(processTV.failedProcessor, "FailedProcessor", return_value=instance):
            processTV.process_failed(path, release_name, result)
        return result

    def test_an_empty_marker_dir_is_removed(self):
        path = self._marker_dir()
        result = self._process_failed(path)

        self.assertFalse(os.path.exists(path))
        self.assertTrue(result.result)
        self.assertIn("Removed the empty failed-download folder", result.output)

    def test_without_delete_failed_nothing_is_removed(self):
        settings.DELETE_FAILED = False
        path = self._marker_dir()
        result = self._process_failed(path)

        self.assertTrue(os.path.exists(path))
        self.assertFalse(result.result)

    def test_a_dir_with_content_survives(self):
        """os.rmdir is the whole safety argument: anything inside, whenever it arrived, fails the call."""
        path = self._marker_dir()
        Path(path, "payload.mkv").write_bytes(b"x")
        result = self._process_failed(path)

        self.assertTrue(os.path.exists(path))
        self.assertFalse(result.result)

    def test_a_non_marker_dir_is_never_touched(self):
        """failed=True does not only come from marker folders; an arbitrary directory must survive."""
        path = os.path.join(self.base, "Some.Manual.Folder")
        os.mkdir(path)
        result = self._process_failed(path)

        self.assertTrue(os.path.exists(path))
        self.assertFalse(result.result)

    def test_an_explicit_release_name_keeps_the_folder(self):
        """An external script's failure signal is not litter, even when it cannot be parsed."""
        path = self._marker_dir()
        result = self._process_failed(path, release_name="Some.Release.Name")

        self.assertTrue(os.path.exists(path))
        self.assertFalse(result.result)

    def test_the_download_root_is_never_removed(self):
        root = os.path.join(self.base, "_FAILED_root")
        os.mkdir(root)
        settings.TV_DOWNLOAD_DIR = root
        result = self._process_failed(root)

        self.assertTrue(os.path.exists(root))
        self.assertFalse(result.result)

    def test_a_deferred_folder_is_retained_and_not_warned_about(self):
        path = self._marker_dir()
        result = processTV.ProcessResult()
        instance = mock.Mock()
        instance.process.return_value = False
        instance.deferred = True
        instance.log = ""
        with mock.patch.object(processTV.failedProcessor, "FailedProcessor", return_value=instance):
            processTV.process_failed(path, None, result)

        self.assertTrue(os.path.exists(path), "the folder is the only evidence until the checker resolves it")
        self.assertFalse(result.result)
        self.assertIn("download status checker", result.output)
        self.assertNotIn("Failed Download Processing failed", result.output)


if __name__ == "__main__":
    unittest.main()
