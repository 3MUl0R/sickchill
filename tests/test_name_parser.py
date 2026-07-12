import datetime
import os
import sys
import unittest

from sickchill import settings, tv
from sickchill.oldbeard import common, scheduler, show_queue
from sickchill.oldbeard.name_parser import parser
from tests import conftest

DEBUG = os.getenv("DEBUG")
VERBOSE = os.getenv("VERBOSE")

SIMPLE_TEST_CASES = {
    "standard": {
        "Mr.Show.Name.S01E02.Source.Quality.Etc-Group": parser.ParseResult(None, "Mr Show Name", 1, [2], "Source.Quality.Etc", "Group"),
        "Show.Name.S01E02": parser.ParseResult(None, "Show Name", 1, [2]),
        "Show Name - S01E02 - My Ep Name": parser.ParseResult(None, "Show Name", 1, [2], "My Ep Name"),
        "Show.1.0.Name.S01.E03.My.Ep.Name-Group": parser.ParseResult(None, "Show 1.0 Name", 1, [3], "My.Ep.Name", "Group"),
        "Show.Name.S01E02E03.Source.Quality.Etc-Group": parser.ParseResult(None, "Show Name", 1, [2, 3], "Source.Quality.Etc", "Group"),
        "Mr. Show Name - S01E02-03 - My Ep Name": parser.ParseResult(None, "Mr. Show Name", 1, [2, 3], "My Ep Name"),
        "Show.Name.S01.E02.E03": parser.ParseResult(None, "Show Name", 1, [2, 3]),
        "Show.Name-0.2010.S01E02.Source.Quality.Etc-Group": parser.ParseResult(None, "Show Name-0 2010", 1, [2], "Source.Quality.Etc", "Group"),
        "S01E02 Ep Name": parser.ParseResult(None, None, 1, [2], "Ep Name"),
        "Show Name - S06E01 - 2009-12-20 - Ep Name": parser.ParseResult(None, "Show Name", 6, [1], "2009-12-20 - Ep Name"),
        "Show Name - S06E01 - -30-": parser.ParseResult(None, "Show Name", 6, [1], "30-"),
        "Show-Name-S06E01-720p": parser.ParseResult(None, "Show-Name", 6, [1], "720p"),
        "Show-Name-S06E01-1080i": parser.ParseResult(None, "Show-Name", 6, [1], "1080i"),
        "Show.Name.S06E01.Other.WEB-DL": parser.ParseResult(None, "Show Name", 6, [1], "Other.WEB-DL"),
        "Show.Name.S06E01 Some-Stuff Here": parser.ParseResult(None, "Show Name", 6, [1], "Some-Stuff Here"),
        "Show Name - S03E14-36! 24! 36! Hut! (1)": parser.ParseResult(None, "Show Name", 3, [14], "36! 24! 36! Hut! (1)"),
    },
    "fov": {
        "Show_Name.1x02.Source_Quality_Etc-Group": parser.ParseResult(None, "Show Name", 1, [2], "Source_Quality_Etc", "Group"),
        "Show Name 1x02": parser.ParseResult(None, "Show Name", 1, [2]),
        "Show Name 1x02 x264 Test": parser.ParseResult(None, "Show Name", 1, [2], "x264 Test"),
        "Show Name - 1x02 - My Ep Name": parser.ParseResult(None, "Show Name", 1, [2], "My Ep Name"),
        "Show_Name.1x02x03x04.Source_Quality_Etc-Group": parser.ParseResult(None, "Show Name", 1, [2, 3, 4], "Source_Quality_Etc", "Group"),
        "Show Name - 1x02-03-04 - My Ep Name": parser.ParseResult(None, "Show Name", 1, [2, 3, 4], "My Ep Name"),
        "1x02 Ep Name": parser.ParseResult(None, None, 1, [2], "Ep Name"),
        "Show-Name-1x02-720p": parser.ParseResult(None, "Show-Name", 1, [2], "720p"),
        "Show-Name-1x02-1080i": parser.ParseResult(None, "Show-Name", 1, [2], "1080i"),
        "Show Name [05x12] Ep Name": parser.ParseResult(None, "Show Name", 5, [12], "Ep Name"),
        "Show.Name.1x02.WEB-DL": parser.ParseResult(None, "Show Name", 1, [2], "WEB-DL"),
    },
    "standard_repeat": {
        "Show.Name.S01E02.S01E03.Source.Quality.Etc-Group": parser.ParseResult(None, "Show Name", 1, [2, 3], "Source.Quality.Etc", "Group"),
        "Show.Name.S01E02.S01E03": parser.ParseResult(None, "Show Name", 1, [2, 3]),
        "Show Name - S01E02 - S01E03 - S01E04 - Ep Name": parser.ParseResult(None, "Show Name", 1, [2, 3, 4], "Ep Name"),
        "Show.Name.S01E02.S01E03.WEB-DL": parser.ParseResult(None, "Show Name", 1, [2, 3], "WEB-DL"),
    },
    "fov_repeat": {
        "Show.Name.1x02.1x03.Source.Quality.Etc-Group": parser.ParseResult(None, "Show Name", 1, [2, 3], "Source.Quality.Etc", "Group"),
        "Show.Name.1x02.1x03": parser.ParseResult(None, "Show Name", 1, [2, 3]),
        "Show Name - 1x02 - 1x03 - 1x04 - Ep Name": parser.ParseResult(None, "Show Name", 1, [2, 3, 4], "Ep Name"),
        "Show.Name.1x02.1x03.WEB-DL": parser.ParseResult(None, "Show Name", 1, [2, 3], "WEB-DL"),
    },
    "bare": {
        "Show.Name.102.Source.Quality.Etc-Group": parser.ParseResult(None, "Show Name", 1, [2], "Source.Quality.Etc", "Group"),
        "show.name.2010.123.source.quality.etc-group": parser.ParseResult(None, "show name 2010", 1, [23], "source.quality.etc", "group"),
        "show.name.2010.222.123.source.quality.etc-group": parser.ParseResult(None, "show name 2010.222", 1, [23], "source.quality.etc", "group"),
        "Show.Name.102": parser.ParseResult(None, "Show Name", 1, [2]),
        "Show.Name.01e02": parser.ParseResult(None, "Show Name", 1, [2]),
        "the.event.401.hdtv-group": parser.ParseResult(None, "the event", 4, [1], "hdtv", "group"),
        "show.name.2010.special.hdtv-blah": None,
        "show.ex-name.102.hdtv-group": parser.ParseResult(None, "show ex-name", 1, [2], "hdtv", "group"),
    },
    "stupid": {"tpz-abc102": parser.ParseResult(None, "abc", 1, [2], None, "tpz"), "tpz-abc.102": parser.ParseResult(None, "abc", 1, [2], None, "tpz")},
    "no_season": {
        "Show Name - 01 - Ep Name": parser.ParseResult(None, "Show Name", None, [1], "Ep Name"),
        "01 - Ep Name": parser.ParseResult(None, None, None, [1], "Ep Name"),
        "Show Name - 01 - Ep Name - WEB-DL": parser.ParseResult(None, "Show Name", None, [1], "Ep Name - WEB-DL"),
    },
    "no_season_general": {
        "Show.Name.E23.Source.Quality.Etc-Group": parser.ParseResult(None, "Show Name", None, [23], "Source.Quality.Etc", "Group"),
        "Show Name - Episode 01 - Ep Name": parser.ParseResult(None, "Show Name", None, [1], "Ep Name"),
        "Show.Name.Part.3.Source.Quality.Etc-Group": parser.ParseResult(None, "Show Name", None, [3], "Source.Quality.Etc", "Group"),
        "Show.Name.Part.1.and.Part.2.Blah-Group": parser.ParseResult(None, "Show Name", None, [1, 2], "Blah", "Group"),
        "Show.Name.Part.IV.Source.Quality.Etc-Group": parser.ParseResult(None, "Show Name", None, [4], "Source.Quality.Etc", "Group"),
        "Deconstructed.E07.1080i.HDTV.DD5.1.MPEG2-TrollHD": parser.ParseResult(None, "Deconstructed", None, [7], "1080i.HDTV.DD5.1.MPEG2", "TrollHD"),
        "Show.Name.E23.WEB-DL": parser.ParseResult(None, "Show Name", None, [23], "WEB-DL"),
    },
    "no_season_multi_ep": {
        "Show.Name.E23-24.Source.Quality.Etc-Group": parser.ParseResult(None, "Show Name", None, [23, 24], "Source.Quality.Etc", "Group"),
        "Show Name - Episode 01-02 - Ep Name": parser.ParseResult(None, "Show Name", None, [1, 2], "Ep Name"),
        "Show.Name.E23-24.WEB-DL": parser.ParseResult(None, "Show Name", None, [23, 24], "WEB-DL"),
    },
    "season_only": {
        "Show.Name.S02.Source.Quality.Etc-Group": parser.ParseResult(None, "Show Name", 2, [], "Source.Quality.Etc", "Group"),
        "Show Name Season 2": parser.ParseResult(None, "Show Name", 2),
        "Season 02": parser.ParseResult(None, None, 2),
    },
    "scene_date_format": {
        "Show.Name.2010.11.23.Source.Quality.Etc-Group": parser.ParseResult(
            None, "Show Name", None, [], "Source.Quality.Etc", "Group", datetime.date(2010, 11, 23)
        ),
        "Show Name - 2010.11.23": parser.ParseResult(None, "Show Name", air_date=datetime.date(2010, 11, 23)),
        "Show.Name.2010.23.11.Source.Quality.Etc-Group": parser.ParseResult(
            None, "Show Name", None, [], "Source.Quality.Etc", "Group", datetime.date(2010, 11, 23)
        ),
        "Show Name - 2010-11-23 - Ep Name": parser.ParseResult(None, "Show Name", extra_info="Ep Name", air_date=datetime.date(2010, 11, 23)),
        "2010-11-23 - Ep Name": parser.ParseResult(None, extra_info="Ep Name", air_date=datetime.date(2010, 11, 23)),
        "Show.Name.2010.11.23.WEB-DL": parser.ParseResult(None, "Show Name", None, [], "WEB-DL", None, datetime.date(2010, 11, 23)),
    },
}

