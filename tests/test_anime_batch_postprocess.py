"""
Regression tests for the anime batch post-processing loop.

Auto post-processing rescanned the download folder every 2 minutes and re-failed on the same files
forever. Five defects combined to cause it, and each is guarded here:

1. The episode-number guard in the regexes was written as an ALTERNATION -- ((?!A)|(?!B)) -- which
   is satisfied whenever EITHER lookahead passes, so it never blocked anything. "H265" parsed as
   absolute episode 265, "1080p" as episode 1080, and "x.264" as season 2 episode 64.
2. Absolute episode ranges were uncapped, so a batch folder ("Ajin 2-12") expanded to an 11-episode
   span whose destination filename concatenated 11 episode titles -> [Errno 36] Filename too long.
3. "<Title> N - <episode>" (the fansub per-season Blu-ray form) read the season as the episode and
   silently discarded the real one, which would file season 2 episode 12 into season 1 episode 2.
4. Creditless openings, commercials, promos and disc menus have no episode number, so they fell back
   to the batch folder's name and were filed as the whole season.
5. Nothing remembered a permanent failure, so every pass redid the full parse/probe/AI/hash work.

The names below are the real ones taken from the failing install.
"""

from __future__ import annotations

import os
import time
import unittest
from unittest import mock


class FakeShow:
    """Minimal stand-in for TVShow: enough for the parser's show/season/episode checks."""

    def __init__(self, name, indexerid, seasons):
        self.name = name
        self.indexerid = indexerid
        self.indexer = 1
        self.is_anime = True
        self.is_scene = False
        self.seasons = seasons  # {season_number: episode_count}

    def __repr__(self):
        return f"<FakeShow {self.name}>"


AJIN = FakeShow("Ajin", 300835, {1: 13, 2: 13})
DATE_A_LIVE = FakeShow("Date A Live IV", 111, {1: 12})
ONE_PIECE = FakeShow("One Piece", 222, {1: 1200})
BLEACH = FakeShow("Bleach", 333, {1: 20, 2: 20})

SHOWS = {show.name.lower(): show for show in (AJIN, DATE_A_LIVE, ONE_PIECE, BLEACH)}
SHOWS_BY_ID = {show.indexerid: show for show in SHOWS.values()}


class FakeDBConnection:
    """Answers only the (show, season, episode) existence query _resolve_title_season makes."""

    def select_one(self, sql, args):
        showid, _indexer, season, episode = args
        show = SHOWS_BY_ID.get(showid)
        if not show:
            return None
        return [1] if season in show.seasons and 1 <= episode <= show.seasons[season] else None

    def select(self, *args, **kwargs):
        return []


def _fake_get_show(name, *args, **kwargs):
    return SHOWS.get((name or "").strip().lower())


def _fake_absolute(show, season, episode):
    """Absolute number = every episode in the earlier seasons, plus this one."""
    return sum(show.seasons[s] for s in sorted(show.seasons) if s < season) + episode


def parse_anime(name, parse_method="anime"):
    """
    Parse an anime release name against the fake show database above.

    ``parse_method=None`` compiles BOTH the normal and anime regex sets, which is what
    ``guessit_findit`` does when it cannot resolve the show up front. Both sets must agree.
    """
    from sickchill.oldbeard.name_parser import parser

    with mock.patch.object(parser.helpers, "get_show", side_effect=_fake_get_show), mock.patch.object(
        parser.db, "DBConnection", return_value=FakeDBConnection()
    ), mock.patch.object(parser.helpers, "get_absolute_number_from_season_and_episode", side_effect=_fake_absolute), mock.patch.object(
        parser.helpers, "get_all_episodes_from_absolute_number", side_effect=lambda show, numbers: (1, list(numbers))
    ), mock.patch.object(
        parser.scene_exceptions, "get_scene_exception_by_name", return_value=(None, None)
    ), mock.patch.object(
        parser.scene_exceptions, "get_scene_exception_by_name_multiple", return_value=[]
    ), mock.patch.object(
        parser.common.Quality, "nameQuality", return_value=0
    ):
        parser.name_parser_cache.data.clear()
        return parser.NameParser(filename=True, parse_method=parse_method).parse(name, cache_result=False)


