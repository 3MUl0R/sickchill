"""Franchise-title collision guards (see .codex-reviews/franchise_collision_plan.md).

Anime franchises publish sub-series whose release names differ from the base title only by a
disambiguator -- a year ("Gintama (2015)"), a roman numeral ("Mushoku Tensei II"), or punctuation
("K-ON!!") -- and each sub-series restarts its own numbering. These tests pin the deterministic
franchise gate and its call sites: the parser chokepoint, the AI search/file matchers, the cache
read path, and the scene-exceptions storage semantics that back it all.
"""

import os
import types
import unittest
from unittest import mock

from sickchill import settings
from sickchill.oldbeard import common, db, name_cache, scene_exceptions
from sickchill.oldbeard.name_parser import parser
from tests import conftest

assert conftest


class TitleYearTests(unittest.TestCase):
    def test_parenthesized_and_bare_years_are_found(self):
        self.assertEqual(parser.extract_title_years("Gintama (2015) - 50 (BD)"), {2015})
        self.assertEqual(parser.extract_title_years("[Abystoma] Gintama 2015-50 BD"), {2015})

    def test_token_only_bracket_year_is_found(self):
        self.assertEqual(parser.extract_title_years("[Abystoma] Gintama [2015] - 50"), {2015})

    def test_leading_release_group_block_is_exempt(self):
        self.assertEqual(parser.extract_title_years("[2015subs] Gintama - 50 [720p]"), set())

    def test_resolutions_and_metadata_blocks_are_not_years(self):
        self.assertEqual(parser.extract_title_years("[Grp] Show - 05 (BD 1920x1080 x265)"), set())
        self.assertEqual(parser.extract_title_years("Show - 05 2160p WEB"), set())
        self.assertEqual(parser.extract_title_years("[Grp] Show - 05 [BD 2015 remaster]"), set())

    def test_crc_blocks_are_not_years(self):
        self.assertEqual(parser.extract_title_years("[Grp] Show - 05 [1AB0376E]"), set())


class ExplicitSeasonTokenTests(unittest.TestCase):
    def test_token_only_bracket_forms_are_detected(self):
        self.assertEqual(parser.extract_explicit_anime_season("[Grp] Show (S2) - 01 [720p]"), 2)
        self.assertEqual(parser.extract_explicit_anime_season("[Grp] Show [S02] - 01 [720p]"), 2)
        self.assertEqual(parser.extract_explicit_anime_season("[Grp] Show (Season 2) - 01 [720p]"), 2)

    def test_group_and_codec_blocks_are_not_seasons(self):
        self.assertIsNone(parser.extract_explicit_anime_season("[S2Productions] Show - 01 [720p]"))
        self.assertIsNone(parser.extract_explicit_anime_season("[Grp] Show - 01 (BD S2 720p)"))

    def test_strip_removes_the_bracketed_form_too(self):
        self.assertEqual(parser.strip_explicit_anime_season("Show (S2) - 01"), "Show - 01")


class SequelMarkerTests(unittest.TestCase):
    def _show(self, name="show name"):
        show = types.SimpleNamespace(indexerid=999999, name=name, custom_name=None)
        return show

    def _markers(self, title, show=None, aliases=()):
        index = {}
        for alias in aliases:
            index.setdefault(scene_exceptions.normalize_alias_name(alias), []).append((alias, -1, 0))
        with mock.patch.object(scene_exceptions, "get_normalized_alias_index", return_value=index):
            return parser.extract_sequel_markers(title, show)

    def test_marker_in_the_title_span_is_found(self):
        self.assertEqual(self._markers("[Moozzi2] Mushoku Tensei II - Isekai Ittara Honki Dasu - 15 (BD 1920x1080)", self._show()), {"II"})

    def test_episode_title_numeral_after_the_episode_number_is_not_scanned(self):
        self.assertEqual(self._markers("[Grp] Show - 03 - Act II (BD 1080p)", self._show()), set())

    def test_bracketed_episode_number_terminates_the_span(self):
        self.assertEqual(self._markers("[Grp] Show - [03] - Act II", self._show()), set())

    def test_numeric_canonical_title_does_not_terminate_the_span(self):
        self.assertEqual(self._markers("[Grp] 86 II - 01 (BD 1080p)", self._show(name="86")), {"II"})

    def test_token_only_bracketed_numeral_counts(self):
        self.assertEqual(self._markers("[Grp] Show [II] - 01 (BD 1080p)", self._show()), {"II"})

    def test_leading_group_block_is_exempt(self):
        self.assertEqual(self._markers("[VII-subs] Show - 01 (BD 1080p)", self._show()), set())

    def test_single_letter_numerals_are_ignored(self):
        self.assertEqual(self._markers("[Grp] Show X - 01 (BD 1080p)", self._show()), set())
        self.assertEqual(self._markers("[Grp] Show I - 01 (BD 1080p)", self._show()), set())