ANIME_TEST_CASES = {
    "anime_SxxExx": {
        "Show Name - S01E02 - Ep Name": parser.ParseResult(None, "Show Name", 1, [2], "Ep Name"),
        "Show Name - S01E02-03 - My Ep Name": parser.ParseResult(None, "Show Name", 1, [2, 3]),
        "Show Name - S01E02": parser.ParseResult(None, "Show Name", 1, [2]),
        "Show Name - S01E02-03": parser.ParseResult(None, "Show Name", 1, [2, 3]),
    },
    "anime_bare": {
        "Show Name - 102": parser.ParseResult(None, "Show Name", ab_episode_numbers=[102], version=1),
        "[SC]_Show_Name_123": parser.ParseResult(None, "Show Name", release_group="SC", ab_episode_numbers=[123], version=1),
        "Show.Name.-.134.-.1080p.BluRay.x264.DHD": parser.ParseResult(
            None, "Show Name", ab_episode_numbers=[134], quality=common.Quality.FULLHDBLURAY, version=1
        ),
    },
}

COMBINATION_TEST_CASES = [
    ("/test/path/to/Season 02/03 - Ep Name.avi", parser.ParseResult(None, None, 2, [3], "Ep Name"), ["no_season", "season_only"]),
    (
        "Show.Name.S02.Source.Quality.Etc-Group/tpz-sn203.avi",
        parser.ParseResult(None, "Show Name", 2, [3], "Source.Quality.Etc", "Group"),
        ["stupid", "season_only"],
    ),
    ("MythBusters.S08E16.720p.HDTV.x264-aAF/aaf-mb.s08e16.720p.mkv", parser.ParseResult(None, "MythBusters", 8, [16], "720p.HDTV.x264", "aAF"), ["standard"]),
    (
        "/home/drop/storage/TV/Terminator The Sarah Connor Chronicles/Season 2/S02E06 The Tower is Tall, But the Fall is Short.mkv",
        parser.ParseResult(None, None, 2, [6], "The Tower is Tall, But the Fall is Short"),
        ["standard"],
    ),
    (
        r"/Test/TV/Jimmy Fallon/Season 2/Jimmy Fallon - 2010-12-15 - blah.avi",
        parser.ParseResult(None, "Jimmy Fallon", extra_info="blah", air_date=datetime.date(2010, 12, 15)),
        ["scene_date_format"],
    ),
    (r"/X/30 Rock/Season 4/30 Rock - 4x22 -.avi", parser.ParseResult(None, "30 Rock", 4, [22]), ["fov"]),
    ("Season 2\\Show Name - 03-04 - Ep Name.ext", parser.ParseResult(None, "Show Name", 2, [3, 4], extra_info="Ep Name"), ["no_season", "season_only"]),
    ("Season 02\\03-04-05 - Ep Name.ext", parser.ParseResult(None, None, 2, [3, 4, 5], extra_info="Ep Name"), ["no_season", "season_only"]),
]