class TestNumberGuard(unittest.TestCase):
    """Codec, resolution and dimension tokens must never be read as episode numbers."""

    def test_guard_is_a_conjunction_not_an_alternation(self):
        from sickchill.oldbeard.name_parser import regexes

        # The old guard read "((?!A)|(?!B))". An alternation of negative lookaheads is satisfied by
        # either branch, making it a no-op. Every guard must apply, so none may be alternated.
        self.assertNotIn("|(?![hx].?26[45])", regexes.NUM_GUARD)
        self.assertIn("(?<![0-9])", regexes.NUM_GUARD)

    def test_h265_is_not_absolute_episode_265(self):
        result = parse_anime("[Erai-raws] One Piece - 1168 [H265][1080p]")
        self.assertEqual(result.ab_episode_numbers, [1168])
        self.assertNotIn(265, result.ab_episode_numbers)

    def test_resolution_is_not_an_episode_number(self):
        result = parse_anime("[Erai-raws] One Piece - 1168 [1080p][CR][WEB-DL]")
        self.assertEqual(result.ab_episode_numbers, [1168])
        self.assertNotIn(1080, result.ab_episode_numbers)

    def test_x264_is_not_season_2_episode_64(self):
        # "Ajin 2 - 12 (BD 1920x1080 x.264 ...)": the `bare` regex used to take "264" out of "x.264"
        # as season 2 / episode 64, producing a show name that could never resolve.
        result = parse_anime("[Moozzi2] Ajin 2 - 12 (BD 1920x1080 x.264 FLACx2)")
        self.assertNotEqual((result.season_number, result.episode_numbers), (2, [64]))

    def test_show_name_ending_in_x_still_parses(self):
        # The codec guard must not fire on a title that merely ends in "x" or "h". It only fires when
        # the digits really are 264/265, so "Show.Max.102" and even a standalone "The.X.102" survive.
        import re

        from sickchill.oldbeard.name_parser import regexes

        bare = dict(regexes.normal_regexes)["bare"]
        for name in ("Show.Max.102.Source.Quality.Etc-Group", "The.X.102.Source.Quality.Etc-Group", "The.H.102.Source.Quality.Etc-Group"):
            match = re.match(bare, name, re.VERBOSE | re.IGNORECASE)
            self.assertIsNotNone(match, f"a show name ending in 'x'/'h' must still parse: {name}")
            self.assertEqual((match.group("season_num"), match.group("ep_num")), ("1", "02"), name)

    def test_every_absolute_episode_group_is_guarded(self):
        # anime_anidb carried its own hand-rolled copy of the broken guard and was missed by the
        # bulk replacement, leaving "[G] Show - 1920x1080 [1080p]" parsable as episode 1920.
        import re

        from sickchill.oldbeard.name_parser import regexes

        for name, pattern in regexes.anime_regexes:
            for group in ("ep_ab_num", "extra_ab_ep_num"):
                marker = f"(?P<{group}>"
                if marker not in pattern:
                    continue
                # Whatever grouping parens wrap it, the guard must come before the digits it guards.
                body = pattern.split(marker, 1)[1]
                guard_at = body.find(regexes.NUM_GUARD)
                digits_at = body.find(r"\d")
                self.assertNotEqual(guard_at, -1, f"{name}: {group} is not protected by NUM_GUARD")
                self.assertLess(guard_at, digits_at, f"{name}: NUM_GUARD must precede {group}'s digits")

        compiled = dict((n, re.compile(p, re.VERBOSE | re.IGNORECASE)) for n, p in regexes.anime_regexes)

        # Before the guard, anime_anidb read the WIDTH of a dimension as the episode number.
        self.assertIsNone(compiled["anime_anidb"].match("[G] Show - 1920x1080 [1080p]"))
        # ...while the names it is meant to handle are untouched.
        self.assertEqual(compiled["anime_anidb"].match("[Erai-raws] One Piece - 1168 [1080p][CR]").group("ep_ab_num"), "1168")
        self.assertEqual(compiled["anime_standard"].match("[Group Name] Show Name - 13 [1080p]").group("ep_ab_num"), "13")
        self.assertEqual(compiled["anime_french_fansub"].match("[Group] Show Name - 07 VOSTFR [1080p]").group("ep_ab_num"), "07")

    def test_start_anchored_pattern_survives_the_lookbehinds(self):
        # anime_WarB3asT matches at position 0, where every fixed-width lookbehind has nothing to
        # look at. A negative lookbehind with no room to match must succeed, not abort the pattern.
        import re

        from sickchill.oldbeard.name_parser import regexes

        war = re.compile(dict(regexes.anime_regexes)["anime_WarB3asT"], re.VERBOSE | re.IGNORECASE)
        self.assertEqual(war.match("003. Show Name - Ep Name").group("ep_ab_num"), "003")
        self.assertEqual(war.match("003-004. Show Name - Ep Name").group("extra_ab_ep_num"), "004")

    def test_dimension_halves_are_not_episodes(self):
        import re

        from sickchill.oldbeard.name_parser import regexes

        guarded = re.compile(regexes.NUM_GUARD + r"\d{1,4}", re.VERBOSE | re.IGNORECASE)

        # Neither half of "1920x1080" may be taken as a number: the width is blocked by the WxH
        # lookahead, the height by the "preceded by <digit>x" lookbehind.
        self.assertIsNone(guarded.match("1920x1080"))
        self.assertIsNone(guarded.match("1920x1080", 5))

        # A codec suffix is blocked wherever it really appears -- always after a separator.
        self.assertIsNone(guarded.search(" x264"))
        self.assertIsNone(guarded.search(" x.264"))
        self.assertIsNone(guarded.search("[H265]"))
        self.assertIsNone(guarded.search("[1080p]"))

        # A bare four-digit number IS a legitimate absolute episode number ("One Piece - 1168").
        self.assertIsNotNone(guarded.match("1168"))