class NormalizeAliasTests(unittest.TestCase):
    def test_dash_and_no_dash_forms_collide(self):
        self.assertEqual(
            scene_exceptions.normalize_alias_name("Mushoku Tensei II - Isekai Ittara Honki Dasu"),
            scene_exceptions.normalize_alias_name("Mushoku Tensei II Isekai Ittara Honki Dasu"),
        )

    def test_bangs_keep_names_distinct(self):
        self.assertNotEqual(scene_exceptions.normalize_alias_name("K-ON!"), scene_exceptions.normalize_alias_name("K-ON!!"))


class FranchiseGateTests(conftest.ResetNameCacheMixin, conftest.SickChillTestPostProcessorCase):
    """The single decision function, against real exception rows in cache.db."""

    def setUp(self):
        super().setUp()
        self.show.anime = 1
        self.show.startyear = 2005
        self.show.save_to_db()

    def _seed(self, rows):
        cache_db = db.DBConnection("cache.db")
        for show_name, season, custom in rows:
            cache_db.action(
                "INSERT INTO scene_exceptions (indexer_id, show_name, season, custom) VALUES (?, ?, ?, ?)",
                [1, show_name, season, custom],
            )
        scene_exceptions.invalidate_derived_caches()

    def test_no_signals_allows(self):
        self.assertTrue(parser.franchise_gate("[Judas] show name - 15 (BD 1080p)", self.show, 1)[0])

    def test_startyear_is_neutral(self):
        self.assertTrue(parser.franchise_gate("show name (2005) - 15 (BD 1080p)", self.show, 1)[0])

    def test_unexplained_year_vetoes_the_gintama_case(self):
        for title in (
            "[Abystoma] Gintama (2015)-50 (BD 1280x720 x264 AAC) [1AB0376E]".replace("Gintama", "show name"),
            "[Abystoma] show name 2015-50 BD 1280x720 x264 AAC [1AB0376E]",
        ):
            allowed, reason = parser.franchise_gate(title, self.show, 2)
            self.assertFalse(allowed, title)
            self.assertIn("2015", reason)

    def test_bracketed_year_vetoes_too(self):
        self.assertFalse(parser.franchise_gate("[Abystoma] show name [2015] - 50 (BD 720p)", self.show, 2)[0])

    def test_year_alias_pins_its_season(self):
        # The Fate/Zero (2012) pattern: the year is a legitimate alias tagged to one season.
        self._seed([("show name (2015)", 3, 0)])
        self.assertTrue(parser.franchise_gate("show name (2015) - 05 (BD 720p)", self.show, 3)[0])
        self.assertFalse(parser.franchise_gate("show name (2015) - 05 (BD 720p)", self.show, 2)[0])

    def test_conflicted_alias_family_vetoes(self):
        # The live Mushoku rows: dash form tagged 1, no-dash twin tagged 2 -- ONE family.
        self._seed([("show name II - subtitle here", 1, 0), ("show name II subtitle here", 2, 0)])
        allowed, reason = parser.franchise_gate("[Moozzi2] show name II - subtitle here - 15 (BD 1080p)", self.show, 1)
        self.assertFalse(allowed)
        self.assertIn("multiple seasons", reason)

    def test_custom_row_overrides_the_conflicted_family(self):
        self._seed(
            [
                ("show name II - subtitle here", 1, 0),
                ("show name II subtitle here", 2, 0),
                ("show name II - subtitle here", 2, scene_exceptions.CUSTOM_BLESSED),
            ]
        )
        self.assertTrue(parser.franchise_gate("[Moozzi2] show name II - subtitle here - 15 (BD 1080p)", self.show, 2)[0])
        self.assertFalse(parser.franchise_gate("[Moozzi2] show name II - subtitle here - 15 (BD 1080p)", self.show, 1)[0])

    def test_clean_alias_pin_checks_the_season(self):
        self._seed([("show name II", 2, 0)])
        self.assertTrue(parser.franchise_gate("[Grp] show name II - 15 (BD 1080p)", self.show, 2)[0])
        self.assertFalse(parser.franchise_gate("[Grp] show name II - 15 (BD 1080p)", self.show, 1)[0])

    def test_explicit_token_must_match_the_release_space_season(self):
        self.assertFalse(parser.franchise_gate("[Grp] show name S2 - 01 (BD 1080p)", self.show, 1)[0])
        self.assertTrue(parser.franchise_gate("[Grp] show name S2 - 01 (BD 1080p)", self.show, 1, scene_season=2)[0])

    def test_token_outranks_a_contradicting_alias_tag(self):
        # "show name extra s2" tagged season 3: release text (rank 1) outranks the alias tag
        # (rank 3), so mapping to season 3 is refused unless the caller's scene season says the
        # token IS season 3's scene name (the divergent-fixture test covers that legit path).
        self._seed([("show name extra s2", 3, 0)])
        self.assertFalse(parser.franchise_gate("[Group] show name extra S2 - 03 (BD 1080p)", self.show, 3)[0])
        self.assertTrue(parser.franchise_gate("[Group] show name extra S2 - 03 (BD 1080p)", self.show, 3, scene_season=2)[0])

    def test_unexplained_marker_vetoes_even_with_no_exception_data(self):
        # Fail-closed with EMPTY alias data: "II" cannot be explained, so nothing may map.
        allowed, reason = parser.franchise_gate("[Moozzi2] show name II - 15 (BD 1080p)", self.show, 1)
        self.assertFalse(allowed)
        self.assertIn("II", reason)

    def test_marker_explained_by_a_season_less_alias_still_vetoes(self):
        self._seed([("show name II", -1, 0)])
        self.assertFalse(parser.franchise_gate("[Grp] show name II - 15 (BD 1080p)", self.show, 1)[0])

    def test_marker_in_the_canonical_name_is_neutral(self):
        self.show.custom_name = "show name II"
        self.assertTrue(parser.franchise_gate("[Grp] show name II - 15 (BD 1080p)", self.show, 1)[0])

    def test_non_anime_shows_always_pass(self):
        self.show.anime = 0
        self.assertTrue(parser.franchise_gate("Something (2015) S02E01", self.show, 2)[0])