# noinspection SpellCheckingInspection
UNICODE_TEST_CASES = [
    (
        "The.Big.Bang.Theory.2x07.The.Panty.Piñata.Polarization.720p.HDTV.x264.AC3-SHELDON.mkv",
        parser.ParseResult(None, "The.Big.Bang.Theory", 2, [7], "The.Panty.Piñata.Polarization.720p.HDTV.x264.AC3", "SHELDON"),
    ),
    (
        "The.Big.Bang.Theory.2x07.The.Panty.Piñata.Polarization.720p.HDTV.x264.AC3-SHELDON.mkv",
        parser.ParseResult(None, "The.Big.Bang.Theory", 2, [7], "The.Panty.Piñata.Polarization.720p.HDTV.x264.AC3", "SHELDON"),
    ),
]

# noinspection SpellCheckingInspection
FAILURE_CASES = ["7sins-jfcs01e09-720p-bluray-x264"]


class UnicodeTests(conftest.SickChillTestDBCase):
    """
    Test str
    """

    def __init__(self, something):
        super().__init__(something)
        super().setUp()
        self.show = tv.TVShow(1, 1, "en")
        self.show.name = "The Big Bang Theory"

    def _test_unicode(self, name, result):
        """
        Test str

        :param name:
        :param result:
        :return:
        """
        name_parser = parser.NameParser(True, show_object=self.show)
        parse_result = name_parser.parse(name)

        # this shouldn't raise an exception
        repr(str(parse_result))
        assert parse_result.extra_info == result.extra_info

    def test_unicode(self):
        """
        Test str
        """
        for name, result in UNICODE_TEST_CASES:
            self._test_unicode(name, result)


class FailureCaseTests(conftest.SickChillTestDBCase):
    """
    Test cases that should fail
    """

    @staticmethod
    def _test_name(name):
        """
        Test name

        :param name:
        :return:
        """
        name_parser = parser.NameParser(True)
        try:
            parse_result = name_parser.parse(name)
        except (parser.InvalidNameException, parser.InvalidShowException):
            return True

        if VERBOSE:
            print("Actual: ", parse_result.which_regex, parse_result)
        return False

    def test_failures(self):
        """
        Test failures
        """
        for name in FAILURE_CASES:
            assert self._test_name(name)


class ComboTests(conftest.SickChillTestDBCase):
    """
    Perform combination tests
    """

    def _test_combo(self, name, result, which_regexes):
        """
        Perform combination test

        :param name:
        :param result:
        :param which_regexes:
        :return:
        """

        if VERBOSE:
            print()
            print("Testing", name)

        name_parser = parser.NameParser(True)

        try:
            test_result = name_parser.parse(name)
        except parser.InvalidShowException:
            return False

        if DEBUG:
            print(test_result, test_result.which_regex)
            print(result, which_regexes)

        assert str(test_result) == str(result)
        for cur_regex in which_regexes:
            assert cur_regex in test_result.which_regex
        assert len(which_regexes) == len(test_result.which_regex)

    def test_combos(self):
        """
        Perform combination tests
        """
        for name, result, which_regexes in COMBINATION_TEST_CASES:
            # Normalise the paths. Converts UNIX-style paths into Windows-style
            # paths when test is run on Windows.
            self._test_combo(os.path.normpath(name), result, which_regexes)


