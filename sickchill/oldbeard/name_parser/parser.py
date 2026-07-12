import os
import os.path
import re
import time
from collections import OrderedDict
from datetime import date
from operator import attrgetter
from threading import Lock
from typing import TYPE_CHECKING, Any

from dateutil.parser import parse

import sickchill
from sickchill import logger
from sickchill.helper.common import remove_extension
from sickchill.oldbeard import common, db, helpers, scene_exceptions, scene_numbering
from sickchill.oldbeard.name_parser import regexes

if TYPE_CHECKING:
    from typing import List

    from sickchill.tv import TVShow


# Confident explicit-season detection for anime release names. Shared by the parser (A1: read the
# token as the season so search maps "K-ON S2 - 01" -> S2E01) and by GenericProvider's cross-season
# guards (A2). These depend only on ``re`` and scene_exceptions so they stay leaf code:
# GenericProvider/tvcache already import this module, and nothing here imports them, so no import
# cycle is introduced.
_ANIME_SEASON_BRACKET_RE = re.compile(r"[\[(][^\])]*[\])]")
# S<n> / S0<n> / Season <n> as a STANDALONE token: not preceded or followed by an alnum, so SxxExx
# codes (S02E03) and stray tags ("S2Productions" / "S2x264") are not mistaken for a bare season.
_ANIME_SEASON_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])S(?:eason)?[ ._]?(\d{1,2})(?![A-Za-z0-9])", re.IGNORECASE)
# A bracket/paren group whose ENTIRE content is a season token ("(S2)", "[S02]", "(Season 2)").
# Group/codec blocks ("[S2Productions]") never match because of the closing anchor.
_TOKEN_ONLY_SEASON_BLOCK_RE = re.compile(r"[\[(]\s*S(?:eason)?[ ._]?(\d{1,2})\s*[\])]", re.IGNORECASE)

# --- Franchise-collision helpers (see .codex-reviews/franchise_collision_plan.md) -------------
# A sub-series in a franchise restarts its own numbering, so a title carrying a disambiguator the
# resolved show cannot explain (a year, a roman numeral, a season token for another season) must
# not be mapped onto that show. franchise_gate() below is the single decision function; the
# parser, search, cache reads, the AI matchers, and post-processing all call it.

# The leading [\...] block is the release group by convention and never carries franchise meaning.
_LEADING_GROUP_BLOCK_RE = re.compile(r"^\s*\[[^\]]*\]")
_SQUARE_BLOCK_RE = re.compile(r"\[[^\]]*\]")
# A standalone year: digit-bounded, and excluded when written as a resolution -- 1920 and 2160
# are year-shaped ("1920x1080", "2160p"), so a resolution glyph on either side disqualifies.
_TITLE_YEAR_RE = re.compile(r"(?<!\d)(?<![xX×])(?:19|20)\d{2}(?!\d)(?![xXpi×])")
# A bracket/paren group whose entire content is a year ("[2015]", "(2015)").
_TOKEN_ONLY_YEAR_BLOCK_RE = re.compile(r"[\[(]\s*((?:19|20)\d{2})\s*[\])]")
# Sequel markers: uppercase roman numerals II..IX as standalone tokens. I and X are deliberately
# excluded (single letters collide with initialisms far too often), and the match is
# case-sensitive because sequel numerals are written uppercase while a lowercase "v" is usually
# "versus" or a version tag.
_ROMAN_MARKER_TOKENS = ("II", "III", "IV", "V", "VI", "VII", "VIII", "IX")
_SXXEYY_TOKEN_RE = re.compile(r"^S\d{1,2}E\d{1,4}$", re.IGNORECASE)
_TITLE_SPAN_TOKEN_RE = re.compile(r"\[[^\]]*\]|\([^)]*\)|\S+")


def extract_title_years(title):
    """
    Standalone 19xx/20xx year tokens that speak to the TITLE's identity: everything outside
    square-bracket metadata blocks, plus any bracket/paren group that is ONLY a year ("[2015]").
    The leading release-group block is exempt.
    """
    if not title:
        return set()
    core = _LEADING_GROUP_BLOCK_RE.sub(" ", title)
    years = {int(match.group(0)) for match in _TITLE_YEAR_RE.finditer(_SQUARE_BLOCK_RE.sub(" ", core))}
    years.update(int(match.group(1)) for match in _TOKEN_ONLY_YEAR_BLOCK_RE.finditer(core))
    return years


def _known_show_names(show):
    """Every raw name the show is known by: canonical, custom, and all scene-exception aliases."""
    names = [name for name in (getattr(show, "name", None), getattr(show, "custom_name", None)) if name]
    try:
        index = scene_exceptions.get_normalized_alias_index(show.indexerid)
    except Exception as error:
        logger.debug(f"Could not load the alias index for {getattr(show, 'name', show)}: {error}")
        index = {}
    for rows in index.values():
        names.extend(raw_name for raw_name, _, _ in rows)
    return names


def extract_sequel_markers(title, show=None):
    """
    Roman-numeral sequel markers (II..IX) found in the series-title span of a release name.

    The span walk terminates at the first episode-like token -- a standalone 1-4 digit number
    (bare or bracketed like "[03]") or an SxxEyy code -- UNLESS that number appears as a
    standalone token in one of the show's known names ("86 II - 01" must walk past the "86").
    Within the span, bracket groups are metadata and skipped, except a token-only bracketed
    numeral ("[II]"), which counts. The leading release-group block is always exempt.
    """
    if not title:
        return set()

    known_numbers = set()
    if show is not None:
        for name in _known_show_names(show):
            known_numbers.update(re.findall(r"(?<![A-Za-z0-9])\d{1,4}(?![A-Za-z0-9])", name))

    core = _LEADING_GROUP_BLOCK_RE.sub(" ", title)
    markers = set()
    for match in _TITLE_SPAN_TOKEN_RE.finditer(core):
        token = match.group(0)
        if token[0] in "[(":
            inner = token[1:-1].strip()
            if inner in _ROMAN_MARKER_TOKENS:
                markers.add(inner)
            elif inner.isdigit() and 1 <= len(inner) <= 4 and inner.lstrip("0") not in known_numbers and inner not in known_numbers:
                break
            continue
        # Bare token: strip common joining punctuation off the edges before classifying.
        word = token.strip(".,:;-_~")
        if not word:
            continue
        if _SXXEYY_TOKEN_RE.match(word):
            break
        if word.isdigit() and 1 <= len(word) <= 4:
            if word in known_numbers or word.lstrip("0") in known_numbers:
                continue
            break
        if word in _ROMAN_MARKER_TOKENS:
            markers.add(word)
    return markers


