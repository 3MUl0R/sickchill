"""
Regression tests: pymediainfo / libmediainfo must not be called from the video
screen-size helper or the AI post-process analyzer.

libmediainfo segfaults the whole Python interpreter on some platforms (notably
Alpine/musl). A native segfault cannot be caught by try/except, so calling it from
the post-processing path crash-looped the whole app (the web UI dropped every ~5
minutes). These tests guard against pymediainfo being reintroduced there.
"""

from __future__ import annotations

import inspect
import unittest
from unittest import mock


class TestMediaInfoHelperNoPymediainfo(unittest.TestCase):
    """sickchill.helper.media_info must not use pymediainfo."""

    def test_module_has_no_mediainfo_symbols(self):
        from sickchill.helper import media_info

        self.assertFalse(hasattr(media_info, "mediainfo"), "pymediainfo import should be gone")
        self.assertFalse(hasattr(media_info, "_mediainfo_screen_size"), "segfaulting helper should be removed")

    def test_source_has_no_pymediainfo_import_or_call(self):
        from sickchill.helper import media_info

        # Guard against the actual dangerous patterns being reintroduced. (The word
        # "pymediainfo" is allowed in explanatory comments; a real import/call is not.)
        src = inspect.getsource(media_info)
        self.assertNotIn("from pymediainfo import", src)
        self.assertNotIn("import pymediainfo", src)
        self.assertNotIn("mediainfo.parse(", src)
        self.assertNotIn("MediaInfo.parse(", src)
        self.assertNotIn("def _mediainfo_screen_size", src)

    def test_video_screen_size_safe_on_nonexistent_file(self):
        from sickchill.helper.media_info import video_screen_size

        # Must not raise, and returns (None, None) for a path that isn't a real media file.
        self.assertEqual(video_screen_size("/nonexistent/path/file.mkv"), (None, None))

    def test_video_screen_size_uses_only_enzyme_and_avi(self):
        from sickchill.helper import media_info

        with mock.patch.object(media_info, "is_media_file", return_value=True), mock.patch.object(
            media_info, "_mkv_screen_size", return_value=(1280, 720)
        ) as mkv, mock.patch.object(media_info, "_avi_screen_size", return_value=(None, None)) as avi:
            # Use a name not already cached in bad_files.
            result = media_info.video_screen_size("/tmp/only-safe-methods.mkv")

        self.assertEqual(result, (1280, 720))
        mkv.assert_called_once()
        avi.assert_not_called()  # short-circuits once mkv returns a size


class TestGetMediaInfoNoPymediainfo(unittest.TestCase):
    """postprocess_analyzer._get_media_info must not call pymediainfo."""

    def test_source_has_no_pymediainfo_import_or_call(self):
        from sickchill.oldbeard.ai import postprocess_analyzer

        # A real import/call is forbidden; the word in an explanatory comment is fine.
        src = inspect.getsource(postprocess_analyzer._get_media_info)
        self.assertNotIn("from pymediainfo import", src)
        self.assertNotIn("import pymediainfo", src)
        self.assertNotIn("MediaInfo.parse(", src)

    def test_unknown_metadata_is_not_fabricated_zero(self):
        # When dimensions can't be read, the AI prompt must NOT receive "0x0" / "0 minutes"
        # (which the model is told to treat as red flags); it must receive "unknown".
        from sickchill.helper import media_info
        from sickchill.oldbeard.ai import postprocess_analyzer

        with mock.patch.object(media_info, "video_screen_size", return_value=(None, None)):
            info = postprocess_analyzer._get_media_info("/downloads/Some.Show.S01E01.1080p.mkv")

        self.assertEqual(info["container"], "MKV")
        self.assertEqual(info["resolution"], "unknown")
        self.assertEqual(info["duration"], "unknown")
        self.assertEqual(info["video_codec"], "unknown")
        self.assertEqual(info["audio_codec"], "unknown")
        # numeric fallbacks stay 0 but must never be surfaced as a real resolution
        self.assertNotEqual(info["resolution"], "0x0")

    def test_populates_resolution_from_helper(self):
        from sickchill.helper import media_info
        from sickchill.oldbeard.ai import postprocess_analyzer

        with mock.patch.object(media_info, "video_screen_size", return_value=(1920, 1080)):
            info = postprocess_analyzer._get_media_info("/downloads/x.mkv")

        self.assertEqual((info["width"], info["height"]), (1920, 1080))
        self.assertEqual(info["resolution"], "1920x1080")

    def test_media_info_keys_match_prompt_placeholders(self):
        # Guard: every {placeholder} the analyzer feeds from media_info must exist in the
        # dict, so analyze_file's template.format() can't KeyError at runtime.
        from sickchill.helper import media_info
        from sickchill.oldbeard.ai import postprocess_analyzer

        with mock.patch.object(media_info, "video_screen_size", return_value=(None, None)):
            info = postprocess_analyzer._get_media_info("/downloads/x.mkv")

        for key in ("container", "resolution", "video_codec", "audio_codec", "duration"):
            self.assertIn(key, info)

    def test_does_not_raise_on_missing_file(self):
        from sickchill.oldbeard.ai import postprocess_analyzer

        # No mocking: exercises the real (crash-safe) path end to end.
        info = postprocess_analyzer._get_media_info("/nonexistent/path/file.mkv")
        self.assertIn("container", info)
        self.assertEqual(info["resolution"], "unknown")


class TestAviScreenSize(unittest.TestCase):
    """The AVI header reader is now the only screen-size source for .avi files (pymediainfo
    was the fallback that used to cover them), so it must actually parse .avi files."""

    def _write_avi_with_dimensions(self, width, height):
        import os
        import struct
        import tempfile

        # The reader takes dwWidth from header[64:68] and dwHeight from header[68:72] (LE).
        header = bytearray(64) + struct.pack("<I", width) + struct.pack("<I", height)
        fd, path = tempfile.mkstemp(suffix=".avi")
        with os.fdopen(fd, "wb") as f:
            f.write(bytes(header))
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def test_avi_header_dimensions_are_parsed(self):
        from sickchill.helper import media_info

        path = self._write_avi_with_dimensions(640, 480)
        self.assertEqual(media_info._avi_screen_size(path), (640, 480))

    def test_video_screen_size_reads_avi(self):
        from sickchill.helper import media_info

        path = self._write_avi_with_dimensions(720, 576)
        # is_media_file recognizes .avi; video_screen_size should fall through mkv -> avi.
        self.assertEqual(media_info.video_screen_size(path), (720, 576))

    def test_avi_extension_match_is_case_insensitive(self):
        import os
        import struct
        import tempfile

        header = bytearray(64) + struct.pack("<I", 640) + struct.pack("<I", 480)
        fd, path = tempfile.mkstemp(suffix=".AVI")
        with os.fdopen(fd, "wb") as f:
            f.write(bytes(header))
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))

        from sickchill.helper import media_info

        self.assertEqual(media_info._avi_screen_size(path), (640, 480))


if __name__ == "__main__":
    unittest.main()