class BasicTests(conftest.SickChillTestDBCase):
    """
    Basic name parsing tests
    """

    def __init__(self, something):
        super().__init__(something)
        super().setUp()
        self.show = tv.TVShow(1, 1, "en")

    def _test_names(self, name_parser, section, transform=None, verbose=False):
        """
        Performs a test

        :param name_parser: to use for test
        :param section:
        :param transform:
        :param verbose:
        :return:
        """

        if VERBOSE or verbose:
            print()
            print("Running", section, "tests")
        for cur_test_base in SIMPLE_TEST_CASES[section]:
            if transform:
                cur_test = transform(cur_test_base)
                name_parser.filename = cur_test
            else:
                cur_test = cur_test_base
            if VERBOSE or verbose:
                print("Testing", cur_test)

            result = SIMPLE_TEST_CASES[section][cur_test_base]

            self.show.name = result.series_name if result else None
            name_parser.show_object = self.show
            if not result:
                self.assertRaises(parser.InvalidNameException, name_parser.parse, cur_test)
                return
            else:
                result.which_regex = [section]
                test_result = name_parser.parse(cur_test)

            def print_debug():
                print(f"{cur_test}:")
                print(f"Test Result: {test_result}")
                print(f"Expected Result: {result}")

            if DEBUG or verbose:
                print_debug()

            result.score = test_result.score  # Needed so we don't have to specify expected regex score in each test case.
            assert test_result.which_regex == [section], print_debug()
            assert str(test_result) == str(result), print_debug()

    def test_standard_names(self):
        """
        Test standard names
        """
        name_parser = parser.NameParser(True)
        self._test_names(name_parser, "standard")

    def test_standard_filenames(self):
        """
        Test standard file names
        """
        name_parser = parser.NameParser()
        self._test_names(name_parser, "standard", lambda x: x + ".avi")

    def test_standard_repeat_names(self):
        """
        Test standard repeat names
        """
        name_parser = parser.NameParser(False)
        self._test_names(name_parser, "standard_repeat")

    def test_standard_repeat_filenames(self):
        """
        Test standard repeat file names
        """
        name_parser = parser.NameParser()
        self._test_names(name_parser, "standard_repeat", lambda x: x + ".avi")

    def test_fov_names(self):
        """
        Test fov names
        """
        name_parser = parser.NameParser(False)
        self._test_names(name_parser, "fov")

    def test_fov_filenames(self):
        """
        Test fov file names
        """
        name_parser = parser.NameParser()
        self._test_names(name_parser, "fov", lambda x: x + ".avi")

    def test_fov_repeat_names(self):
        """
        Test fov repeat names
        """
        name_parser = parser.NameParser(False)
        self._test_names(name_parser, "fov_repeat")

    def test_fov_repeat_filenames(self):
        """
        Test fov repeat file names
        """
        name_parser = parser.NameParser()
        self._test_names(name_parser, "fov_repeat", lambda x: x + ".avi")

    def test_stupid_names(self):
        """
        Test stupid names
        """
        name_parser = parser.NameParser(False)
        self._test_names(name_parser, "stupid")

    def test_stupid_filenames(self):
        """
        Test stupid file names
        """
        name_parser = parser.NameParser()
        self._test_names(name_parser, "stupid", lambda x: x + ".avi")

    def test_no_s_general_names(self):
        """
        Test no season general names
        """
        name_parser = parser.NameParser(False)
        self._test_names(name_parser, "no_season_general")

    def test_no_s_general_filenames(self):
        """
        Test no season general file names
        """
        name_parser = parser.NameParser()
        self._test_names(name_parser, "no_season_general", lambda x: x + ".avi")

    def test_no_s_multi_ep_names(self):
        """
        Test no season multi episode names
        """
        name_parser = parser.NameParser(False)
        self._test_names(name_parser, "no_season_multi_ep")

    def test_no_s_multi_ep_filenames(self):
        """
        Test no season multi episode file names
        """
        name_parser = parser.NameParser()
        self._test_names(name_parser, "no_season_multi_ep", lambda x: x + ".avi")

    def test_s_only_names(self):
        """
        Test season only names
        """
        name_parser = parser.NameParser(False)
        self._test_names(name_parser, "season_only")

    def test_s_only_filenames(self):
        """
        Test season only file names
        """
        name_parser = parser.NameParser()
        self._test_names(name_parser, "season_only", lambda x: x + ".avi")


class AnimeTests(conftest.SickChillTestDBCase):
    """
    Basic tests for anime
    """

    def __init__(self, something):
        super().__init__(something)
        super().setUp()
        self.show: tv.TVShow = tv.TVShow(1, 1, "en")
        self.show.paused = True
        self.show.anime = 1

    def tearDown(self):
        parser.name_parser_cache.data.clear()

    def _test_names(self, name_parser, section, transform=None, verbose=False):
        """
        Performs a test

        :param name_parser: to use for test
        :param section:
        :param transform:
        :param verbose:
        :return:
        """
        if VERBOSE or verbose:
            print()
            print("Running", section, "tests")
        for cur_test_base in ANIME_TEST_CASES[section]:
            if transform:
                cur_test = transform(cur_test_base)
                name_parser.filename = cur_test
            else:
                cur_test = cur_test_base
            if VERBOSE or verbose:
                print("Testing", cur_test)

            result = ANIME_TEST_CASES[section][cur_test_base]

            self.show.name = result.series_name if result else None
            name_parser.show_object = self.show
            if not result:
                self.assertRaises(parser.InvalidNameException, name_parser.parse, cur_test)
                return
            else:
                result.which_regex = [section]
                test_result = name_parser.parse(cur_test)

            def print_debug():
                print(f"{cur_test}:")
                print(f"Test Result: {test_result}")
                print(f"Expected Result: {result}")

            if DEBUG or verbose:
                print_debug()

            result.score = test_result.score  # Needed so we don't have to specify expected regex score in each test case.
            assert test_result.which_regex == [section], print_debug()
            assert str(test_result) == str(result), print_debug()

    def test_anime_sxxexx_filenames(self):
        """
        Test anime SxxExx file names
        """
        name_parser = parser.NameParser(parse_method="anime")
        self._test_names(name_parser, "anime_SxxExx", lambda x: x + ".avi")

    def test_anime_bare_filenames(self):
        """
        Test anime bare file names
        """
        name_parser = parser.NameParser(parse_method="anime")
        self._test_names(name_parser, "anime_bare", lambda x: x + ".avi")

    @unittest.expectedFailure
    def test_anime_bare_filenames_indirect(self):
        """
        Test anime bare file names without defining parse method
        """
        name_parser = parser.NameParser()
        self._test_names(name_parser, "anime_bare", lambda x: x + ".avi")


