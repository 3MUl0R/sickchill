"""
Tests for processTV.restore_media_extensions.

Some NZBs deliver a single obfuscated payload file with no extension; the download client reports
Completed but nothing in the folder passes is_media_file, so the release strands in a snatched
status forever. restore_media_extensions sniffs the container magic of extension-less files and
renames them in place so the normal pipeline can process them.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from sickchill.oldbeard import processTV

EBML = b"\x1aE\xdf\xa3" + b"\x00" * 20
MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 16
AVI = b"RIFF\x10\x00\x00\x00AVI LIST" + b"\x00" * 16


class RestoreMediaExtensionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.result = processTV.ProcessResult()

    def write(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def restore(self, filenames):
        return processTV.restore_media_extensions(self.tmp, filenames, self.result)

    def test_extensionless_mkv_payload_is_renamed(self):
        self.write("z6oG5wuIRuVINpMritQokB", EBML)
        restored = self.restore(["z6oG5wuIRuVINpMritQokB"])
        self.assertEqual(restored, ["z6oG5wuIRuVINpMritQokB.mkv"])
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "z6oG5wuIRuVINpMritQokB.mkv")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "z6oG5wuIRuVINpMritQokB")))

    def test_extensionless_mp4_and_avi_payloads_are_renamed(self):
        self.write("payload1", MP4)
        self.write("payload2", AVI)
        restored = self.restore(["payload1", "payload2"])
        self.assertEqual(restored, ["payload1.mp4", "payload2.avi"])

    def test_non_media_extensionless_file_is_left_alone(self):
        self.write("README", b"this is not a video, it just has no extension")
        restored = self.restore(["README"])
        self.assertEqual(restored, ["README"])
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "README")))

    def test_file_with_extension_is_never_touched(self):
        self.write("episode.mkv", EBML)
        restored = self.restore(["episode.mkv"])
        self.assertEqual(restored, ["episode.mkv"])

    def test_collision_with_existing_target_leaves_payload_alone(self):
        self.write("payload", EBML)
        self.write("payload.mkv", EBML)
        restored = self.restore(["payload", "payload.mkv"])
        self.assertEqual(restored, ["payload", "payload.mkv"])
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "payload")))

    def test_missing_file_is_left_alone(self):
        restored = self.restore(["never-downloaded"])
        self.assertEqual(restored, ["never-downloaded"])

    def test_empty_directory_returns_input(self):
        # release-file mode walks a synthetic ("", [], [name]) triple; nothing to sniff there
        restored = processTV.restore_media_extensions("", ["whatever"], self.result)
        self.assertEqual(restored, ["whatever"])

    def test_short_file_is_left_alone(self):
        self.write("tiny", b"\x1aE")
        restored = self.restore(["tiny"])
        self.assertEqual(restored, ["tiny"])

    def test_m4a_audio_is_not_promoted(self):
        # ISO-BMFF ftyp with an audio brand: same container family as mp4, must not be renamed
        self.write("audiobook", b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 16)
        restored = self.restore(["audiobook"])
        self.assertEqual(restored, ["audiobook"])

    def test_heif_avif_images_are_not_promoted(self):
        self.write("photo1", b"\x00\x00\x00\x18ftypheic" + b"\x00" * 16)
        self.write("photo2", b"\x00\x00\x00\x18ftypavif" + b"\x00" * 16)
        restored = self.restore(["photo1", "photo2"])
        self.assertEqual(restored, ["photo1", "photo2"])


if __name__ == "__main__":
    unittest.main()