class ParserChokepointTests(conftest.ResetNameCacheMixin, conftest.SickChillTestPostProcessorCase):
    """Every anime mapping the parser produces passes the gate; refusals surface as ambiguous."""

    def setUp(self):
        super().setUp()
        self.show.anime = 1
        self.show.startyear = 2005
        self.show.save_to_db()

    def _seed(self, rows):
        cache_db = db.DBConnection("cache.db")
        for show_name, season, custom in rows:
            cache_db.action(
                "INSERT INTO scene_exceptions (indexer_id, show_name, season, custom) VALUES (?, ?, ?, ?)",
                [1, show_name, season, custom],
            )
        name_cache.build_name_cache()

    def test_mushoku_conflicted_family_refuses_instead_of_filing_s1(self):
        """THE regression: probe-verified, these rows used to map straight to S01E15."""
        self._seed(
            [
                ("show name II - subtitle here", 1, 0),
                ("show name II", 1, 0),
                ("show name II subtitle here", 2, 0),
                ("show name S2", 2, 0),
            ]
        )
        result = parser.NameParser().parse("[Moozzi2] show name II - subtitle here - 15 (BD 1920x1080 x264 FLAC)")
        self.assertTrue(result.ambiguous)
        self.assertTrue(result.franchise_conflict)
        self.assertIsNone(result.season_number)

    def test_custom_pin_resolves_the_conflict_to_the_right_season(self):
        self._seed(
            [
                ("show name II - subtitle here", 1, 0),
                ("show name II subtitle here", 2, 0),
                ("show name II - subtitle here", 2, scene_exceptions.CUSTOM_BLESSED),
            ]
        )
        result = parser.NameParser().parse("[Moozzi2] show name II - subtitle here - 15 (BD 1920x1080 x264 FLAC)")
        self.assertFalse(result.ambiguous)
        self.assertEqual(result.season_number, 2)
        self.assertEqual(result.episode_numbers, [15])

    def test_kon_single_season_pin_still_maps(self):
        self._seed([("show name!!", 2, 0), ("show name!", 1, 0)])
        result = parser.NameParser().parse("[Moozzi2] show name!! - 05 (BD 1920x1080 x264 FLAC)")
        self.assertEqual(result.season_number, 2)
        self.assertEqual(result.episode_numbers, [5])

    def test_unexplained_year_refuses_a_sxxeyy_mapping(self):
        # Forced show (PP-style), so the anime SxxEyy branch is reached with a year the show
        # cannot explain -- the F3 case that branch-local guards would have missed.
        result = parser.NameParser(show_object=self.show).parse("show name (2015) S02E01 720p")
        self.assertTrue(result.ambiguous)

    def test_clean_sxxeyy_mapping_still_works(self):
        result = parser.NameParser(show_object=self.show).parse("show name S02E01 720p")
        self.assertFalse(result.ambiguous)
        self.assertEqual(result.season_number, 2)
        self.assertEqual(result.episode_numbers, [1])

    def test_ajin_title_season_resolution_still_clears_ambiguity(self):
        # The pre-existing resolver must keep working: "show name 2 - 12" resolves to S2E12
        # (both exist on the fixture show) and the chokepoint then allows it.
        result = parser.NameParser().parse("[Moozzi2] show name 2 - 12 (BD 1920x1080 x264 FLAC)")
        self.assertFalse(result.ambiguous)
        self.assertEqual(result.season_number, 2)
        self.assertEqual(result.episode_numbers, [12])

    def test_a_veto_clears_the_mapped_numbers(self):
        """Consumers that never learned about `ambiguous` (nzbSplitter, proper finder) must not
        find an actionable mapping on a vetoed result."""
        result = parser.NameParser(show_object=self.show).parse("show name (2015) S02E01 720p")
        self.assertTrue(result.ambiguous)
        self.assertIsNone(result.season_number)
        self.assertEqual(result.episode_numbers, [])
        self.assertEqual(result.ab_episode_numbers, [])

    def test_conflicted_family_clears_the_raw_numbers_too(self):
        self._seed(
            [
                ("show name II - subtitle here", 1, 0),
                ("show name II subtitle here", 2, 0),
            ]
        )
        result = parser.NameParser().parse("[Moozzi2] show name II - subtitle here - 15 (BD 1920x1080 x264 FLAC)")
        self.assertTrue(result.franchise_conflict)
        self.assertEqual(result.episode_numbers, [])
        self.assertEqual(result.ab_episode_numbers, [])

    def test_scene_divergent_token_compares_in_scene_space(self):
        """Scene/indexer divergence (R2-4/R4-1): indexer S3E5 is scene S2E5. A release naming the
        SCENE season (S2) must be allowed to map to indexer S3E5, and a release naming a season
        that is neither must be vetoed."""
        self.show.scene = 1
        self.show.save_to_db()
        main_db = db.DBConnection()
        main_db.action(
            "INSERT OR REPLACE INTO scene_numbering "
            "(indexer, indexer_id, season, episode, absolute_number, scene_season, scene_episode, scene_absolute_number) "
            "VALUES (1, 1, 3, 5, 55, 2, 5, 25)",
        )
        self._seed([("show name II", 3, 0)])

        # Token S2 == the mapped episode's scene season -> allowed.
        allowed = parser.NameParser().parse("[Grp] show name II - 05 (S2) (BD 1920x1080 x264 FLAC)")
        self.assertFalse(allowed.ambiguous)
        self.assertEqual(allowed.season_number, 3)
        self.assertEqual(allowed.episode_numbers, [5])

        # Token S4 matches neither the indexer nor the scene season -> vetoed.
        vetoed = parser.NameParser().parse("[Grp] show name II - 05 (S4) (BD 1920x1080 x264 FLAC)")
        self.assertTrue(vetoed.ambiguous)
        self.assertEqual(vetoed.episode_numbers, [])

    def test_parse_cache_is_invalidated_by_an_exceptions_generation_bump(self):
        name = "[Judas] show name - 5 [1080p]"
        first = parser.NameParser().parse(name)
        self.assertIsNotNone(parser.name_parser_cache[name])
        scene_exceptions.invalidate_derived_caches()
        self.assertIsNone(parser.name_parser_cache[name])
        self.assertIsNotNone(first)  # the stale result object itself is untouched, just not served