# TODO: Make these work or document why they shouldn't
class BasicFailedTests(conftest.SickChillTestDBCase):
    """
    Basic tests that currently fail
    """

    def __init__(self, something):
        super().__init__(something)
        super().setUp()
        self.show = tv.TVShow(1, 1, "en")

    def _test_names(self, name_parser, section, transform=None, verbose=False):
        """
        Performs a test

        :param name_parser: to use for test
        :param section:
        :param transform:
        :param verbose:
        :return:
        """
        if VERBOSE or verbose:
            print()
            print("Running", section, "tests")
        for cur_test_base in SIMPLE_TEST_CASES[section]:
            if transform:
                cur_test = transform(cur_test_base)
                name_parser.filename = cur_test
            else:
                cur_test = cur_test_base
            if VERBOSE or verbose:
                print("Testing", cur_test)

            result = SIMPLE_TEST_CASES[section][cur_test_base]

            self.show.name = result.series_name if result else None
            name_parser.show_object = self.show
            if not result:
                self.assertRaises(parser.InvalidNameException, name_parser.parse, cur_test)
                return
            else:
                result.which_regex = [section]
                test_result = name_parser.parse(cur_test)

            def print_debug():
                print(f"{cur_test}:")
                print(f"Test Result: {test_result}")
                print(f"Expected Result: {result}")

            if DEBUG or verbose:
                print_debug()

            result.score = test_result.score  # Needed so we don't have to specify expected regex score in each test case.
            assert test_result.which_regex == [section], print_debug()
            assert str(test_result) == str(result), print_debug()

    def test_no_s_names(self):
        """
        Test no season names
        """
        name_parser = parser.NameParser(False)
        self._test_names(name_parser, "no_season")

    def test_no_s_filenames(self):
        """
        Test no season file names
        """
        name_parser = parser.NameParser()
        self._test_names(name_parser, "no_season", lambda x: x + ".avi")

    @unittest.expectedFailure
    def test_bare_names(self):
        """
        Test bare names
        """
        name_parser = parser.NameParser(False)
        self._test_names(name_parser, "bare")

    @unittest.expectedFailure
    def test_bare_filenames(self):
        """
        Test bare file names
        """
        name_parser = parser.NameParser()
        self._test_names(name_parser, "bare", lambda x: x + ".avi")

    @unittest.skip("Not yet implemented")
    def test_combination_names(self):
        """
        Test combination names
        """
        pass

    @unittest.skip("Not trying indexer")
    def test_scene_date_fmt_names(self):
        """
        Test scene date format names
        """
        name_parser = parser.NameParser(False)
        self._test_names(name_parser, "scene_date_format")

    @unittest.skip("Not trying indexer")
    def test_scene_date_fmt_filenames(self):
        """
        Test scene date format file names
        """
        name_parser = parser.NameParser()
        self._test_names(name_parser, "scene_date_format", lambda x: x + ".avi")


class SeasonRelativeAnimeTests(conftest.ResetNameCacheMixin, conftest.SickChillTestPostProcessorCase):
    """A per-season anime release (Moozzi2-style) whose title matches a scene exception mapped to a
    specific season must resolve season-relative (e.g. 'Show II - 5' -> S2E5), not as a series-wide
    absolute number (which would land in season 1)."""

    def test_season_specific_exception_maps_season_relative(self):
        from sickchill.oldbeard import db, name_cache

        self.show.anime = 1
        self.show.save_to_db()

        # Map an alternate title to season 2 of the test show (indexer_id 1).
        cache_db = db.DBConnection("cache.db")
        cache_db.action(
            "INSERT INTO scene_exceptions (indexer_id, show_name, season, custom) VALUES (?, ?, ?, ?)",
            [1, "show name II", 2, 1],
        )
        name_cache.build_name_cache()

        result = parser.NameParser().parse("[Group] show name II - 5 (BD 1920x1080 x265)")
        self.assertEqual(result.show.indexerid, 1)
        self.assertEqual(result.season_number, 2)
        self.assertEqual(result.episode_numbers, [5])

    def test_ambiguous_multi_season_exception_stays_series_absolute(self):
        """If a title maps to MORE than one distinct season, it is ambiguous and must NOT be
        treated as season-relative (which would naively pick the lowest season)."""
        from sickchill.oldbeard import db, name_cache

        self.show.anime = 1
        self.show.save_to_db()

        cache_db = db.DBConnection("cache.db")
        cache_db.action(
            "INSERT INTO scene_exceptions (indexer_id, show_name, season, custom) VALUES (?, ?, ?, ?)",
            [1, "show name extra", 2, 1],
        )
        cache_db.action(
            "INSERT INTO scene_exceptions (indexer_id, show_name, season, custom) VALUES (?, ?, ?, ?)",
            [1, "show name extra", 3, 1],
        )
        name_cache.build_name_cache()

        result = parser.NameParser().parse("[Group] show name extra - 5 (BD 1920x1080 x265)")
        # Ambiguous -> falls back to series-absolute (no absolute data on the test show), so it
        # must NOT have been season-relative-mapped to the lowest season (2).
        self.assertNotEqual(result.season_number, 2)


class ResolutionGuardTests(conftest.SickChillTestDBCase):
    """A screen resolution like 1920x1080 must not be parsed as season x episode.

    Regression for BD releases such as '[Moozzi2] <title> - 16 (BD 1920x1080 x265-10Bit Flac)'
    where the anime_and_normal_x regex used to read 1920x1080 as S1920E1080, which (combined
    with a get_episode caching bug) poisoned anime absolute->episode resolution.
    """

    def test_resolution_not_parsed_as_season_episode(self):
        # _parse_string returns the raw regex parse without the show-resolution that parse() requires.
        name_parser = parser.NameParser(naming_pattern=True)
        for name in [
            "[Moozzi2] Some Anime Title - 16 (BD 1920x1080 x265-10Bit Flac)",
            "[Group] Another Show - 05 (BD 1280x720)",
            "Show Title - 07 (3840x2160)",
        ]:
            result = name_parser._parse_string(name)
            for resolution_width in (1920, 1280, 3840):
                self.assertNotEqual(result.season_number, resolution_width, f"{name!r} parsed width as a season")
            for resolution_height in (1080, 720, 2160):
                self.assertNotIn(resolution_height, result.episode_numbers or [], f"{name!r} parsed height as an episode")

    def test_real_nxm_still_parses(self):
        # A genuine NxM episode notation must still work.
        name_parser = parser.NameParser(naming_pattern=True)
        result = name_parser._parse_string("Some Show 3x05 720p")
        self.assertEqual(result.season_number, 3)
        self.assertEqual(result.episode_numbers, [5])