class TestAbsoluteRangeCap(unittest.TestCase):
    """A batch folder is not a multi-episode file."""

    def test_absolute_range_cap_matches_episode_range_cap(self):
        from sickchill.oldbeard.name_parser import parser

        self.assertEqual(parser.MAX_MULTI_EPISODES, 4)

    def test_batch_folder_does_not_expand_to_eleven_episodes(self):
        # "[Moozzi2] Ajin 2-12" used to yield absolute episodes 2..12, and the destination filename
        # concatenated all eleven episode titles -> [Errno 36] Filename too long.
        result = parse_anime("[Moozzi2] Ajin 2-12 [BD 1920x1080 x 264 FLACx2]")
        self.assertLessEqual(len(result.episode_numbers), 4)
        self.assertLessEqual(len(result.ab_episode_numbers), 4)

    def test_twelve_episode_batch_folder_does_not_expand(self):
        result = parse_anime("[Nii-sama] Date A Live IV-1-12 [1080p][BD][Batch][HEVC][10bit][FLAC]")
        self.assertLessEqual(len(result.episode_numbers), 4)

    def test_genuine_two_episode_range_is_preserved(self):
        result = parse_anime("[ShinBunBu-Subs] Bleach - 02-03 (CX 1280x720 x264 AAC)")
        self.assertEqual(result.episode_numbers, [2, 3])
        self.assertFalse(result.ambiguous)


class TestTitleSeasonResolution(unittest.TestCase):
    """`<Title> N - <episode>` is the fansub per-season form."""

    def test_moozzi2_season_release_maps_to_the_right_season(self):
        # The real file: Ajin season 2 episode 12, whose library name is "Ajin - s02e12 - 025".
        result = parse_anime("[Moozzi2] Ajin 2 - 12 (BD 1920x1080 x.264 FLACx2)")
        self.assertFalse(result.ambiguous, "a DB-verified season reading is not ambiguous")
        self.assertEqual(result.season_number, 2)
        self.assertEqual(result.episode_numbers, [12])
        self.assertEqual(result.ab_episode_numbers, [25])

    def test_both_regex_sets_agree(self):
        # guessit_findit uses a show-scoped parser (anime regexes only) when it can resolve the show,
        # and an all-regexes parser when it cannot. The two must not disagree about which episode
        # this is, or the outcome would depend on whether guessit happened to recognise the title.
        for parse_method in ("anime", None):
            with self.subTest(parse_method=parse_method or "all"):
                episode = parse_anime("[Moozzi2] Ajin 2 - 12 (BD 1920x1080 x.264 FLACx2)", parse_method)
                self.assertEqual((episode.season_number, episode.episode_numbers, episode.ab_episode_numbers), (2, [12], [25]))
                self.assertFalse(episode.ambiguous)

                folder = parse_anime("[Moozzi2] Ajin 2-12 [BD 1920x1080 x 264 FLACx2]", parse_method)
                self.assertTrue(folder.ambiguous)

    def test_season_reading_is_refused_when_the_episode_does_not_exist(self):
        # Ajin has 13 episodes per season, so season 2 episode 99 cannot be confirmed.
        result = parse_anime("[Moozzi2] Ajin 2 - 99 (BD 1920x1080 x.264 FLACx2)")
        self.assertTrue(result.ambiguous, "an unverifiable season reading must stay ambiguous")

    def test_bare_hyphen_batch_range_is_never_read_as_a_season(self):
        # "Ajin 2-12" (no spaces) is a batch range, not "season 2 episode 12". It must not resolve.
        result = parse_anime("[Moozzi2] Ajin 2-12 [BD 1920x1080 x 264 FLACx2]")
        self.assertTrue(result.ambiguous)
        self.assertNotEqual((result.season_number, result.episode_numbers), (2, [12]))

    def test_season_number_above_the_cap_is_not_a_season(self):
        # "Mob Psycho 100 - 05": 100 is part of the title, not a season.
        from sickchill.oldbeard.name_parser import parser

        self.assertEqual(parser.MAX_TITLE_SEASON, 9)

    def test_season_one_is_never_read_from_the_title(self):
        # "Show 1 - 12" is overwhelmingly a 1-12 batch. A real season-1 release is just "Show - 12".
        from sickchill.oldbeard.name_parser import parser

        self.assertEqual(parser.MIN_TITLE_SEASON, 2)

        result = parse_anime("[Nii-sama] Date A Live IV 1 - 12 (BD 1920x1080)")
        self.assertTrue(result.ambiguous, "a season-1 'batch' shape must not collapse to one episode")

    def test_batch_marker_keeps_a_spaced_name_ambiguous(self):
        # "[Moozzi2] Ajin 2 - 12 (BD Batch ...)" is all of season 2, not episode 12. The database can
        # confirm S02E12 exists; it cannot tell us the release holds only that episode. The control
        # below (no marker) resolves, so these assertions really are testing the marker.
        self.assertFalse(parse_anime("[Moozzi2] Ajin 2 - 12 (BD 1920x1080 x.264 FLACx2)").ambiguous)

        for marker in ("Batch", "Pack", "Complete", "Season", "Vol", "Collection", "BoxSet"):
            with self.subTest(marker=marker):
                result = parse_anime(f"[Moozzi2] Ajin 2 - 12 (BD {marker} 1920x1080 x.264)")
                self.assertTrue(result.ambiguous, f"{marker} must keep the name ambiguous")
                self.assertNotEqual((result.season_number, result.episode_numbers), (2, [12]))

    def test_batch_marker_regex_matches_only_whole_tokens(self):
        from sickchill.oldbeard.name_parser.parser import _BATCH_MARKER_RE

        for marker in ("[Batch]", "[Pack]", "[BoxSet]", "[Box-Set]", "[Collection]", " Complete ", ".Season.", ".Seasons.", "-Vol-", " S01-S03 "):
            self.assertIsNotNone(_BATCH_MARKER_RE.search(marker), marker)
        # Substrings of ordinary words must not trip it.
        for benign in ("Seasoning", "Batchelor", "Volvo", "Completion", "Packard", "Backpack1"):
            self.assertIsNone(_BATCH_MARKER_RE.search(benign), benign)