class AliasIndexTests(conftest.ResetNameCacheMixin, conftest.SickChillTestPostProcessorCase):
    def test_one_db_read_per_show_per_generation(self):
        scene_exceptions.invalidate_derived_caches()
        real = scene_exceptions.db.DBConnection
        with mock.patch.object(scene_exceptions.db, "DBConnection", side_effect=real) as counted:
            scene_exceptions.get_normalized_alias_index(1)
            scene_exceptions.get_normalized_alias_index(1)
            scene_exceptions.get_normalized_alias_index(1)
            self.assertEqual(counted.call_count, 1)
        scene_exceptions.invalidate_derived_caches()
        with mock.patch.object(scene_exceptions.db, "DBConnection", side_effect=real) as counted:
            scene_exceptions.get_normalized_alias_index(1)
            self.assertEqual(counted.call_count, 1)


class CustomExceptionLifecycleTests(conftest.ResetNameCacheMixin, conftest.SickChillTestPostProcessorCase):
    """Tri-state custom flag: user rows delete, blessed rows downgrade, sync never displaces."""

    def _rows(self):
        cache_db = db.DBConnection("cache.db")
        return {
            (row["show_name"], int(row["season"])): int(row["custom"])
            for row in cache_db.select("SELECT show_name, season, custom FROM scene_exceptions WHERE indexer_id = 1")
        }

    def _seed_synced(self, show_name, season):
        cache_db = db.DBConnection("cache.db")
        cache_db.action("INSERT INTO scene_exceptions (indexer_id, show_name, season, custom) VALUES (1, ?, ?, 0)", [show_name, season])

    def test_blessing_upgrades_a_synced_row_in_place(self):
        self._seed_synced("alias one", 2)
        scene_exceptions.update_custom_scene_exceptions(1, {2: [{"show_name": "alias one", "custom": scene_exceptions.CUSTOM_BLESSED}]})
        self.assertEqual(self._rows()[("alias one", 2)], scene_exceptions.CUSTOM_BLESSED)

    def test_unblessing_downgrades_but_never_deletes(self):
        self._seed_synced("alias one", 2)
        scene_exceptions.update_custom_scene_exceptions(1, {2: [{"show_name": "alias one", "custom": scene_exceptions.CUSTOM_BLESSED}]})
        scene_exceptions.update_custom_scene_exceptions(1, {})
        self.assertEqual(self._rows()[("alias one", 2)], 0)

    def test_a_user_row_absent_from_the_submission_is_deleted(self):
        scene_exceptions.update_custom_scene_exceptions(1, {2: [{"show_name": "my own name", "custom": scene_exceptions.CUSTOM_USER}]})
        self.assertEqual(self._rows()[("my own name", 2)], scene_exceptions.CUSTOM_USER)
        scene_exceptions.update_custom_scene_exceptions(1, {})
        self.assertNotIn(("my own name", 2), self._rows())

    def test_a_user_submission_equal_to_a_synced_row_blesses_it(self):
        self._seed_synced("alias one", 2)
        scene_exceptions.update_custom_scene_exceptions(1, {2: [{"show_name": "alias one", "custom": scene_exceptions.CUSTOM_USER}]})
        self.assertEqual(self._rows()[("alias one", 2)], scene_exceptions.CUSTOM_BLESSED)

    def test_pin_payload_round_trip_blesses_and_unpins(self):
        """The wire format the JS submits (handler-level payload test): pin -> bless, then a
        submission without the blessed entry -> downgrade to synced."""
        self._seed_synced("alias one", 2)

        payload = scene_exceptions.parse_exceptions_payload("2:my+own+name", "2:alias+one")
        self.assertEqual(
            payload,
            {
                2: [
                    {"show_name": "my own name", "custom": scene_exceptions.CUSTOM_USER},
                    {"show_name": "alias one", "custom": scene_exceptions.CUSTOM_BLESSED},
                ]
            },
        )
        scene_exceptions.update_custom_scene_exceptions(1, payload)
        rows = self._rows()
        self.assertEqual(rows[("alias one", 2)], scene_exceptions.CUSTOM_BLESSED)
        self.assertEqual(rows[("my own name", 2)], scene_exceptions.CUSTOM_USER)

        # Unpin: same custom entry, no blessed entry.
        scene_exceptions.update_custom_scene_exceptions(1, scene_exceptions.parse_exceptions_payload("2:my+own+name", None))
        rows = self._rows()
        self.assertEqual(rows[("alias one", 2)], 0, "unpinning downgrades, never deletes")
        self.assertEqual(rows[("my own name", 2)], scene_exceptions.CUSTOM_USER)

    def test_a_blessed_row_survives_a_full_sync_cycle(self):
        self._seed_synced("alias one", 2)
        scene_exceptions.update_custom_scene_exceptions(1, {2: [{"show_name": "alias one", "custom": scene_exceptions.CUSTOM_BLESSED}]})

        def synced_rows():
            yield 1, "alias one", 2
            yield 1, "alias two", 1

        with mock.patch.object(scene_exceptions, "_sickchill_exceptions_generator", side_effect=lambda: synced_rows()):
            with mock.patch.object(scene_exceptions, "_xem_exceptions_generator", side_effect=lambda: iter(())):
                with mock.patch.object(scene_exceptions, "_anidb_exceptions_generator", side_effect=lambda: iter(())):
                    scene_exceptions.retrieve_exceptions()
                    scene_exceptions.retrieve_exceptions()

        rows = self._rows()
        self.assertEqual(rows[("alias one", 2)], scene_exceptions.CUSTOM_BLESSED, "sync must not displace the user's pin")
        self.assertEqual(rows[("alias two", 1)], 0)
        # And INSERT OR IGNORE really ignored: one row each, no duplicate growth.
        cache_db = db.DBConnection("cache.db")
        count = cache_db.select_one("SELECT COUNT(*) AS c FROM scene_exceptions WHERE indexer_id = 1 AND show_name = 'alias one'")
        self.assertEqual(int(count["c"]), 1)