class SceneAnimeSeasonRelativeTests(conftest.ResetNameCacheMixin, conftest.SickChillTestPostProcessorCase):
    """Regression for the within-season mis-numbering bug (Fix B).

    For a SCENE-numbered anime, the absolute number is run through scene->indexer conversion, so the
    parsed release number ``epAbsNo`` and the converted value ``a`` differ. The season-relative
    interpretation must use the RAW ``epAbsNo`` as the within-season episode. The old code used the
    scene-converted ``a``: e.g. "Show II - 5" -> a=15 and, because S2E15 existed, it wrongly mapped to
    S2E15. (This is exactly how the K-ON S2 BD scattered across S2E13-E26.)
    """

    def _seed_scene_absolute(self, scene_absolute_number, scene_season, absolute_number, season, episode):
        from sickchill.oldbeard import db

        main_db = db.DBConnection()
        main_db.action(
            "INSERT OR REPLACE INTO scene_numbering "
            "(indexer, indexer_id, season, episode, absolute_number, scene_season, scene_episode, scene_absolute_number) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [1, 1, season, episode, absolute_number, scene_season, episode, scene_absolute_number],
        )

    def _make_scene_anime_with_season2_exception(self):
        from sickchill.oldbeard import db, name_cache

        self.show.anime = 1
        self.show.scene = 1
        self.show.save_to_db()

        cache_db = db.DBConnection("cache.db")
        cache_db.action(
            "INSERT INTO scene_exceptions (indexer_id, show_name, season, custom) VALUES (?, ?, ?, ?)",
            [1, "show name II", 2, 1],
        )
        name_cache.build_name_cache()

    def test_scene_season_relative_uses_raw_episode_not_converted_absolute(self):
        self._make_scene_anime_with_season2_exception()
        # scene-absolute 5 in season 2 converts to indexer absolute 15; S2E15 exists on the test show,
        # which is what made the old code mis-map "show name II - 5" to S2E15.
        self._seed_scene_absolute(scene_absolute_number=5, scene_season=2, absolute_number=15, season=2, episode=5)

        result = parser.NameParser().parse("[Group] show name II - 5 (BD 1920x1080 x265)")
        self.assertEqual(result.show.indexerid, 1)
        self.assertEqual(result.season_number, 2)
        self.assertEqual(result.episode_numbers, [5])  # must be E5 (raw), not E15 (scene-converted absolute)

    def test_scene_nonexistent_season_relative_refuses_a_cross_season_fallback(self):
        """When the raw number is NOT a valid episode in the mapped season, the series-absolute
        fallback fires -- but the franchise gate refuses it when it would land OUTSIDE the season
        the alias pins. "show name II - 38" with II pinned to S2 and no S2E38 used to file into
        S3E1 on the absolute fallback; that cross-season guess is exactly the corruption class
        the gate forbids, so the parse now refuses (ambiguous, no mapping) instead."""
        from sickchill.oldbeard import db

        self._make_scene_anime_with_season2_exception()
        # Give S3E1 a known series-absolute number, and an identity scene mapping for that number.
        main_db = db.DBConnection()
        main_db.action("UPDATE tv_episodes SET absolute_number = 38 WHERE showid = 1 AND indexer = 1 AND season = 3 AND episode = 1")
        self._seed_scene_absolute(scene_absolute_number=38, scene_season=2, absolute_number=38, season=3, episode=1)

        result = parser.NameParser().parse("[Group] show name II - 38 (BD 1920x1080 x265)")
        self.assertTrue(result.ambiguous)
        self.assertIsNone(result.season_number)
        self.assertEqual(result.episode_numbers, [])