class TestAmbiguityGuard(unittest.TestCase):
    """A discarded trailing number means we must not guess."""

    def test_discarded_number_marks_the_result_ambiguous(self):
        result = parse_anime("[Nii-sama] Date A Live IV-1-12 [1080p][BD][Batch][HEVC][10bit][FLAC]")
        self.assertTrue(result.ambiguous)
        self.assertEqual(result.discarded_number, 12)
        self.assertIn("season", result.ambiguity_reason)

    def test_numeric_episode_title_is_not_ambiguous(self):
        # "Show - 05 - 3 Days Later": the trailing 3 is an episode TITLE, not a discarded number,
        # because it is followed by a word rather than an info block.
        from sickchill.oldbeard.name_parser.parser import _DISCARDED_EPISODE_NUMBER_RE

        self.assertIsNone(_DISCARDED_EPISODE_NUMBER_RE.match(" - 3 Days Later"))
        self.assertIsNotNone(_DISCARDED_EPISODE_NUMBER_RE.match(" - 12 (BD 1920x1080)"))
        self.assertIsNotNone(_DISCARDED_EPISODE_NUMBER_RE.match("-12 [1080p]"))

    def test_resolution_suffix_is_not_a_discarded_number(self):
        from sickchill.oldbeard.name_parser.parser import _DISCARDED_EPISODE_NUMBER_RE

        self.assertIsNone(_DISCARDED_EPISODE_NUMBER_RE.match(" - 1080p BluRay"))

    def test_discarded_number_is_caught_after_a_bare_release_tag(self):
        # "Ajin 2 - 12 BD 1080p" has no bracket after the number, but "BD" is a release tag, so the
        # trailing 12 is still a discarded episode number rather than an episode title.
        from sickchill.oldbeard.name_parser.parser import _DISCARDED_EPISODE_NUMBER_RE

        for tail in (" - 12 BD 1080p", " - 12 1080p", " - 12 WEB-DL", " - 12 x264", " - 12 HEVC"):
            self.assertIsNotNone(_DISCARDED_EPISODE_NUMBER_RE.match(tail), tail)

        # ...but an ordinary word after the number still means it is an episode title. The bare-tag
        # branch must consume the WHOLE remainder, so a title merely STARTING with a tag word
        # ("Web of Lies", "BD Company") is not mistaken for release metadata.
        for tail in (" - 3 Days Later", " - 12 Angry Men", " - 7 Samurai", " - 12 Web of Lies", " - 12 BD Company Reunion"):
            self.assertIsNone(_DISCARDED_EPISODE_NUMBER_RE.match(tail), tail)

    def test_spaced_dash_flag_distinguishes_season_form_from_batch_range(self):
        from sickchill.oldbeard.name_parser.parser import _DISCARDED_EPISODE_NUMBER_RE

        spaced = _DISCARDED_EPISODE_NUMBER_RE.match(" - 12 (BD)")
        bare = _DISCARDED_EPISODE_NUMBER_RE.match("-12 [BD]")
        self.assertTrue(spaced.group("lead") and spaced.group("trail"))
        self.assertFalse(bare.group("lead") and bare.group("trail"))

    def test_clean_release_is_not_ambiguous(self):
        self.assertFalse(parse_anime("[Erai-raws] One Piece - 1168 [1080p][CR][WEB-DL]").ambiguous)

    def _processor(self):
        from sickchill.oldbeard import postProcessor

        processor = postProcessor.PostProcessor.__new__(postProcessor.PostProcessor)
        processor.log = ""
        processor.saw_ambiguous_name = False
        return processor

    def test_postprocessor_refuses_an_ambiguous_name(self):
        from sickchill.oldbeard import postProcessor

        processor = self._processor()
        ambiguous = mock.Mock(ambiguous=True, ambiguity_reason="the leading number may be a season")

        with mock.patch.object(postProcessor, "guessit_findit", return_value=ambiguous):
            show, season, episodes, quality, version = processor._analyze_name("[Moozzi2] Ajin 2-12 [BD]")

        self.assertIsNone(show, "an ambiguous name must not yield a show")
        self.assertEqual(episodes, [])
        self.assertTrue(processor.saw_ambiguous_name, "the refusal must be remembered for the whole pass")

    def test_ambiguity_blocks_the_ai_fallback_entirely(self):
        # Handing an ambiguous name to the AI is still guessing, and a wrong guess overwrites a real
        # episode. _find_info must refuse outright rather than fall through to AI matching.
        from sickchill.oldbeard import postProcessor

        processor = self._processor()
        processor.directory = "/downloads/[Moozzi2] Ajin 2-12 [BD]/file.mkv"
        processor.filename = "file.mkv"
        processor.folder_name = "[Moozzi2] Ajin 2-12 [BD]"
        processor.release_name = None
        processor.in_history = False
        processor._history_lookup = lambda: (None, None, [], None, None)

        ambiguous = mock.Mock(ambiguous=True, ambiguity_reason="the leading number may be a season")

        with mock.patch.object(postProcessor, "guessit_findit", return_value=ambiguous), mock.patch.object(
            postProcessor, "_get_ai_match_result"
        ) as ai_fallback:
            show, season, episodes, quality, version = processor._find_info()

        ai_fallback.assert_not_called()
        self.assertIsNone(show)
        self.assertIsNone(season)
        self.assertEqual(episodes, [])

    def test_ambiguous_refusal_reports_a_meaningful_reason(self):
        # A bare EpisodePostProcessingFailedException() renders as "Processing failed: " with no
        # reason at all, which is what made the original bug so hard to read in the logs.
        from sickchill.helper.exceptions import EpisodePostProcessingFailedException
        from sickchill.oldbeard import postProcessor

        processor = self._processor()
        processor.directory = "/downloads/[Moozzi2] Ajin 2-12 [BD]/file.mkv"
        processor.release_name = None
        processor.anidbEpisode = None
        processor.in_history = False

        with mock.patch.object(postProcessor.PostProcessor, "_find_info", side_effect=self._refuse(processor)), mock.patch(
            "os.path.exists", return_value=True
        ):
            with self.assertRaises(EpisodePostProcessingFailedException) as caught:
                processor.process()

        self.assertIn("mbiguous", str(caught.exception))

    @staticmethod
    def _refuse(processor):
        def _find_info():
            processor.saw_ambiguous_name = True
            return None, None, [], None, None

        return _find_info

    def test_ai_fallback_still_runs_for_an_ordinary_parse_failure(self):
        # The refusal must be specific to ambiguity, not to every failed parse.
        from sickchill.oldbeard import postProcessor

        processor = self._processor()
        processor.directory = "/downloads/mystery/file.mkv"
        processor.filename = "file.mkv"
        processor.folder_name = "mystery"
        processor.release_name = None
        processor.in_history = False
        processor._history_lookup = lambda: (None, None, [], None, None)

        with mock.patch.object(postProcessor, "guessit_findit", return_value=None), mock.patch.object(
            postProcessor, "_get_ai_match_result", return_value=None
        ) as ai_fallback:
            processor._find_info()

        ai_fallback.assert_called_once()