class MassEditRetentionTests(unittest.TestCase):
    """A direct mass-edit call never submits exception data; reconciling its empty payload would
    wipe custom rows and downgrade blessed pins (the franchise-collision overrides)."""

    def test_direct_call_without_payload_does_not_reconcile(self):
        from sickchill.views.home import _should_reconcile_exceptions

        self.assertFalse(_should_reconcile_exceptions(True, None, None))
        self.assertFalse(_should_reconcile_exceptions(True, [], None))
        self.assertFalse(_should_reconcile_exceptions(True, "", ""))

    def test_the_edit_form_always_reconciles(self):
        from sickchill.views.home import _should_reconcile_exceptions

        # The real form posts the fields even when empty -- deliberate clear-all still works.
        self.assertTrue(_should_reconcile_exceptions(False, "", ""))
        self.assertTrue(_should_reconcile_exceptions(False, None, None))
        self.assertTrue(_should_reconcile_exceptions(True, "2:name", None))
        self.assertTrue(_should_reconcile_exceptions(True, None, "2:alias+one"))


class SceneExceptionsMigrationTests(conftest.SickChillTestDBCase):
    """The dedupe + unique-index migration, exercised against a polluted legacy table."""

    def _connection(self):
        return db.DBConnection("cache.db")

    def test_migration_dedupes_prefers_custom_and_is_idempotent(self):
        from sickchill.oldbeard.databases.cache import SceneExceptionsUnique

        connection = self._connection()
        connection.action("DROP INDEX IF EXISTS idx_scene_exceptions_unique")
        connection.action("DELETE FROM scene_exceptions WHERE indexer_id = 424242")
        for _ in range(3):
            connection.action("INSERT INTO scene_exceptions (indexer_id, show_name, season, custom) VALUES (424242, 'dupe name', 2, 0)")
        connection.action("INSERT INTO scene_exceptions (indexer_id, show_name, season, custom) VALUES (424242, 'dupe name', 2, 2)")

        migration = SceneExceptionsUnique(connection)
        self.assertFalse(migration.test())
        migration.execute()
        self.assertTrue(migration.test())

        rows = connection.select("SELECT custom FROM scene_exceptions WHERE indexer_id = 424242 AND show_name = 'dupe name' AND season = 2")
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0]["custom"]), 2, "the user's row wins the dedupe")

        # Idempotent, and OR IGNORE now actually ignores.
        migration.execute()
        self.assertTrue(migration.test())
        connection.action("INSERT OR IGNORE INTO scene_exceptions (indexer_id, show_name, season, custom) VALUES (424242, 'dupe name', 2, 0)")
        rows = connection.select("SELECT custom FROM scene_exceptions WHERE indexer_id = 424242 AND show_name = 'dupe name' AND season = 2")
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0]["custom"]), 2)
        connection.action("DELETE FROM scene_exceptions WHERE indexer_id = 424242")


