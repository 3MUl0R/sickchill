"""
Tests for legacy provider ``search_mode`` normalization.

Old SickChill/SickRage configs stored ``eponly``/``sponly``; the search code now compares
``provider.search_mode`` against ``episode``/``season`` exactly, so a stored legacy value matches
neither and silently breaks episode search. ``GenericProvider.normalize_search_mode`` maps them, and is
applied at config load (``start.py``) and in the newznab / torrent-rss pipe-string parsers.
"""
import unittest

from configobj import ConfigObj

from sickchill.oldbeard.config import check_setting_str
from sickchill.oldbeard.providers.newznab import NewznabProvider
from sickchill.oldbeard.providers.rsstorrent import TorrentRssProvider
from sickchill.providers.GenericProvider import GenericProvider
from tests import conftest


class NormalizeSearchModeTests(unittest.TestCase):
    """The pure normalizer on the base provider class."""

    def test_legacy_eponly_maps_to_episode(self):
        self.assertEqual(GenericProvider.normalize_search_mode("eponly"), "episode")

    def test_legacy_sponly_maps_to_season(self):
        self.assertEqual(GenericProvider.normalize_search_mode("sponly"), "season")

    def test_current_values_pass_through(self):
        self.assertEqual(GenericProvider.normalize_search_mode("episode"), "episode")
        self.assertEqual(GenericProvider.normalize_search_mode("season"), "season")

    def test_unknown_empty_none_default_to_episode(self):
        self.assertEqual(GenericProvider.normalize_search_mode(""), "episode")
        self.assertEqual(GenericProvider.normalize_search_mode(None), "episode")
        self.assertEqual(GenericProvider.normalize_search_mode("garbage"), "episode")

    def test_case_and_whitespace_insensitive(self):
        self.assertEqual(GenericProvider.normalize_search_mode("EpOnly"), "episode")
        self.assertEqual(GenericProvider.normalize_search_mode(" SPONLY "), "season")
        self.assertEqual(GenericProvider.normalize_search_mode("Season"), "season")


class NewznabMakeProviderSearchModeTests(conftest.SickChillTestDBCase):
    """The custom-newznab pipe-string parser normalizes legacy values."""

    def _mode(self, raw):
        # name|url|key|categories|enabled|search_mode|search_fallback|enable_daily|enable_backlog
        cfg = f"Test|https://example.com/api/|k|5030,5040|1|{raw}|0|1|1"
        return NewznabProvider._make_provider(cfg).search_mode

    def test_legacy_values_normalized(self):
        self.assertEqual(self._mode("eponly"), "episode")
        self.assertEqual(self._mode("sponly"), "season")

    def test_current_values_pass_through(self):
        self.assertEqual(self._mode("episode"), "episode")
        self.assertEqual(self._mode("season"), "season")


class TorrentRssMakeProviderSearchModeTests(conftest.SickChillTestDBCase):
    """The custom torrent-RSS pipe-string parser normalizes legacy values (previously it did not)."""

    def _mode(self, raw):
        # name|url|cookies|titleTAG|enabled|search_mode|search_fallback|enable_daily|enable_backlog
        cfg = f"Test|https://example.com/rss|||1|{raw}|0|1|1"
        return TorrentRssProvider._make_provider(cfg).search_mode

    def test_legacy_values_normalized(self):
        self.assertEqual(self._mode("eponly"), "episode")
        self.assertEqual(self._mode("sponly"), "season")


class StartupLoadDefaultPreservationTests(conftest.SickChillTestDBCase):
    """start.py load must not clobber an already-parsed search_mode when the per-provider key is absent."""

    def test_missing_per_key_preserves_parsed_pipe_value(self):
        # Custom provider parsed from a pipe string with legacy sponly -> season.
        provider = NewznabProvider._make_provider("Test|https://example.com/api/|k|5030,5040|1|sponly|0|1|1")
        self.assertEqual(provider.search_mode, "season")
        # Replicate the start.py load with NO per-provider key: default to the parsed value, then normalize.
        cfg = ConfigObj()
        loaded = check_setting_str(cfg, provider.get_id().upper(), provider.get_id("_search_mode"), provider.search_mode or "episode")
        self.assertEqual(provider.normalize_search_mode(loaded), "season")

    def test_present_legacy_per_key_overrides_then_normalizes(self):
        provider = NewznabProvider._make_provider("Test|https://example.com/api/|k|5030,5040|1|episode|0|1|1")
        cfg = ConfigObj()
        cfg[provider.get_id().upper()] = {provider.get_id("_search_mode"): "eponly"}
        loaded = check_setting_str(cfg, provider.get_id().upper(), provider.get_id("_search_mode"), provider.search_mode or "episode")
        self.assertEqual(provider.normalize_search_mode(loaded), "episode")


if __name__ == "__main__":
    unittest.main()