def _padded_contains(needle, haystack):
    """Token-boundary containment for normalized names ('k on' must not match inside 'back onto')."""
    return f" {needle} " in f" {haystack} "


def _family_seasons(rows):
    """
    The concrete seasons an alias family pins, with the user's rows overriding synced ones:
    if any row is custom/blessed, only those count (the repair channel for polluted sync data).
    """
    customs = {season for _, season, custom in rows if custom in (scene_exceptions.CUSTOM_USER, scene_exceptions.CUSTOM_BLESSED) and season != -1}
    if customs:
        return customs
    return {season for _, season, custom in rows if season != -1}


def alias_season_pins(text, show):
    """
    The seasons pinned by the LONGEST scene-exception alias contained in ``text``, as
    ``(seasons, conflicted)``, or None when no alias matches or only season -1 rows do.
    Family = every exception row sharing the matched alias's normalized form, so "II - Isekai"
    and "II Isekai" (the same alias written two ways, tagged to different seasons by polluted
    upstream data) are seen as ONE conflicted family.
    """
    if not text or not show:
        return None
    try:
        index = scene_exceptions.get_normalized_alias_index(show.indexerid)
    except Exception as error:
        logger.debug(f"Could not load the alias index for {getattr(show, 'name', show)}: {error}")
        return None

    normalized_text = scene_exceptions.normalize_alias_name(text)
    best = None
    for normalized in index:
        if _padded_contains(normalized, normalized_text):
            if best is None or len(normalized) > len(best):
                best = normalized
    if best is None:
        return None

    seasons = _family_seasons(index[best])
    if not seasons:
        return None
    return seasons, len(seasons) > 1


def franchise_gate(title, show, indexer_season, scene_season=None):
    """
    THE franchise-consistency decision: may ``title`` be mapped to ``indexer_season`` of ``show``?

    Returns (allowed, reason). Reject-only -- a False verdict never re-files anything, it only
    stops a mapping. Signals are compared in their own numbering namespace: alias/year pins are
    XEM origin=tvdb data (indexer seasons); an explicit season token is release text (compared
    against ``scene_season`` when the caller knows it, else ``indexer_season``). Non-anime shows
    always pass; with no exception data at all, the year/marker/token rules still fail closed on
    disambiguated titles.
    """
    if not title or not show or not getattr(show, "is_anime", False):
        return True, None

    try:
        index = scene_exceptions.get_normalized_alias_index(show.indexerid)
    except Exception as error:
        logger.debug(f"Could not load the alias index for {getattr(show, 'name', show)}: {error}")
        index = {}

    normalized_title = scene_exceptions.normalize_alias_name(title)
    best_alias = None
    for normalized in index:
        if _padded_contains(normalized, normalized_title):
            if best_alias is None or len(normalized) > len(best_alias):
                best_alias = normalized

    # 0 doubles as "no scene mapping" in tv_episodes.scene_season, so only a positive scene
    # season replaces the indexer season for release-text comparisons (S0 specials compare the
    # same either way, since their indexer season is also 0).
    release_space_season = scene_season if scene_season else indexer_season

    # 1) Explicit season token. Release text is the most trustworthy signal (rank 1); it must
    #    match the release-space season, full stop -- even when an alias embeds the same token
    #    with a different tag (a scene name "Show S2" that XEM maps to indexer S3 is legitimate
    #    ONLY when actual scene numbering says so, and then the caller's scene_season already
    #    matches the token; without that data, refusing beats trusting the rank-3 alias tag).
    token_season = extract_explicit_anime_season(title)
    if token_season is not None and release_space_season is not None and token_season != release_space_season:
        return False, _("the title names season {token_season} but the mapping is to season {mapped}").format(
            token_season=token_season, mapped=release_space_season
        )

    # 2) Years: neutral when the show itself explains them, season-checked when an alias carries
    #    them, veto when nothing does.
    show_names_text = " ".join(name for name in (getattr(show, "name", None), getattr(show, "custom_name", None)) if name)
    show_years = {int(match.group(0)) for match in _TITLE_YEAR_RE.finditer(show_names_text)}
    for year in sorted(extract_title_years(title)):
        if getattr(show, "startyear", None) and year == show.startyear:
            continue
        if year in show_years:
            continue
        year_re = re.compile(rf"(?<!\d){year}(?!\d)")
        pinned = set()
        year_in_alias = False
        for normalized, rows in index.items():
            if year_re.search(normalized):
                year_in_alias = True
                pinned |= _family_seasons(rows)
        if year_in_alias:
            if not pinned:
                continue
            if indexer_season is not None and indexer_season in pinned:
                continue
            return False, _("the year {year} in the title belongs to season(s) {seasons}, not season {mapped}").format(
                year=year, seasons=sorted(pinned), mapped=indexer_season
            )
        return False, _("the title carries the year {year}, which is not this show's year ({startyear}) or any known alias").format(
            year=year, startyear=getattr(show, "startyear", None)
        )

    # 3) Alias pin: a conflicted family vetoes outright (the season tags contradict each other,
    #    so every deterministic guess is a coin flip); a clean pin must include the mapping.
    if best_alias is not None:
        seasons = _family_seasons(index[best_alias])
        if len(seasons) > 1:
            return False, _("the alias '{alias}' is season-tagged to multiple seasons {seasons} in scene exceptions; add a custom exception to pin the right one").format(
                alias=best_alias, seasons=sorted(seasons)
            )
        if seasons and indexer_season is not None and indexer_season not in seasons:
            return False, _("the alias '{alias}' pins season {seasons}, not season {mapped}").format(
                alias=best_alias, seasons=sorted(seasons), mapped=indexer_season
            )

    # 4) Sequel markers must be explained by the show's own names or by a matched, season-pinning
    #    alias family; when pins exist they must include the mapping.
    for marker in sorted(extract_sequel_markers(title, show)):
        if re.search(rf"(?<![A-Za-z0-9]){marker}(?![A-Za-z0-9])", show_names_text):
            continue
        marker_lower = marker.lower()
        pins = set()
        marker_matched = False
        for normalized, rows in index.items():
            if _padded_contains(normalized, normalized_title) and _padded_contains(marker_lower, normalized):
                marker_matched = True
                pins |= _family_seasons(rows)
        if pins:
            if indexer_season is not None and indexer_season not in pins:
                return False, _("the sequel marker '{marker}' belongs to season(s) {seasons}, not season {mapped}").format(
                    marker=marker, seasons=sorted(pins), mapped=indexer_season
                )
            continue
        if marker_matched:
            # Matched only season -1 aliases: the marker is known but pins nothing, so a mapping
            # would still be the coin flip this gate exists to forbid.
            return False, _("the sequel marker '{marker}' matches only season-less aliases; add a custom exception to pin its season").format(marker=marker)
        return False, _("the title carries the sequel marker '{marker}', which no known name of this show explains").format(marker=marker)

    return True, None