class SearchMatcherVetoTests(conftest.ResetNameCacheMixin, conftest.SickChillTestPostProcessorCase):
    """The AI search matcher's deterministic vetoes -- the prompt is advice, these are the boundary."""

    def setUp(self):
        super().setUp()
        self.show.anime = 1
        self.show.startyear = 2005
        self.show.save_to_db()
        self.episode = self.show.get_episode(2, 1)

    def _validate(self, title, season=2, episode=1, confidence=0.95):
        from sickchill.oldbeard.ai import search_matcher

        items = [{"title": title, "url": "http://example.com/x"}]
        response = {"matches": [{"index": 0, "season": season, "episode": episode, "confidence": confidence}]}
        with mock.patch.object(search_matcher, "get_preferences_manager") as prefs:
            prefs.return_value.get_confidence_threshold.return_value = 0.85
            return search_matcher._validate_matches(response, items, [self.episode], self.show)

    def test_the_gintama_regression_is_rejected(self):
        # Both live name forms, at the exact confidences the AI used (0.92 / 0.95).
        self.assertEqual(self._validate("[Abystoma] show name (2015)-50 (BD 1280x720 x264 AAC) [1AB0376E]", confidence=0.92), [])
        self.assertEqual(self._validate("[Abystoma].show name.2015-50.BD.1280x720.x264.AAC.[1AB0376E]", confidence=0.95), [])

    def test_a_clean_absolute_release_is_accepted(self):
        matches = self._validate("[Judas] show name - 50 [1080p]")
        self.assertEqual(len(matches), 1)
        self.assertEqual((matches[0]["season"], matches[0]["episode"]), (2, 1))

    def test_an_explicit_token_for_another_season_is_rejected(self):
        self.assertEqual(self._validate("[Grp] show name S3 - 01 [1080p]"), [])

    def test_a_marker_with_no_exception_data_is_rejected_fail_closed(self):
        self.assertEqual(self._validate("[Moozzi2] show name II - 01 (BD 1080p)"), [])


