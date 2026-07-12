import logging
import re
from pathlib import Path

from sickchill import logger, settings
from sickchill.helper.common import valid_url
from sickchill.helper.exceptions import FailedPostProcessingFailedException
from sickchill.oldbeard import search_queue, show_name_helpers
from sickchill.oldbeard.db import DBConnection
from sickchill.oldbeard.name_parser.parser import InvalidNameException, InvalidShowException, NameParser

# The folder markers validate_dir routes to failed processing. _UNPACK is deliberately absent: those
# folders mean "still unpacking" and never reach this module.
_FAILURE_MARKERS = re.compile(r"^(?:_FAILED_|_UNDERSIZED_)+|(?:_FAILED_|_UNDERSIZED_)+$", re.IGNORECASE)


def strip_failure_markers(name):
    """Remove the download client's failure markers from the edges of a folder-derived release name.

    SAB names the litter it leaves in the completed dir `_FAILED_<jobname>`; parsed as-is, the marker
    becomes part of the series name ("FAILED Show ...") and the lookup fails -- the failure marker
    itself breaks failed processing. Interior occurrences are left alone: they are part of the name.
    """
    return _FAILURE_MARKERS.sub("", name)


class FailedProcessor(object):
    """Take appropriate action when a download fails to complete"""

    def __init__(self, directory, release_name):
        """
        :param directory: Full path to the folder of the failed download
        :param release_name: Full name of the release file that failed
        """
        self.directory = directory
        self.release_name = release_name

        self.log = ""
        # True when at least one episode's failure was left to the download status checker. The folder
        # must then be kept: it is the only filesystem evidence should the checker never resolve it.
        self.deferred = False

    def process(self):
        """
        Do the actual work

        :return: True
        """
        self._log(_("Failed download detected: ({release_name}, {directory})").format(release_name=self.release_name, directory=self.directory))

        if self.release_name and valid_url(self.release_name) is True:
            cache_db_con = DBConnection("cache.db")
            cache_result = cache_db_con.select_one("SELECT name FROM results WHERE url = ?", [self.release_name])
            if cache_result:
                self.release_name = cache_result["name"]

        release_name = show_name_helpers.determine_release_name(self.directory, self.release_name)
        if not release_name:
            self._log(_("Warning: unable to find a valid release name."), logger.WARNING)
            raise FailedPostProcessingFailedException()

        # Strip client failure markers, but only off a name that is the marker folder's own basename:
        # explicit downloader-supplied names and nzb/nfo-derived stems are never altered.
        if not self.release_name and self.directory and release_name == Path(self.directory).name:
            stripped = strip_failure_markers(release_name)
            if stripped and stripped != release_name:
                self._log(_("Stripped the failure marker off the folder name: {stripped}").format(stripped=stripped), logger.DEBUG)
                release_name = stripped

        try:
            parsed = NameParser(False).parse(release_name)
        except (InvalidNameException, InvalidShowException) as error:
            self._log(f"{error}", logger.DEBUG)
            raise FailedPostProcessingFailedException()

        self._log("name_parser info: ", logger.DEBUG)
        self._log(f"{parsed.series_name}", logger.DEBUG)
        self._log(f"{parsed.season_number}", logger.DEBUG)
        self._log(f"{parsed.episode_numbers}", logger.DEBUG)
        self._log(f"{parsed.extra_info}", logger.DEBUG)
        self._log(f"{parsed.release_group}", logger.DEBUG)
        self._log(f"{parsed.air_date}", logger.DEBUG)

        main_db_con = DBConnection()
        for episode in parsed.episode_numbers:
            segment = parsed.show.get_episode(parsed.season_number, episode)

            # The download status checker owns every download it is tracking: whatever this folder
            # names, a pending row means either the same job (the checker will classify it within a
            # poll) or a newer one (enqueueing would fail a working download). Rows from a client we
            # no longer talk to cannot be resolved by the checker, so those do not defer.
            rows = main_db_con.select(
                "SELECT client FROM pending_downloads WHERE showid = ? AND season = ? AND episode = ?",
                [parsed.show.indexerid, segment.season, segment.episode],
            )
            if any(row["client"] == settings.NZB_METHOD for row in rows):
                self._log(
                    _("Deferring {episode} to the download status checker, which is tracking its download").format(episode=segment.pretty_name),
                )
                self.deferred = True
                continue

            # anonymous: this failure was inferred from a folder, so mark_failed must decline if any
            # newer snatch has claimed the episode. The release is the folder's own, so the block
            # lands on the release that actually failed instead of a guess from history.
            cur_failed_queue_item = search_queue.FailedQueueItem(
                parsed.show,
                [segment],
                failed_releases={(segment.season, segment.episode): {"release": release_name, "anonymous": True}},
            )
            settings.searchQueueScheduler.action.add_item(cur_failed_queue_item)

        return not self.deferred

    def _log(self, message, level=logging.INFO):
        """Log to regular logfile and save for return for PP script log"""
        logger.log(level, message)
        self.log += message + "\n"