class ExplicitAnimeSeasonTests(conftest.ResetNameCacheMixin, conftest.SickChillTestPostProcessorCase):
    """A1: a confident explicit season token in an anime release name (e.g. the search-time
    "K-ON.S2-01" whose ".S2." is swallowed into the series name and does NOT match a scene exception)
    is read as the season, so the release maps season-relative (S2E01) instead of to the
    series-absolute season 1. Gated hard: only when stripping the token still resolves to THIS show,
    and only when the (season, raw-episode) actually exists (else series-absolute fallback)."""

    def _make_anime(self, scene=0):
        self.show.anime = 1
        self.show.scene = scene
        self.show.save_to_db()

    def _seed_alias(self, alias, season=-1):
        """Map an alias to the show WITHOUT pinning a season (custom season -1 = whole show), so the
        title resolves to the show at the top level but does not set season_relative_season -- exactly
        the gap A1 fills. (Mirrors how the real "K-ON S2" resolves the show but matches no
        season-specific exception.)"""
        from sickchill.oldbeard import db, name_cache

        cache_db = db.DBConnection("cache.db")
        cache_db.action(
            "INSERT INTO scene_exceptions (indexer_id, show_name, season, custom) VALUES (?, ?, ?, ?)",
            [1, alias, season, 1],
        )
        name_cache.build_name_cache()

    def test_explicit_s2_token_maps_season_relative(self):
        self._make_anime()
        self._seed_alias("show name s2")  # resolves "show name S2" -> show 1, no pinned season

        result = parser.NameParser().parse("[Moozzi2] show name S2 - 03 (BD 1920x1080 x264 FLAC)")
        self.assertEqual(result.show.indexerid, 1)
        self.assertEqual(result.season_number, 2)
        self.assertEqual(result.episode_numbers, [3])

    def test_explicit_token_missing_episode_refuses_a_cross_season_fallback(self):
        """When the token's (season, raw-episode) does not exist, A1 must not force the season --
        and the franchise gate now refuses the series-absolute fallback too, because filing an
        "S2"-titled release into S4 is the cross-season guess the gate forbids. (Previously this
        fell through to absolute resolution and mapped S4E5.)"""
        from sickchill.oldbeard import db

        self._make_anime()
        self._seed_alias("show name s2")
        # The test show has no S2E99. Give a real episode the series-absolute number 99 so the
        # old fallback WOULD have resolved deterministically (S4E5) -- proving the refusal is the
        # gate's doing, not a parse failure.
        main_db = db.DBConnection()
        main_db.action("UPDATE tv_episodes SET absolute_number = 99 WHERE showid = 1 AND indexer = 1 AND season = 4 AND episode = 5")

        result = parser.NameParser().parse("[Moozzi2] show name S2 - 99 (BD 1920x1080 x264 FLAC)")
        self.assertTrue(result.ambiguous)
        self.assertIsNone(result.season_number)
        self.assertEqual(result.episode_numbers, [])

    def test_strip_not_resolving_same_show_stays_absolute(self):
        """Gate 1: if stripping the season token does NOT resolve to this show, the trailing number is
        treated as part of the title (not a season) and we stay series-absolute.

        The show is forced (show_object) -- as in a show-scoped search/PP -- so the anime branch is
        reached. The full title "zzz S2" is aliased (so anime-preference selects the anime match), but
        the stripped "zzz" is deliberately NOT in the name cache, so A1's "stripped title still
        resolves to this show" gate fails and the explicit token is ignored."""
        from sickchill.oldbeard import db

        self._make_anime()
        self._seed_alias("zzz s2")  # full title resolves -> reaches the anime branch; bare "zzz" does not
        # Pin series-absolute number 3 to S1E3 so the rejected-token fallback resolves deterministically.
        main_db = db.DBConnection()
        main_db.action("UPDATE tv_episodes SET absolute_number = 3 WHERE showid = 1 AND indexer = 1 AND season = 1 AND episode = 3")

        result = parser.NameParser(show_object=self.show).parse("[Group] zzz S2 - 03 (BD 1920x1080 x264 FLAC)")
        self.assertEqual(result.show.indexerid, 1)
        # A1's gate still ignores the token as a SEASON SOURCE (that is what this test pins), but
        # the franchise gate then refuses the series-absolute S1E3 fallback outright: the title
        # says S2, and filing it into S1 is the forbidden cross-season guess.
        self.assertTrue(result.ambiguous)
        self.assertIsNone(result.season_number)
        self.assertEqual(result.episode_numbers, [])

    def test_exception_season_conflicting_with_the_token_refuses(self):
        """A1 only fills the gap when no scene exception pins a season -- but when the exception's
        pinned season CONTRADICTS the title's explicit token, the franchise gate refuses rather
        than letting either side win. Release text outranks alias tags in trust, and an alias tag
        claiming an "S2"-titled release belongs to season 3 is legitimate only when actual scene
        numbering says so (then the gate's scene-season comparison passes; see
        test_franchise_collision.ParserChokepointTests.test_scene_divergent_token_compares_in_scene_space).
        (Previously the exception silently won and this filed S3E5.)"""
        self._make_anime()
        # Pin "show name extra s2" to season 3 (a deliberately different season than the "S2" token).
        # The show is forced so the anime branch is reached (the bare "show name extra" is not aliased).
        self._seed_alias("show name extra s2", season=3)

        result = parser.NameParser(show_object=self.show).parse("[Moozzi2] show name extra S2 - 05 (BD 1920x1080 x264 FLAC)")
        self.assertEqual(result.show.indexerid, 1)
        self.assertTrue(result.ambiguous)
        self.assertIsNone(result.season_number)
        self.assertEqual(result.episode_numbers, [])

    def test_scene_show_explicit_token_uses_raw_episode(self):
        """A1 stacked on Fix B for a SCENE anime: the explicit token supplies the season and the
        season-relative episode is the RAW parsed number, not the scene-converted absolute."""
        from sickchill.oldbeard import db

        self._make_anime(scene=1)
        self._seed_alias("show name s2")
        # scene-absolute 3 in season 2 converts to indexer absolute 17; S2E17 exists on the test show,
        # which is exactly what would mis-map "show name S2 - 3" to S2E17 if the converted value were
        # used as the episode.
        main_db = db.DBConnection()
        main_db.action(
            "INSERT OR REPLACE INTO scene_numbering "
            "(indexer, indexer_id, season, episode, absolute_number, scene_season, scene_episode, scene_absolute_number) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [1, 1, 2, 3, 17, 2, 3, 3],
        )

        result = parser.NameParser().parse("[Moozzi2] show name S2 - 3 (BD 1920x1080 x264 FLAC)")
        self.assertEqual(result.season_number, 2)
        self.assertEqual(result.episode_numbers, [3])  # raw 3, not the scene-converted absolute 17