class ReleaseGroupExtractionTests(unittest.TestCase):
    def test_bracket_group_is_extracted(self):
        from sickchill.providers.GenericProvider import GenericProvider

        self.assertEqual(GenericProvider._extract_release_group("[Abystoma] Gintama (2015)-50 (BD 1280x720 x264 AAC) [1AB0376E]"), "Abystoma")

    def test_no_group_returns_empty(self):
        from sickchill.providers.GenericProvider import GenericProvider

        self.assertEqual(GenericProvider._extract_release_group("Some.Show.S01E01.720p"), "")


class CacheGateTests(conftest.ResetNameCacheMixin, conftest.SickChillTestPostProcessorCase):
    """Pre-fix poisoned rows are filtered on the way OUT of the cache, incl. the manual paths."""

    _COLUMNS = ("provider", "name", "season", "episodes", "indexerid", "url", "time", "quality", "release_group", "version", "seeders", "leechers", "size")

    def setUp(self):
        super().setUp()
        # Other test modules leave global word filters behind; this class tests the franchise
        # gate, not filter_bad_releases.
        settings.REQUIRE_WORDS = ""
        settings.IGNORE_WORDS = ""
        settings.PREFER_WORDS = ""
        self.show.anime = 1
        self.show.startyear = 2005
        self.show.quality = common.Quality.combineQualities([common.Quality.FULLHDBLURAY], [])
        self.show.save_to_db()
        self.episode = self.show.get_episode(2, 1)
        self.episode.status = common.WANTED
        self.episode.save_to_db()
        from tests.test_anime_search import _make_provider

        self.provider = _make_provider()

    def _cache_row(self, name, season=2, episodes="|1|"):
        cache_db = self.provider.cache.get_db()
        values = {
            "provider": self.provider.cache.provider_id,
            "name": name,
            "season": season,
            "episodes": episodes,
            "indexerid": 1,
            "url": "http://example.com/" + name,
            "time": 1,
            "quality": common.Quality.FULLHDBLURAY,
            "release_group": "",
            "version": -1,
            "seeders": -1,
            "leechers": -1,
            "size": -1,
        }
        cache_db.action(
            "INSERT INTO results ({0}) VALUES ({1})".format(", ".join(self._COLUMNS), ", ".join("?" for _ in self._COLUMNS)),
            [values[column] for column in self._COLUMNS],
        )

    def test_a_year_collision_row_is_not_served(self):
        self._cache_row("[Abystoma] show name (2015)-50 (BD 1920x1080 x264 FLAC)")
        needed = self.provider.cache.find_needed_episodes(self.episode)
        self.assertEqual(needed.get(self.episode, []), [])

    def test_a_clean_row_is_still_served(self):
        self._cache_row("[Judas] show name - 50 [1080p BluRay]")
        needed = self.provider.cache.find_needed_episodes(self.episode)
        self.assertTrue(needed.get(self.episode))

    def test_gate_cached_anime_row_is_the_shared_helper(self):
        from sickchill.oldbeard.tvcache import gate_cached_anime_row

        self.assertFalse(gate_cached_anime_row("[Abystoma] show name (2015)-50 (BD)", self.show, 2, None))
        self.assertTrue(gate_cached_anime_row("[Judas] show name - 50 [1080p]", self.show, 2, None))

    def test_add_cache_entry_refuses_an_ambiguous_anime_parse(self):
        parse_result = mock.MagicMock()
        parse_result.ambiguous = True
        parse_result.ambiguity_reason = "conflicting season tags"
        parse_result.show.is_anime = True
        self.assertIsNone(self.provider.cache.add_cache_entry("x", "http://example.com/x", -1, -1, -1, parse_result=parse_result))

    def test_add_cache_entry_keeps_non_anime_ambiguous_behavior(self):
        parse_result = mock.MagicMock()
        parse_result.ambiguous = True
        parse_result.show.is_anime = False
        parse_result.season_number = 1
        parse_result.episode_numbers = [2]
        parse_result.show.indexerid = 1
        parse_result.quality = common.Quality.SDTV
        parse_result.release_group = ""
        parse_result.version = -1
        self.assertIsNotNone(self.provider.cache.add_cache_entry("x", "http://example.com/x", -1, -1, -1, parse_result=parse_result))