class TestAnimeExtras(unittest.TestCase):
    """Bonus content is not an episode."""

    EXTRAS = [
        "[Nii-sama] Date A Live IV NCOP - 01 [1080p][BD][HEVC][10bit][FLAC][8EBC8580].mkv",
        "[Nii-sama] Date A Live IV NCED - 01 [1080p][BD].mkv",
        "[Nii-sama] Date A Live IV CM - 01 [1080p][BD].mkv",
        "[Nii-sama] Date A Live IV PV - 04 [1080p][BD].mkv",
        "[Nii-sama] Date A Live IV BD1 Menu - 01 [1080p][BD].mkv",
        "[Rom & Rem] Isekai wa Smartphone to Tomo ni. - Creditless NCOP [BD][H265][1080p].mkv",
    ]

    EPISODES = [
        "[Nii-sama] Date A Live IV - 05 [1080p][BD][HEVC][10bit][FLAC].mkv",
        "[Moozzi2] Ajin 2 - 12 (BD 1920x1080 x.264 FLACx2).mkv",
        "[Erai-raws] One Piece - 1168 [1080p].mkv",
        "Show.Name.S01E02.1080p.WEB-DL.mkv",
        "Cowboy Bebop - 03 - Honky Tonk Women.mkv",
        # A real episode whose TITLE contains an extras-sounding word must still be processed.
        "Show - 05 - The Preview Menu Room.mkv",
        "Show - 06 - Omakase Sushi Night.mkv",
        # "CM" and "PV" are also release-group tags. A leading [Group] is never a content marker.
        "[CM] Show - 01 [1080p].mkv",
        "[PV] Show - 02 [1080p].mkv",
        # Bare weak markers without the fansub extras numbering stay episodes.
        "Show - 07 - Menu For Two.mkv",
        # A real show literally named "Trailer Park Boys" must never be skipped as bonus content.
        "Trailer Park Boys - S01E01.mkv",
        "Show.S01E05.Trailer.Park.mkv",
        "Trailer Park Boys - 01 - Take Your Little Gun and Get Out of My Trailer Park.mkv",
        # An explicit episode code settles it, whatever else the name says.
        "Show.S02E03.Teaser.Campaign.mkv",
    ]

    ALTERNATE_SPELLINGS = [
        "[Group] Show - NC OP - 01 [1080p].mkv",
        "[Group] Show - NC-OP [1080p].mkv",
        "[Group] Show - NC_ED [1080p].mkv",
        "[Group] Show - Non-Credit OP [1080p].mkv",
        "[Group] Show - Clean OP [1080p].mkv",
        "[Group] Show - Clean Opening [1080p].mkv",
        # Numeric suffixes: the token boundary must not reject them.
        "Show NCOP1.mkv",
        "Show NCED1.mkv",
        "Show NCOP01.mkv",
        "Show NCBD2.mkv",
        "Show NC Opening - 01.mkv",
        "Show NC Ending - 01.mkv",
        "Show Clean OP1.mkv",
        "Show Clean ED1.mkv",
    ]

    NOT_EXTRAS_LOOKALIKES = [
        # "Cleaned" must not read as "Clean" + "ed": the separator before OP/ED is required.
        "Show - 05 - Cleaned Up.mkv",
        "Show - 06 - Clean Operation.mkv",
        # A word merely containing the token is bounded out.
        "Franc Editions - 01.mkv",
        "Show - 07 - Omakase Sushi Night.mkv",
    ]

    def test_lookalike_titles_are_not_extras(self):
        from sickchill.helper.common import is_anime_extra

        for name in self.NOT_EXTRAS_LOOKALIKES:
            self.assertFalse(is_anime_extra(name), f"should be a real episode: {name}")

    def test_alternate_creditless_spellings_are_detected(self):
        from sickchill.helper.common import is_anime_extra

        for name in self.ALTERNATE_SPELLINGS:
            self.assertTrue(is_anime_extra(name), f"should be bonus content: {name}")

    def test_explicit_episode_code_always_wins(self):
        from sickchill.helper.common import is_anime_extra

        # Even a genuine extras marker loses to an explicit SxxExx: that is an episode.
        self.assertFalse(is_anime_extra("Show.S01E05.NCOP.mkv"))
        self.assertTrue(is_anime_extra("Show.NCOP.mkv"))

    def test_extras_are_detected(self):
        from sickchill.helper.common import is_anime_extra

        for name in self.EXTRAS:
            self.assertTrue(is_anime_extra(name), f"should be bonus content: {name}")

    def test_episodes_are_not_mistaken_for_extras(self):
        from sickchill.helper.common import is_anime_extra

        for name in self.EPISODES:
            self.assertFalse(is_anime_extra(name), f"should be a real episode: {name}")

    def test_only_the_basename_is_examined(self):
        from sickchill.helper.common import is_anime_extra

        self.assertFalse(is_anime_extra(os.path.join("Some NCOP Folder", "Show - 01 [1080p].mkv")))