# The most episodes a single video file may legitimately contain. Already enforced for ``ep_num``
# ranges; also applied to absolute ranges so a batch/season-pack name is not read as one huge file.
MAX_MULTI_EPISODES = 4

# The range of "N" we will read as a season in a "<Title> N - <episode>" release name. Season 1 is
# excluded on purpose: "Show 1 - 12" is overwhelmingly a 1-12 batch, while a genuine season-1 release
# is just "Show - 12". "Mob Psycho 100 - 05" is likewise excluded by the upper bound.
MIN_TITLE_SEASON = 2
MAX_TITLE_SEASON = 9

# A season pack / complete-series marker. "Show 2 - 12 [Batch]" is a season-2 PACK, not episode 12,
# so the season reading must not collapse it to a single episode.
_BATCH_MARKER_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:batch|pack|boxset|box[ ._-]?set|collection|complete|seasons?|vol|volume|s\d+-s?\d+)(?![A-Za-z0-9])",
    re.IGNORECASE,
)

# A number the parser DID NOT consume, sitting right after the number it took as the episode:
# "[Moozzi2] Ajin 2 - 12 (BD ...)" -> the regex takes 2 as the episode and silently drops "- 12".
# A genuine release never leaves a stray " - 12" after its episode number, so this is the tell that
# the leading number was really a season and the parse cannot be trusted as-is.
#
# The trailing number must be followed by an info block ("(BD ...", "[1080p]"), a run of bare release
# tags that reaches the end ("BD 1080p"), or nothing. That keeps a numeric EPISODE TITLE from tripping
# the check: "Show - 05 - 3 Days Later" has "3" followed by an ordinary word, so it is left alone.
#
# The bare-tag branch must consume the WHOLE remainder. Accepting a single tag would misread
# "- 12 Web of Lies" (a real episode title beginning with the word "Web") as a discarded number.
# Longest alternatives come first so "WEB-DL" is not matched as a bare "WEB".
_RELEASE_TAG = r"(?:BluRay|Blu-?Ray|BD-?Rip|BR-?Rip|WEB-?DL|WEB-?Rip|WEB|HDTV|HEVC|AVC|FLAC|AAC|x26[45]|h\.?26[45]|DVD|BD|\d{3,4}[pi])"
_DISCARDED_EPISODE_NUMBER_RE = re.compile(
    rf"""
    ^(?P<lead>[ ._]*)-(?P<trail>[ ._]*)   # a dash separating it from the number we consumed
    (?!(?:1080|720|480)[pi])              # not a resolution label ("- 1080p BluRay")
    (?P<number>\d{{1,4}})(?![0-9])        # a whole number
    (?:v\d)?                              # optional fansub version tag ("- 12v2")
    (?:
        [ ._-]*[(\[]                                        # an info block "(BD ..." / "[1080p]"
      | [ ._-]+(?:{_RELEASE_TAG}(?![A-Za-z0-9])[ ._-]*)+$   # only release tags, to the end
      | [ ._-]*$                                            # or nothing at all
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)


def extract_explicit_anime_season(title):
    """
    Return a CONFIDENTLY-detected explicit season number from an anime release name, else None.

    Confident tokens only: ``S2`` / ``S02`` / ``Season 2`` (optionally space/dot/underscore
    separated). Deliberately does NOT treat as a season: a full ``SxxExx`` episode code, Roman
    numerals (``II``/``III`` — left to the parser's scene-exception handling), or a bare trailing
    number. Bracketed/parenthesized tags (release group, codec, resolution) are stripped first so a
    group like ``[S2Productions]`` is not mistaken for a season marker.
    """
    if not title:
        return None
    core = _ANIME_SEASON_BRACKET_RE.sub(" ", title)
    match = _ANIME_SEASON_TOKEN_RE.search(core)
    if match:
        return int(match.group(1))
    # A season token that is a bracket/paren group's ENTIRE content ("(S2)", "[Season 2]") is just
    # as confident; the bracket-strip above removed it, so look for it separately. The leading
    # release-group block is exempt by convention.
    match = _TOKEN_ONLY_SEASON_BLOCK_RE.search(_LEADING_GROUP_BLOCK_RE.sub(" ", title))
    return int(match.group(1)) if match else None


def strip_explicit_anime_season(title):
    """
    Remove a confident explicit season token from a title, collapsing whitespace.

    Used by the parser to verify the remaining title still resolves to the SAME show before trusting
    the token as a season (so a number that is genuinely part of the canonical title is not mistaken
    for a season marker).
    """
    if not title:
        return title
    # Block form first: stripping the bare token out of "(S2)" first would leave an empty "()".
    stripped = _ANIME_SEASON_TOKEN_RE.sub(" ", _TOKEN_ONLY_SEASON_BLOCK_RE.sub(" ", title))
    return re.sub(r"\s+", " ", stripped).strip()


class NameParser(object):
    ALL_REGEX = 0
    NORMAL_REGEX = 1
    ANIME_REGEX = 2

    def __init__(self, filename: bool = True, show_object=None, try_indexers: bool = False, naming_pattern: bool = False, parse_method: str = None):
        self.filename: bool = filename
        self.show_object: TVShow = show_object
        self.try_indexers: bool = try_indexers
        self.compiled_regexes: List = []

        self.naming_pattern = naming_pattern

        if (self.show_object and not self.show_object.is_anime) or parse_method == "normal":
            self._compile_regexes(self.NORMAL_REGEX)
        elif (self.show_object and self.show_object.is_anime) or parse_method == "anime":
            self._compile_regexes(self.ANIME_REGEX)
        else:
            self._compile_regexes(self.ALL_REGEX)

    @staticmethod
    def clean_series_name(series_name):
        """Cleans up series name by removing any . and _
        characters, along with any trailing hyphens.

        Is basically equivalent to replacing all _ and . with a
        space, but handles decimal numbers in string.
        Stolen from dbr's tvnamer
        """

        series_name = re.sub(r"(\D)\.(?!\s)(\D)", "\\1 \\2", series_name)
        series_name = re.sub(r"(\d)\.(\d{4})", "\\1 \\2", series_name)  # if it ends in a year then don't keep the dot
        series_name = re.sub(r"(\D)\.(?!\s)", "\\1 ", series_name)
        series_name = re.sub(r"\.(?!\s)(\D)", " \\1", series_name)
        series_name = series_name.replace("_", " ")
        series_name = re.sub(r"-$", "", series_name)
        series_name = re.sub(r"^\[.*]", "", series_name)
        return series_name.strip()

    def _compile_regexes(self, regex_mode):
        if regex_mode == self.ANIME_REGEX:
            dbg_str = "ANIME"
            uncompiled_regex = [regexes.anime_regexes]
        elif regex_mode == self.NORMAL_REGEX:
            dbg_str = "NORMAL"
            uncompiled_regex = [regexes.normal_regexes]
        else:
            dbg_str = "ALL"
            uncompiled_regex = [regexes.normal_regexes, regexes.anime_regexes]

        for regexItem in uncompiled_regex:
            for cur_pattern_num, (cur_pattern_name, cur_pattern) in enumerate(regexItem):
                try:
                    cur_regex = re.compile(cur_pattern, re.VERBOSE | re.I)
                except re.error as error_message:
                    logger.info(f"WARNING: Invalid episode_pattern using {dbg_str} regexes, {error_message}. {cur_pattern}")
                else:
                    self.compiled_regexes.append((cur_pattern_num, cur_pattern_name, cur_regex))

    def _parse_string(self, name, skip_scene_detection=False):
        if not name:
            return

        matches = []
        best_result = None

        for cur_regex_num, cur_regex_name, cur_regex in self.compiled_regexes:
            match = cur_regex.match(name)

            if not match:
                continue

            result = ParseResult(name)
            result.which_regex = [cur_regex_name]
            result.score = 0 - cur_regex_num

            named_groups = list(match.groupdict())

            if "series_name" in named_groups:
                result.series_name = match.group("series_name")
                if result.series_name:
                    result.series_name = self.clean_series_name(result.series_name)
                    result.score += 1

            if "series_num" in named_groups and match.group("series_num"):
                result.score += 1

            if "season_num" in named_groups:
                tmp_season = int(match.group("season_num"))
                if cur_regex_name == "bare" and tmp_season in (19, 20):
                    continue
                if cur_regex_name == "fov" and tmp_season > 500:
                    continue

                result.season_number = tmp_season
                result.score += 1

            if "ep_num" in named_groups:
                ep_num = self._convert_number(match.group("ep_num"))
                if "extra_ep_num" in named_groups and match.group("extra_ep_num"):
                    tmp_episodes = list(range(ep_num, self._convert_number(match.group("extra_ep_num")) + 1))
                    if len(tmp_episodes) > MAX_MULTI_EPISODES:
                        continue
                else:
                    tmp_episodes = [ep_num]

                result.episode_numbers = tmp_episodes
                result.score += 3

            if "ep_ab_num" in named_groups:
                ep_ab_num = self._convert_number(match.group("ep_ab_num"))
                if "extra_ab_ep_num" in named_groups and match.group("extra_ab_ep_num"):
                    tmp_ab_episodes = list(range(ep_ab_num, self._convert_number(match.group("extra_ab_ep_num")) + 1))
                    # A season pack / batch folder ("[Group] Ajin 2-12", "Date A Live IV-1-12") is not a
                    # multi-episode FILE. Absolute ranges were uncapped while ep_num ranges above were
                    # capped at 4, so a batch name expanded into a 11-12 episode span, and the generated
                    # destination filename -- which concatenates every episode title in the span -- blew
                    # past the 255-byte filesystem limit. Apply the same cap to absolute ranges.
                    if len(tmp_ab_episodes) > MAX_MULTI_EPISODES:
                        continue
                    result.ab_episode_numbers = tmp_ab_episodes
                    result.score += 1
                else:
                    result.ab_episode_numbers = [ep_ab_num]
                result.score += 1

            self._check_discarded_episode_number(result, name, match)

            if "air_date" in named_groups:
                air_date = match.group("air_date")
                try:
                    # Workaround for shows that get interpreted as 'air_date' incorrectly.
                    # Shows so far are 11.22.63 and 9-1-1
                    excluded_shows = ["112263", "911"]
                    assert re.sub(r"\D*", "", air_date) not in excluded_shows

                    try:
                        check: date = parse(air_date, fuzzy=True).date()
                        # Make sure a 20th century date isn't returned as a 21st century date
                        # 1 Year into the future (No releases should be coming out a year ahead of time, that's just insane)
                        if check > check.today() and (check - check.today()).days // 365 > 1:
                            check = check.replace(year=check.year - 100)

                        result.air_date = check
                        result.score += 1
                    except Exception as error:
                        logger.debug(error)
                        continue
                except Exception as error:
                    logger.debug(error)
                    continue

            if "extra_info" in named_groups:
                tmp_extra_info = match.group("extra_info")

                # Show.S04.Special or Show.S05.Part.2.Extras is almost certainly not every episode in the season
                if tmp_extra_info and cur_regex_name == "season_only" and re.search(r"([. _-]|^)(special|extra)s?\w*([. _-]|$)", tmp_extra_info, re.I):
                    continue
                result.extra_info = tmp_extra_info
                result.score += 1

            if "release_group" in named_groups:
                result.release_group = match.group("release_group")
                result.score += 1

            if "version" in named_groups:
                # assigns version to anime file if detected using anime regex. Non-anime regex receives -1
                version = match.group("version")
                if version:
                    result.version = version
                else:
                    result.version = 1
            else:
                result.version = -1

            matches.append(result)

        # only get matches with series_name
        # TODO: This makes tests fail when checking filenames that do not include the show name (refresh, force update, etc)
        # matches = [x for x in matches if x.series_name]

        if matches:
            # pick the best match with the highest score based on placement
            best_result = max(sorted(matches, reverse=True, key=attrgetter("which_regex")), key=attrgetter("score"))

            show = None
            if best_result and best_result.series_name and not self.naming_pattern:
                # try and create a show object for this result
                show = helpers.get_show(best_result.series_name, self.try_indexers)

            # confirm passed in show object indexer id matches result show object indexer id
            if show:
                if self.show_object and show.indexerid != self.show_object.indexerid:
                    show = None
                best_result.show = show
            elif self.show_object and not show:
                best_result.show = self.show_object

            # Only allow anime matches if resolved show or specified show is anime
            best_result = self.check_anime_preferred(best_result, matches)

            # if this is a naming pattern test or result doesn't have a show object then return best result
            if not best_result.show or self.naming_pattern:
                return best_result

            # get quality
            best_result.quality = common.Quality.nameQuality(name, best_result.show.is_anime)

            new_episode_numbers = []
            new_season_numbers = []
            new_absolute_numbers = []

            # The season as the RELEASE TEXT wrote it, before any scene->indexer conversion; the
            # franchise gate compares release-text signals (explicit season tokens) against this
            # namespace, and XEM alias pins against the converted indexer season.
            raw_release_season = None

            # "<Title> N - M" ("[Moozzi2] Ajin 2 - 12"): the regex read the season as the episode and
            # dropped the real one. Take the season reading only when the database confirms it exists;
            # otherwise the result stays ambiguous and post-processing will refuse it rather than guess.
            if best_result.ambiguous and self._resolve_title_season(best_result, new_season_numbers, new_episode_numbers, new_absolute_numbers):
                pass

            # if we have an air-by-date show then get the real season/episode numbers
            elif best_result.is_air_by_date:
                airdate = best_result.air_date.toordinal()
                main_db_con = db.DBConnection()
                sql_result = main_db_con.select(
                    "SELECT season, episode FROM tv_episodes WHERE showid = ? and indexer = ? and airdate = ?",
                    [best_result.show.indexerid, best_result.show.indexer, airdate],
                )

                season_number = None
                episode_numbers = []

                if sql_result:
                    season_number = int(sql_result[0][0])
                    episode_numbers = [int(sql_result[0][1])]

                if season_number is None or not episode_numbers:
                    try:
                        episode_object = sickchill.indexer.episode(best_result.show, firstAired=best_result.air_date)
                        season_number = episode_object["airedSeason"]
                        episode_numbers = [episode_object["airedEpisode"]]
                    except Exception as error:
                        logger.debug(error)
                        logger.warning(f"Unable to find episode with date {best_result.air_date} for show {best_result.show.name}, skipping")
                        episode_numbers = []

                for epNo in episode_numbers:
                    s = season_number
                    e = epNo

                    if best_result.show.is_scene:
                        (s, e) = scene_numbering.get_indexer_numbering(best_result.show.indexerid, best_result.show.indexer, season_number, epNo)
                    new_episode_numbers.append(e)
                    new_season_numbers.append(s)

            elif best_result.show.is_anime and best_result.ab_episode_numbers:
                self._resolve_anime_absolute(
                    best_result, best_result.ab_episode_numbers, new_season_numbers, new_episode_numbers, new_absolute_numbers, skip_scene_detection
                )

            elif best_result.show.is_anime and best_result.episode_numbers and best_result.season_number is None:
                # A season-less episode number on an anime release is a series-wide ABSOLUTE number. This
                # happens when a NORMAL regex (e.g. "Show.Name.E04.quality" via no_season_general) claims the
                # number as ep_num with no season instead of an anime regex's ep_ab_num. Without this branch
                # such a result kept season=None, matched none of the conversion branches, and could not be
                # filed (it fell through to the AI fallback). Route the parsed episode numbers through the
                # SAME anime-absolute resolution as ab_episode_numbers so scene-season / explicit-season (A1)
                # / season-relative handling all apply. Gated on `season_number is None` (NOT a falsy check)
                # so anime season-0 specials like S00E01 are left untouched.
                self._resolve_anime_absolute(
                    best_result, best_result.episode_numbers, new_season_numbers, new_episode_numbers, new_absolute_numbers, skip_scene_detection
                )

            elif best_result.season_number and best_result.episode_numbers:
                raw_release_season = best_result.season_number
                for epNo in best_result.episode_numbers:
                    s = best_result.season_number
                    e = epNo

                    if best_result.show.is_scene and not skip_scene_detection:
                        (s, e) = scene_numbering.get_indexer_numbering(best_result.show.indexerid, best_result.show.indexer, best_result.season_number, epNo)
                    if best_result.show.is_anime:
                        a = helpers.get_absolute_number_from_season_and_episode(best_result.show, s, e)
                        if a:
                            new_absolute_numbers.append(a)

                    new_episode_numbers.append(e)
                    new_season_numbers.append(s)

            # need to do a quick sanity check regex.  It's possible that we now have episodes
            # from more than one season (by tvdb numbering), and this is just too much
            # for oldbeard, so we'd need to flag it.
            new_season_numbers = list(set(new_season_numbers))  # remove duplicates
            if len(new_season_numbers) > 1:
                raise InvalidNameException(
                    f"Scene numbering results episodes from seasons {new_season_numbers}, (i.e. more than one) and sickchill does not support this. Sorry."
                )

            # I guess it's possible that we'd have duplicate episodes too, so let's eliminate them
            new_episode_numbers = sorted(set(new_episode_numbers))

            # maybe even duplicate absolute numbers so why not do them as well
            new_absolute_numbers = list(set(new_absolute_numbers))
            new_absolute_numbers.sort()

            if new_absolute_numbers:
                best_result.ab_episode_numbers = new_absolute_numbers

            if new_season_numbers and new_episode_numbers:
                best_result.episode_numbers = new_episode_numbers
                best_result.season_number = new_season_numbers[0]

            if best_result.show.is_scene and not skip_scene_detection:
                logger.debug(f"Converted parsed result {best_result.original_name} into {best_result}")

            # Franchise chokepoint: EVERY anime mapping this method can produce (SxxEyy branch,
            # anime-absolute branch, title-season resolution) passes the gate here, after the
            # existing ambiguity resolver has had its chance. A refusal marks the result ambiguous
            # AND clears the mapped numbers -- consumers that never learned about `ambiguous`
            # (nzbSplitter, the proper finder, rescans) must not find an actionable mapping on a
            # vetoed result. The release-text namespace for the explicit-token rule is the raw
            # pre-conversion season (SxxEyy branch) or, for the absolute branch on a scene show,
            # the mapped episode's scene season; alias/year pins compare against the resolved
            # indexer season inside the gate.
            if best_result.show.is_anime and not best_result.ambiguous and best_result.season_number is not None and best_result.episode_numbers:
                gate_scene_season = raw_release_season
                if gate_scene_season is None and best_result.show.is_scene and not skip_scene_detection and best_result.episode_numbers:
                    try:
                        gate_scene_season = scene_numbering.get_scene_numbering(
                            best_result.show.indexerid, best_result.show.indexer, best_result.season_number, best_result.episode_numbers[0]
                        )[0]
                    except Exception as error:
                        logger.debug(f"Could not derive the scene season for the franchise gate: {error}")
                if not gate_scene_season and best_result.scene_season and best_result.scene_season > 0:
                    gate_scene_season = best_result.scene_season
                allowed, reason = franchise_gate(best_result.original_name, best_result.show, best_result.season_number, scene_season=gate_scene_season)
                if not allowed:
                    best_result.ambiguous = True
                    best_result.ambiguity_reason = reason
                    best_result.season_number = None
                    best_result.episode_numbers = []
                    best_result.ab_episode_numbers = []
                    logger.debug(f"Refusing the parsed mapping for {best_result.original_name}: {reason}")

        # CPU sleep
        time.sleep(0.02)

        return best_result

    @staticmethod
    def _check_discarded_episode_number(result, name, match):
        """
        Flag a match that consumed a season-looking number as the episode and dropped the real one.

        "[Moozzi2] Ajin 2 - 12 (BD ...)" is *Ajin season 2, episode 12*, but the lazy series_name
        stops at "Ajin", so the regex takes 2 as the episode and lets ".*?" swallow "- 12". Left
        alone that files season-2 content into season 1 episode 2. Marking the result ambiguous lets
        the caller either resolve it (``_resolve_title_season``) or refuse to touch the file.
        """
        named_groups = match.groupdict()
        if named_groups.get("air_date"):
            return

        consumed = [match.end(group) for group in ("ep_num", "extra_ep_num", "ep_ab_num", "extra_ab_ep_num") if named_groups.get(group)]
        if not consumed:
            return

        leftover = _DISCARDED_EPISODE_NUMBER_RE.match(name[max(consumed) :])
        if not leftover:
            return

        result.discarded_number = int(leftover.group("number"))
        # "Ajin 2 - 12" (spaced dash) is the fansub season/episode form; "Ajin 2-12" and
        # "Date A Live IV-1-12" (bare hyphen) are batch ranges. Both are ambiguous, but only the
        # spaced form may be resolved as a season -- see _resolve_title_season.
        result.discarded_number_spaced = bool(leftover.group("lead")) and bool(leftover.group("trail"))
        result.ambiguous = True
        result.ambiguity_reason = f"parsed episode number leaves a discarded '{leftover.group(0).strip()}' -- the leading number may be a season"

    def _resolve_title_season(self, best_result, new_season_numbers, new_episode_numbers, new_absolute_numbers):
        """
        Read "<Title> N - M" as season N, episode M, but only when the database confirms it.

        Fansub groups publish per-season Blu-rays as "<Title> <season> - <episode>". Accepting the
        season reading on faith would be exactly as wrong as the parse we are correcting, so every
        step is checked: N must be a plausible season, the show must be anime, and season N episode M
        must already exist. Anything less leaves ``best_result.ambiguous`` set, and the file is not
        post-processed. Returns True when the mapping was accepted.
        """
        show = best_result.show
        if not show or not show.is_anime or best_result.discarded_number is None:
            return False

        # Only the spaced "Title 2 - 12" form carries a season. A bare hyphen ("Ajin 2-12",
        # "Date A Live IV-1-12") is a batch/season-pack range and must never collapse to one episode.
        if not best_result.discarded_number_spaced:
            return False

        # Nor may a spaced name that advertises itself as a pack: "[Group] Show 2 - 12 [BD][Batch]"
        # is all twelve episodes of season 2, not episode 12. The database can confirm that S02E12
        # exists; it cannot tell us the release holds only that episode.
        if _BATCH_MARKER_RE.search(best_result.original_name or ""):
            return False

        if len(best_result.episode_numbers) == 1:
            season = best_result.episode_numbers[0]
        elif len(best_result.ab_episode_numbers) == 1:
            season = best_result.ab_episode_numbers[0]
        else:
            return False

        episode = best_result.discarded_number
        if not MIN_TITLE_SEASON <= season <= MAX_TITLE_SEASON:
            return False

        main_db_con = db.DBConnection()
        if not main_db_con.select_one(
            "SELECT 1 FROM tv_episodes WHERE showid = ? AND indexer = ? AND season = ? AND episode = ?",
            [show.indexerid, show.indexer, season, episode],
        ):
            return False

        absolute_number = helpers.get_absolute_number_from_season_and_episode(show, season, episode)
        if absolute_number:
            new_absolute_numbers.append(absolute_number)
        new_season_numbers.append(season)
        new_episode_numbers.append(episode)

        # The season reading is verified, so the name is no longer ambiguous.
        best_result.ab_episode_numbers = []
        best_result.ambiguous = False
        best_result.ambiguity_reason = None
        logger.debug(f"Read {best_result.original_name} as {show.name} season {season} episode {episode} (title carries the season)")
        return True

    def _resolve_anime_absolute(self, best_result, numbers, new_season_numbers, new_episode_numbers, new_absolute_numbers, skip_scene_detection):
        """Resolve a list of anime numbers (series-wide absolute, or season-less episode numbers treated as
        such) into season/episode/absolute numbers, appending to the passed-in lists.

        Shared by the ``ab_episode_numbers`` branch and the season-less ``episode_numbers`` branch so both get
        the same scene-season / explicit-season (A1) / season-relative handling.
        """
        best_result.scene_season = scene_exceptions.get_scene_exception_by_name(best_result.series_name)[1]

        # Only treat the release as season-relative when the title maps UNAMBIGUOUSLY to a single
        # specific season for THIS show. The pin comes from the alias FAMILY (every exception row
        # sharing the name's normalized form), not the exact string: XEM simultaneously tags
        # "Mushoku Tensei II - Isekai Ittara Honki Dasu" season 1 and its no-dash twin season 2,
        # and an exact lookup sees only one of them -- a confident, WRONG pin. A conflicted family
        # refuses outright (ambiguous): the season-relative reading and the series-absolute
        # fallback are both known-wrong guesses in that case, and refusing hands the release to
        # the AI matcher (search) or declines the file (post-processing). The user resolves a
        # conflicted family deliberately, with a custom/blessed exception, which overrides synced
        # rows in the family. See franchise_gate / .codex-reviews/franchise_collision_plan.md.
        pins = alias_season_pins(best_result.series_name, best_result.show)
        if pins and pins[1]:
            best_result.ambiguous = True
            best_result.franchise_conflict = True
            best_result.ambiguity_reason = (
                f"the name '{best_result.series_name}' is season-tagged to multiple seasons in scene exceptions; "
                f"add a custom scene exception to pin the right one"
            )
            # Clear the raw numbers too: consumers that never learned about `ambiguous` must not
            # find an actionable mapping on a refused result.
            best_result.season_number = None
            best_result.episode_numbers = []
            best_result.ab_episode_numbers = []
            logger.debug(f"Refusing to resolve {best_result.original_name}: {best_result.ambiguity_reason}")
            return
        season_relative_season = next(iter(pins[0])) if pins else None

        # A1: no scene exception pinned a season, but the title carries a CONFIDENT explicit
        # season token (search-time "K-ON S2 - 01" whose ".S2." was swallowed into the series
        # name and does NOT match the "K-On!! S2" exception). Treat that token as the season --
        # the same season the double-bang PP filename already gets via its exception -- so
        # search maps it to the right season instead of the series-absolute season 1. Gated
        # hard: only when stripping the token STILL resolves to THIS show (so a number that is
        # genuinely part of the title is not mistaken for a season). This only supplies
        # season_relative_season; the per-epAbsNo EXISTS check below (Fix B, using the RAW
        # epAbsNo) still validates (season, episode) and falls back to series-absolute when it
        # does not exist -- A1 adds no new numbering of its own.
        if season_relative_season is None:
            explicit_season = extract_explicit_anime_season(best_result.series_name)
            if explicit_season is not None:
                stripped_name = strip_explicit_anime_season(best_result.series_name)
                stripped_show = helpers.get_show(stripped_name, False) if stripped_name else None
                if stripped_show and stripped_show.indexerid == best_result.show.indexerid:
                    season_relative_season = explicit_season

        main_db_con = db.DBConnection()
        for epAbsNo in numbers:
            a = epAbsNo

            if best_result.show.is_scene and not skip_scene_detection:
                a = scene_numbering.get_indexer_absolute_numbering(
                    best_result.show.indexerid, best_result.show.indexer, epAbsNo, scene_season=best_result.scene_season
                )

            # When the title maps to a single specific season (e.g. "Mushoku Tensei II -
            # Isekai Ittara Honki Dasu" -> season 2), per-season releases like Moozzi2 BDs
            # number episodes within that season, so the parsed number is the season-relative
            # episode (S2E19), NOT a series-wide absolute number. Prefer that mapping when the
            # episode actually exists; otherwise fall back to series-absolute resolution.
            # (Verified directly against tv_episodes so we never create a placeholder episode
            # for a non-existent number.)
            # For a SCENE show, `a` was converted above into a series-wide ABSOLUTE number;
            # the season-relative interpretation must use the RAW parsed number (epAbsNo) as
            # the within-season episode. Using the scene-converted `a` here is a bug: e.g. for
            # K-ON "S2 - 01", epAbsNo=1 -> a=15 (indexer absolute), and since S2E15 exists the
            # check would wrongly map it to S2E15. The scene-converted `a` is only valid for
            # the series-absolute fallback below.
            season_relative = season_relative_season is not None and main_db_con.select_one(
                "SELECT 1 FROM tv_episodes WHERE showid = ? AND indexer = ? AND season = ? AND episode = ?",
                [best_result.show.indexerid, best_result.show.indexer, season_relative_season, epAbsNo],
            )

            if season_relative:
                s, e = season_relative_season, epAbsNo
                season_absolute = helpers.get_absolute_number_from_season_and_episode(best_result.show, s, e)
                if season_absolute:
                    new_absolute_numbers.append(season_absolute)
                new_episode_numbers.append(e)
                new_season_numbers.append(s)
            else:
                (s, e) = helpers.get_all_episodes_from_absolute_number(best_result.show, [a])
                new_absolute_numbers.append(a)
                new_episode_numbers.extend(e)
                new_season_numbers.append(s)

    def check_anime_preferred(self, best_result, matches):
        show = self.show_object or best_result.show
        if (best_result.show and best_result.show.is_anime and not self.show_object) or (self.show_object and self.show_object.is_anime):
            anime_matches = [x for x in matches if "anime" in x.which_regex[0]]
            if anime_matches:
                best_result_anime = max(sorted(anime_matches, reverse=True, key=attrgetter("which_regex")), key=attrgetter("score"))
                if best_result_anime and best_result_anime.series_name:
                    show_anime = helpers.get_show(best_result_anime.series_name)
                    if show_anime and show_anime.indexerid == show.indexerid:
                        best_result_anime.show = show_anime
                        best_result = best_result_anime

        return best_result

    @staticmethod
    def _combine_results(first: Any, second: Any, attr: str) -> Any:
        # if the first doesn't exist then return the second or nothing
        if not first:
            if not second:
                return None
            else:
                return getattr(second, attr)

        # if the second doesn't exist then return the first
        if not second:
            return getattr(first, attr)

        first_value = getattr(first, attr)
        second_value = getattr(second, attr)

        # if first_value is good use it
        if first_value is not None or (isinstance(first_value, list) and first_value):
            return first_value
        # if not use second_value (if second_value isn't set it'll just be default)
        else:
            return second_value

    @staticmethod
    def _to_unicode(obj, encoding="utf-8"):
        if isinstance(obj, bytes):
            obj = str(obj, encoding, "replace")
        return obj

    @staticmethod
    def _convert_number(org_number):
        """
        Convert org_number into an integer
        org_number: integer or representation of a number: string or str
        Try force converting to int first, on error try converting from Roman numerals
        returns integer or 0
        """

        try:
            # try forcing to int
            if org_number:
                number = int(org_number)
            else:
                number = 0

        except Exception as error:
            logger.debug(error)
            # on error try converting from Roman numerals
            roman_to_int_map = (
                ("M", 1000),
                ("CM", 900),
                ("D", 500),
                ("CD", 400),
                ("C", 100),
                ("XC", 90),
                ("L", 50),
                ("XL", 40),
                ("X", 10),
                ("IX", 9),
                ("V", 5),
                ("IV", 4),
                ("I", 1),
            )

            roman_numeral = str(org_number).upper()
            number = 0
            index = 0

            for numeral, integer in roman_to_int_map:
                while roman_numeral[index : index + len(numeral)] == numeral:
                    number += integer
                    index += len(numeral)

        return number

    def parse(self, name, cache_result=True, skip_scene_detection=False):
        name = self._to_unicode(name)

        if self.naming_pattern:
            cache_result = False

        # Captured BEFORE the work: if the alias data changes while this parse is running, the
        # stored result carries the stale generation and no reader will accept it.
        parse_generation = scene_exceptions.exceptions_generation()

        cached = name_parser_cache[name]
        if cached:
            return cached

        # break it into parts if there are any (dirname, file name, extension)
        dir_name, filename = os.path.split(name)

        if self.filename:
            base_filename = remove_extension(filename)
        else:
            base_filename = filename

        # set up a result to use
        final_result = ParseResult(name)

        # try parsing the file name
        filename_result = self._parse_string(base_filename, skip_scene_detection)

        # use only the direct parent dir
        dir_name = os.path.basename(dir_name)

        # parse the dirname for extra info if needed
        dir_name_result = self._parse_string(dir_name, skip_scene_detection)

        # build the ParseResult object
        final_result.air_date = self._combine_results(filename_result, dir_name_result, "air_date")

        # anime absolute numbers
        final_result.ab_episode_numbers = self._combine_results(filename_result, dir_name_result, "ab_episode_numbers")

        # season and episode numbers
        final_result.season_number = self._combine_results(filename_result, dir_name_result, "season_number")
        final_result.episode_numbers = self._combine_results(filename_result, dir_name_result, "episode_numbers")
        final_result.scene_season = self._combine_results(filename_result, dir_name_result, "scene_season")

        # Ambiguity belongs to whichever result actually supplied the numbers above -- the same
        # precedence _combine_results uses (filename first, dirname only as a fallback). A batch
        # FOLDER is often ambiguous ("Date A Live IV-1-12") while a file inside it is perfectly
        # clear, and in that case the file's verdict is the one that counts.
        episode_source = filename_result or dir_name_result
        if episode_source:
            final_result.ambiguous = episode_source.ambiguous
            final_result.ambiguity_reason = episode_source.ambiguity_reason
            final_result.discarded_number = episode_source.discarded_number
            final_result.franchise_conflict = episode_source.franchise_conflict

        # if the dirname has a release group/show name I believe it over the filename
        final_result.series_name = self._combine_results(dir_name_result, filename_result, "series_name")
        final_result.extra_info = self._combine_results(dir_name_result, filename_result, "extra_info")
        final_result.release_group = self._combine_results(dir_name_result, filename_result, "release_group")
        final_result.version = self._combine_results(dir_name_result, filename_result, "version")

        final_result.which_regex = []
        if final_result == filename_result:
            final_result.which_regex = filename_result.which_regex
            final_result.score = filename_result.score
        elif final_result == dir_name_result:
            final_result.which_regex = dir_name_result.which_regex
            final_result.score = dir_name_result.score
        else:
            final_result.score = 0
            if filename_result:
                final_result.which_regex += filename_result.which_regex
                final_result.score += filename_result.score
            if dir_name_result:
                final_result.which_regex += dir_name_result.which_regex
                final_result.score += dir_name_result.score

        final_result.show = self._combine_results(filename_result, dir_name_result, "show")
        final_result.quality = self._combine_results(filename_result, dir_name_result, "quality")

        if not final_result.show:
            raise InvalidShowException(f"Unable to match {name} to a show in your database. Parser result: {final_result}")

        # if there's no useful info in it then raise an exception
        if (
            final_result.season_number is None
            and not final_result.episode_numbers
            and final_result.air_date is None
            and not final_result.ab_episode_numbers
            and not final_result.series_name
        ):
            raise InvalidNameException(f"Unable to parse {name} to a valid episode of {final_result.show.name}. Parser result: {final_result}")

        if cache_result:
            name_parser_cache.store(name, final_result, parse_generation)

        logger.debug(f"Parsed {name} into {final_result}")
        return final_result


class ParseResult(object):
    def __init__(
        self,
        original_name,
        series_name=None,
        season_number=None,
        episode_numbers=None,
        extra_info=None,
        release_group=None,
        air_date=None,
        ab_episode_numbers=None,
        show=None,
        score=0,
        quality=None,
        version=None,
    ):
        self.original_name = original_name

        self.series_name = series_name
        self.season_number = season_number
        if not episode_numbers:
            self.episode_numbers = []
        else:
            self.episode_numbers = episode_numbers

        if not ab_episode_numbers:
            self.ab_episode_numbers = []
        else:
            self.ab_episode_numbers = ab_episode_numbers

        if not quality:
            self.quality = common.Quality.UNKNOWN
        else:
            self.quality = quality

        self.extra_info = extra_info
        self.release_group = release_group

        self.air_date = air_date

        self.which_regex = []
        self.show: "TVShow" = show
        self.score = score

        self.version = version

        self.scene_season = None

        # Set when the name admits more than one credible episode reading and we refused to guess.
        # Consumers that WRITE to disk (post-processing) must not act on an ambiguous result.
        # See NameParser._check_discarded_episode_number.
        self.ambiguous = False
        self.ambiguity_reason = None
        # Ambiguous specifically because the name matches an alias family whose season tags
        # contradict each other. Search must NOT hand such a release to the AI matcher: no
        # deterministic check could tell a right AI answer from a wrong one, so the release stays
        # unmatched until the user pins the family with a custom scene exception.
        self.franchise_conflict = False
        # The episode number the regex threw away, when there was one ("Ajin 2 - 12" -> 12), and
        # whether its dash was spaced (the fansub season form) rather than a batch range hyphen.
        self.discarded_number = None
        self.discarded_number_spaced = False

    def __eq__(self, other):
        return other and all(
            [
                self.series_name == other.series_name,
                self.season_number == other.season_number,
                self.episode_numbers == other.episode_numbers,
                self.extra_info == other.extra_info,
                self.release_group == other.release_group,
                self.air_date == other.air_date,
                self.ab_episode_numbers == other.ab_episode_numbers,
                self.show == other.show,
                self.score == other.score,
                self.quality == other.quality,
                self.version == other.version,
            ]
        )

    def __str__(self):
        if self.series_name is not None:
            to_return = f"{self.series_name} - "
        else:
            to_return = ""
        if self.season_number is not None:
            to_return += f"S{self.season_number:02}"
        if self.episode_numbers:
            for e in self.episode_numbers:
                if e is not None:
                    to_return += f"E{e:02}"

        if self.is_air_by_date:
            to_return += f" {self.air_date}"
        if self.ab_episode_numbers:
            to_return += f" [ABS: {self.ab_episode_numbers}]"
        if self.version and self.is_anime is True:
            to_return += f" [ANIME VER: {self.version}]"

        if self.release_group:
            to_return += f" [GROUP: {self.release_group}]"

        to_return += f" [ABD: {self.is_air_by_date}] [ANIME: {self.is_anime}] [whichReg: {self.which_regex}] Score: {self.score}"

        return re.sub(r" +", " ", to_return)

    @property
    def is_air_by_date(self):
        return bool(self.air_date)

    @property
    def is_anime(self):
        return bool(self.ab_episode_numbers)


class NameParserCache(object):
    """Parsed-result cache, validated against the scene-exceptions generation on every read.

    A parse depends on alias data (show resolution, season pins, the franchise gate), so a result
    computed under one generation must not be served after a sync or custom edit changes that
    data. Entries carry the generation their parse STARTED under; a mismatched entry is simply
    not returned (a stale in-flight parse may still store its result -- harmlessly, because no
    reader will ever accept it).
    """

    def __init__(self):
        self.lock = Lock()
        self.data = OrderedDict()
        self.max_size = 200

    def __getitem__(self, name):
        current = scene_exceptions.exceptions_generation()
        with self.lock:
            entry = self.data.get(name)
            if not entry:
                return None
            generation, value = entry
            if generation != current:
                self.data.pop(name, None)
                return None
            logger.debug(f"Using cached parse result for: {name}")
            return value

    def __setitem__(self, key, value):
        self.store(key, value, scene_exceptions.exceptions_generation())

    def store(self, key, value, generation):
        with self.lock:
            self.data.update({key: (generation, value)})
            while len(self.data) > self.max_size:
                self.data.pop(list(self.data)[0], None)


name_parser_cache = NameParserCache()


class InvalidNameException(Exception):
    """The given release name is not valid"""


class InvalidShowException(Exception):
    """The given show name is not valid"""