class PostProcessorIdentityGateTests(conftest.ResetNameCacheMixin, conftest.SickChillTestPostProcessorCase):
    """_find_info validates every completed candidate and the exit, with rollback in between."""

    def setUp(self):
        super().setUp()
        self.show.anime = 1
        self.show.startyear = 2005
        self.show.save_to_db()

    def _post_processor(self, filename, release_name=None):
        from sickchill.oldbeard.postProcessor import PostProcessor

        return PostProcessor(os.path.join(conftest.FILE_DIR, "sub", filename), release_name=release_name)

    def test_validate_identity_rejects_a_nonexistent_episode(self):
        processor = self._post_processor("show name - s01e05.mkv")
        self.assertIn("does not exist", processor._validate_identity(self.show, 1, [99]))

    def test_validate_identity_rejects_a_franchise_violating_release_name(self):
        processor = self._post_processor("payload.mkv", release_name="[Abystoma] show name (2015) - 50 (BD 720p)")
        rejection = processor._validate_identity(self.show, 2, [1])
        self.assertIsNotNone(rejection)
        self.assertIn("release name", rejection)

    def test_validate_identity_accepts_a_clean_identity(self):
        processor = self._post_processor("show name - s02e01.mkv", release_name="[Judas] show name - 50 [1080p]")
        self.assertIsNone(processor._validate_identity(self.show, 2, [1]))

    def test_non_anime_shows_skip_the_franchise_check(self):
        self.show.anime = 0
        processor = self._post_processor("payload.mkv", release_name="Something (2015) S02E01")
        self.assertIsNone(processor._validate_identity(self.show, 2, [1]))

    def test_find_info_rolls_back_a_rejected_source_and_falls_through(self):
        # History proposes S1E1 for a release whose name says S2 (veto); the release-name parse
        # then proposes S2E1 (allowed). The rejected candidate must not survive nor block the
        # fallback.
        processor = self._post_processor("payload.mkv", release_name="[Grp] show name S2 - 01 (BD 1080p)")
        attempts = [
            (self.show, 1, [1], None, None),
            (self.show, 2, [1], None, None),
        ]
        processor._history_lookup = lambda: attempts[0]
        processor._analyze_name = lambda name: attempts.pop(1) if len(attempts) > 1 and name == processor.release_name else (None, None, [], None, None)

        show, season, episodes, quality, version = processor._find_info()
        self.assertEqual((season, episodes), (2, [1]))

    def test_find_info_exit_belt_refuses_a_poisoned_terminal_identity(self):
        processor = self._post_processor("payload.mkv", release_name="[Abystoma] show name (2015) - 50 (BD 720p)")
        processor._history_lookup = lambda: (None, None, [], None, None)
        processor._analyze_name = lambda name: (None, None, [], None, None)
        with mock.patch("sickchill.oldbeard.postProcessor._get_ai_match_result") as ai:
            ai.return_value = {"show_indexer_id": 1, "season": 2, "episodes": [1], "confidence": 0.95}
            show, season, episodes, quality, version = processor._find_info()
        self.assertIsNone(show)
        self.assertEqual(episodes, [])


if __name__ == "__main__":
    unittest.main()