class TestReapDecision(unittest.TestCase):
    """Reaping is an rmtree. Every folder state must be checked for wrongful deletion."""

    def _blocker(self, video_files, failed_files, extra_files, process_method="move", mode="auto", delete_on=False):
        from sickchill.oldbeard.processTV import reap_blocker

        return reap_blocker(process_method, mode, delete_on, video_files, failed_files, extra_files)

    def test_episodes_only_all_succeeded_is_reaped(self):
        # The only state where deletion is correct: everything was imported, nothing left behind.
        self.assertIsNone(self._blocker(video_files=["ep.mkv"], failed_files=[], extra_files=[]))

    def test_episodes_only_with_a_failure_is_kept(self):
        self.assertIsNotNone(self._blocker(video_files=["ep.mkv"], failed_files=["ep.mkv"], extra_files=[]))

    def test_extras_only_is_kept(self):
        self.assertIsNotNone(self._blocker(video_files=[], failed_files=[], extra_files=["NCOP.mkv"]))

    def test_mixed_folder_is_kept_even_when_every_episode_succeeded(self):
        # The dangerous case: episodes import cleanly, and rmtree would take the extras with them.
        self.assertIsNotNone(self._blocker(video_files=["ep.mkv"], failed_files=[], extra_files=["NCOP.mkv"]))

    def test_empty_folder_is_kept(self):
        self.assertIsNotNone(self._blocker(video_files=[], failed_files=[], extra_files=[]))

    def test_non_move_methods_never_reap(self):
        for method in ("copy", "hardlink", "symlink"):
            with self.subTest(method=method):
                self.assertIsNotNone(self._blocker(["ep.mkv"], [], [], process_method=method))

    def test_manual_mode_without_delete_never_reaps(self):
        self.assertIsNotNone(self._blocker(["ep.mkv"], [], [], mode="manual", delete_on=False))
        self.assertIsNone(self._blocker(["ep.mkv"], [], [], mode="manual", delete_on=True))

    def test_process_dir_uses_the_blocker_before_deleting(self):
        import inspect

        from sickchill.oldbeard import processTV

        source = inspect.getsource(processTV.process_dir)
        self.assertLess(source.index("reap_blocker("), source.index("delete_folder(current_directory"))