class SeasonlessAnimeAbsoluteTests(conftest.ResetNameCacheMixin, conftest.SickChillTestPostProcessorCase):
    """An anime release that parsed an episode number but NO season (e.g. the E-prefixed
    'Show.Name.E04.quality-group' form, which a NORMAL regex claims as ep_num with no season) must be
    treated as a series-wide ABSOLUTE number. Before the fix such results kept season=None, matched no
    conversion branch, and could not be filed (they only reached the AI fallback)."""

    def _make_anime(self):
        self.show.anime = 1
        self.show.save_to_db()

    def test_e_prefixed_seasonless_number_resolves_as_absolute(self):
        from sickchill.oldbeard import db, name_cache

        self._make_anime()
        # Give S1E4 a known series-absolute number so absolute 4 -> S1E4.
        main_db = db.DBConnection()
        main_db.action("UPDATE tv_episodes SET absolute_number = 4 WHERE showid = 1 AND indexer = 1 AND season = 1 AND episode = 4")
        name_cache.build_name_cache()

        result = parser.NameParser().parse("show name E04 MULTi 1080p WEB x264-AMB3R")
        self.assertEqual(result.show.indexerid, 1)
        self.assertEqual(result.season_number, 1)
        self.assertEqual(result.episode_numbers, [4])

    def test_season_zero_specials_not_reinterpreted(self):
        """Anime S00E01 (specials) must be left as season 0 episode 1 -- the new branch is gated on
        ``season_number is None``, NOT a falsy check, so season 0 does not get reinterpreted as absolute."""
        from sickchill.oldbeard import name_cache

        self._make_anime()
        name_cache.build_name_cache()

        result = parser.NameParser().parse("show name S00E01 MULTi 1080p WEB x264-AMB3R")
        self.assertEqual(result.show.indexerid, 1)
        self.assertEqual(result.season_number, 0)
        self.assertEqual(result.episode_numbers, [1])

    def test_nonexistent_absolute_stays_unmatched(self):
        """A season-less number with no matching absolute episode must stay unmatched (season None) and
        must NOT fabricate a season/episode -- status quo is to fall through to the AI fallback."""
        from sickchill.oldbeard import name_cache

        self._make_anime()
        name_cache.build_name_cache()

        # No episode has absolute_number 99 on the test show.
        result = parser.NameParser().parse("show name E99 MULTi 1080p WEB x264-AMB3R")
        self.assertEqual(result.show.indexerid, 1)
        self.assertIsNone(result.season_number)

    def test_season_specific_alias_with_e_prefix_maps_season_relative(self):
        """The shared anime-absolute helper means the season-relative logic applies to the E-prefixed
        form too: a title whose alias pins season 2 maps 'Show II E05' -> S2E5 (not series-absolute)."""
        from sickchill.oldbeard import db, name_cache

        self._make_anime()
        cache_db = db.DBConnection("cache.db")
        cache_db.action(
            "INSERT INTO scene_exceptions (indexer_id, show_name, season, custom) VALUES (?, ?, ?, ?)",
            [1, "show name II", 2, 1],
        )
        name_cache.build_name_cache()

        result = parser.NameParser().parse("show name II E05 MULTi 1080p WEB x264-AMB3R")
        self.assertEqual(result.show.indexerid, 1)
        self.assertEqual(result.season_number, 2)
        self.assertEqual(result.episode_numbers, [5])

    def test_non_anime_seasonless_number_left_unmatched(self):
        """The new branch is gated on is_anime: a NON-anime show's season-less E04 must NOT be
        reinterpreted as a series-absolute number (behavior unchanged -- season stays None)."""
        from sickchill.oldbeard import db, name_cache

        # Explicitly mark the show NON-anime (the test DB row can carry anime=1 from a sibling test,
        # since the harness reuses the same indexer_id across tests). Set an absolute_number to prove it
        # is not consulted for a non-anime show. Use a release string no other test parses, because the
        # process-global name_parser_cache is keyed by name and would otherwise return a sibling's result.
        self.show.anime = 0
        self.show.save_to_db()
        main_db = db.DBConnection()
        main_db.action("UPDATE tv_episodes SET absolute_number = 7 WHERE showid = 1 AND indexer = 1 AND season = 1 AND episode = 7")
        name_cache.build_name_cache()

        result = parser.NameParser().parse("show name E07 MULTi 1080p WEB x264-AMB3R")
        self.assertEqual(result.show.indexerid, 1)
        self.assertIsNone(result.season_number)


class ResolutionNotSeasonEpisodeTests(conftest.SickChillTestDBCase):
    """A WxH resolution token (1440x1080p, 1920x1080) must never parse as season x episode.

    Live failure 2026-07-11: 'Dirty Pair Flash 2 - 04 (BDRip 1440x1080p ...)' parsed as S1440E1080
    via the 'anime SxEE' regex, so six valid files sat unprocessable in the download folder while
    their episodes reverted to Wanted and re-downloaded.
    """

    def __init__(self, something):
        super().__init__(something)
        super().setUp()
        self.show = tv.TVShow(1, 1, "en")

    def tearDown(self):
        parser.name_parser_cache.data.clear()

    def _parse(self, name, series_name, anime):
        self.show.name = series_name
        self.show.anime = int(anime)
        name_parser = parser.NameParser(True, show_object=self.show)
        return name_parser.parse(name)

    def test_anime_resolution_is_not_season_episode(self):
        name = "Dirty Pair Flash 2 - 04 (BDRip 1440x1080p x265 HEVC FLAC, AC-3 2.0x2)(Dual Audio)[sxales]"
        try:
            result = self._parse(name, "Dirty Pair Flash", anime=True)
        except (parser.InvalidNameException, parser.InvalidShowException):
            return  # refusing to parse is acceptable; inventing S1440E1080 is not
        self.assertNotEqual(result.season_number, 1440)
        self.assertNotIn(1080, result.episode_numbers or [])

    def test_bare_resolution_is_not_season_episode(self):
        try:
            result = self._parse("Some Show 1920x1080 WEBRip xViD-GRP", "Some Show", anime=False)
        except (parser.InvalidNameException, parser.InvalidShowException):
            return
        self.assertNotEqual(result.season_number, 1920)
        self.assertNotIn(1080, result.episode_numbers or [])

    def test_fov_still_parses(self):
        result = self._parse("Some Fov Show 3x07 Source-GRP", "Some Fov Show", anime=False)
        self.assertEqual(result.season_number, 3)
        self.assertEqual(result.episode_numbers, [7])

    def test_backtracking_cannot_slice_a_resolution(self):
        # (?![pi]) alone is defeated by backtracking: 64x480p would parse as S64E48 with '0p'
        # left over, and 1x02p as S01E00. The guard must reject the whole run, not a slice.
        for name, series in (("Some Show 64x480p WEBRip", "Some Show"), ("Some Show 1x02p Oddity", "Some Show")):
            try:
                result = self._parse(name, series, anime=False)
            except (parser.InvalidNameException, parser.InvalidShowException):
                continue
            self.assertNotIn((result.season_number, tuple(result.episode_numbers or ())), [(64, (48,)), (1, (0,))], name)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        SUITE = unittest.TestLoader().loadTestsFromName("name_parser_tests.BasicTests.test_" + sys.argv[1])
        unittest.TextTestRunner(verbosity=2).run(SUITE)
    else:
        SUITE = unittest.TestLoader().loadTestsFromTestCase(BasicTests)
    unittest.TextTestRunner(verbosity=2).run(SUITE)

    SUITE = unittest.TestLoader().loadTestsFromTestCase(ComboTests)
    unittest.TextTestRunner(verbosity=2).run(SUITE)

    SUITE = unittest.TestLoader().loadTestsFromTestCase(UnicodeTests)
    unittest.TextTestRunner(verbosity=2).run(SUITE)

    SUITE = unittest.TestLoader().loadTestsFromTestCase(FailureCaseTests)
    unittest.TextTestRunner(verbosity=2).run(SUITE)

    SUITE = unittest.TestLoader().loadTestsFromTestCase(AnimeTests)
    unittest.TextTestRunner(verbosity=2).run(SUITE)