class TestAlreadyProcessedRefusesAmbiguity(unittest.TestCase):
    """already_processed() must not declare an ambiguous file handled -- that would let it be reaped."""

    def test_episode_scope_is_none_for_an_ambiguous_parse(self):
        from sickchill.oldbeard.processTV import _episode_scope

        show = mock.Mock(indexerid=300835)
        clean = mock.Mock(show=show, season_number=1, episode_numbers=[2], ab_episode_numbers=[2], ambiguous=False)
        self.assertIsNotNone(_episode_scope(clean), "a clean parse still scopes normally")

        ambiguous = mock.Mock(show=show, season_number=1, episode_numbers=[2], ab_episode_numbers=[2], ambiguous=True)
        self.assertIsNone(_episode_scope(ambiguous), "an ambiguous parse must have no trustworthy scope")


class TestFilenameTruncation(unittest.TestCase):
    """Generated names must fit the filesystem's 255-byte component limit."""

    def test_short_name_is_untouched(self):
        from sickchill.helper.common import truncate_filename

        name = "Ajin - s02e12 - 025 - But That Makes It Interesting, So Whatever - 1080p BluRay"
        self.assertEqual(truncate_filename(name), name)

    def test_long_multi_episode_name_is_clamped(self):
        from sickchill.helper.common import FILENAME_SUFFIX_HEADROOM, MAX_FILENAME_BYTES, truncate_filename

        # The exact shape that produced [Errno 36] on the live install.
        name = (
            "Ajin - s01e02-12 - 002-003-004-005-006-007-008-009-010-011-012 - Why is This Happening "
            "Why Me... & Maybe This is the End & Have You Ever Seen a Black Ghost & But When It Comes "
            "Down to It, You Still Want Them to Help You... & I'm Gonna Kill You Too - 1080p BluRay"
        )
        truncated = truncate_filename(name)
        budget = MAX_FILENAME_BYTES - FILENAME_SUFFIX_HEADROOM
        self.assertLess(len(truncated), len(name))
        self.assertLessEqual(len(truncated.encode("utf-8")), budget)
        self.assertFalse(truncated.endswith((" ", "-", ".", "&", ",", "_")), "no dangling separator")

    def test_headroom_is_left_for_extension_and_associated_files(self):
        from sickchill.helper.common import FILENAME_SUFFIX_HEADROOM, MAX_FILENAME_BYTES, truncate_filename

        truncated = truncate_filename("x" * 400)
        # ".en.forced.srt" and friends must still fit inside the 255-byte component limit.
        self.assertLessEqual(len(f"{truncated}.en.forced.srt".encode("utf-8")), MAX_FILENAME_BYTES)
        self.assertGreaterEqual(FILENAME_SUFFIX_HEADROOM, len(".en.forced.srt"))

    def test_multibyte_names_are_not_cut_mid_character(self):
        from sickchill.helper.common import MAX_FILENAME_BYTES, truncate_filename

        truncated = truncate_filename("日本語のタイトル" * 40)
        self.assertLessEqual(len(truncated.encode("utf-8")), MAX_FILENAME_BYTES)
        # Decoding is implicit -- a mid-codepoint cut would have raised or dropped to replacement.
        self.assertTrue(truncated.encode("utf-8").decode("utf-8"))

    def test_names_sharing_a_long_prefix_do_not_collide(self):
        # The move path overwrites its destination, so two truncated names must never be equal.
        from sickchill.helper.common import FILENAME_SUFFIX_HEADROOM, MAX_FILENAME_BYTES, truncate_filename

        prefix = "Show - s01e01-04 - " + ("A Very Long Episode Title That Repeats " * 8)
        first, second = truncate_filename(prefix + "alpha"), truncate_filename(prefix + "beta")

        self.assertNotEqual(first, second, "truncation must not let one episode overwrite another")
        budget = MAX_FILENAME_BYTES - FILENAME_SUFFIX_HEADROOM
        for truncated in (first, second):
            self.assertLessEqual(len(truncated.encode("utf-8")), budget)

    def test_truncation_is_deterministic(self):
        from sickchill.helper.common import truncate_filename

        name = "y" * 400
        self.assertEqual(truncate_filename(name), truncate_filename(name))

    def test_tiny_budget_still_honours_the_byte_limit(self):
        # The digest does not fit; the helper must still respect its own contract.
        from sickchill.helper.common import truncate_filename

        for budget in (1, 5, 9, 10, 40):
            truncated = truncate_filename("z" * 400, max_bytes=budget)
            self.assertLessEqual(len(truncated.encode("utf-8")), budget, f"budget={budget}")


class TestFailureBackoff(unittest.TestCase):
    """A permanently-failing file must not be retried every scheduler pass."""

    def setUp(self):
        from sickchill.oldbeard import processTV

        self.processTV = processTV
        processTV._failure_backoff.clear()
        self.addCleanup(processTV._failure_backoff.clear)

    def _make_file(self):
        import tempfile

        handle, path = tempfile.mkstemp(suffix=".mkv")
        with os.fdopen(handle, "wb") as stream:
            stream.write(b"some bytes")
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def test_first_failure_starts_a_backoff(self):
        path = self._make_file()
        self.assertFalse(self.processTV.in_failure_backoff(path))

        self.processTV.record_processing_failure(path)
        self.assertTrue(self.processTV.in_failure_backoff(path), "a just-failed file must be skipped")

    def test_backoff_grows_with_consecutive_failures(self):
        self.assertEqual(self.processTV._backoff_seconds(1), self.processTV.FAILURE_BACKOFF_BASE_SECONDS)
        self.assertEqual(self.processTV._backoff_seconds(2), self.processTV.FAILURE_BACKOFF_BASE_SECONDS * 2)
        self.assertEqual(self.processTV._backoff_seconds(99), self.processTV.FAILURE_BACKOFF_MAX_SECONDS)

    def test_backoff_expires(self):
        path = self._make_file()
        self.processTV.record_processing_failure(path)

        fingerprint, failures, _ = self.processTV._failure_backoff[path]
        stale = time.time() - self.processTV.FAILURE_BACKOFF_BASE_SECONDS - 1
        self.processTV._failure_backoff[path] = (fingerprint, failures, stale)

        self.assertFalse(self.processTV.in_failure_backoff(path), "backoff must expire so the file is retried")

    def test_changed_file_is_retried_immediately(self):
        path = self._make_file()
        self.processTV.record_processing_failure(path)
        self.assertTrue(self.processTV.in_failure_backoff(path))

        with open(path, "wb") as stream:
            stream.write(b"different content entirely")

        self.assertFalse(self.processTV.in_failure_backoff(path), "a repaired/replaced file must retry at once")
        self.assertNotIn(path, self.processTV._failure_backoff)

    def test_force_bypasses_the_backoff(self):
        path = self._make_file()
        self.processTV.record_processing_failure(path)
        self.assertFalse(self.processTV.in_failure_backoff(path, force=True))

    def test_success_clears_the_backoff(self):
        path = self._make_file()
        self.processTV.record_processing_failure(path)
        self.processTV.record_processing_success(path)
        self.assertNotIn(path, self.processTV._failure_backoff)
        self.assertFalse(self.processTV.in_failure_backoff(path))

    def test_missing_file_is_not_recorded(self):
        self.processTV.record_processing_failure("/nonexistent/path/file.mkv")
        self.assertNotIn("/nonexistent/path/file.mkv", self.processTV._failure_backoff)


if __name__ == "__main__":
    unittest.main()
