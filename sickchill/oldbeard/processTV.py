import logging
import os
import shutil
import stat
import threading
import time
import traceback
from pathlib import Path
from typing import TYPE_CHECKING

from rarfile import BadRarFile, Error, NeedFirstVolume, PasswordRequired, RarCRCError, RarExecError, RarFile, RarOpenError, RarWrongPassword

from sickchill import logger, settings
from sickchill.helper.common import is_anime_extra, is_media_file, is_rar_file, is_sync_file, is_torrent_or_nzb_file, remove_extension, valid_url
from sickchill.helper.exceptions import EpisodePostProcessingFailedException, FailedPostProcessingFailedException
from sickchill.oldbeard import common, db, failedProcessor, helpers, postProcessor

if TYPE_CHECKING:
    from sickchill.oldbeard.name_parser.parser import ParseResult


# Auto post-processing rescans the download folder on a short timer (every 2 minutes by default). A
# file that fails for a PERMANENT reason -- unparseable name, ambiguous release, no matching episode
# -- fails identically on every pass, re-running the parse, media probe, AI analysis and hashing each
# time. That is the "post-processing the same thing over and over" loop. Remember failures and back
# off exponentially. The fingerprint is (size, mtime) so a repaired or replaced file retries at once,
# and a restart clears the table, so a backoff can never permanently strand a file.
# Guarded by _failure_backoff_lock: the scheduled auto-processing task and a user-triggered
# force_next run can touch this concurrently, and the read/evict/update sequences are not atomic.
_failure_backoff = {}
_failure_backoff_lock = threading.Lock()
FAILURE_BACKOFF_BASE_SECONDS = 15 * 60
FAILURE_BACKOFF_MAX_SECONDS = 24 * 60 * 60
_FAILURE_BACKOFF_MAX_ENTRIES = 1000


def reap_blocker(process_method, mode, delete_on, video_files, failed_files, extra_files):
    """
    Say why a just-processed folder must NOT be deleted, or return None when reaping is safe.

    Reaping is an ``rmtree``, so every "keep it" reason here is a data-loss guard. Kept as a pure
    function of the folder's classification so all four folder states (episodes only / extras only /
    mixed / neither) can be asserted directly.

    :return: a human-readable reason to keep the folder, or ``None`` to allow deletion
    """
    if process_method != "move":
        # copy/hardlink/symlink deliberately leave the source in place.
        return "process method is not 'move'"
    if mode == "manual" and not delete_on:
        return "manual mode without delete requested"
    if not video_files:
        # Nothing was imported from here this pass -- including a folder holding only bonus content.
        return "no episodes were processed here"
    if failed_files:
        return f"{len(failed_files)} unprocessed video file(s) remain: {failed_files}"
    if extra_files:
        # Bonus content we declined to import is still the user's file, and a mis-classified episode
        # would be deleted along with it.
        return f"keeping {len(extra_files)} bonus file(s) we did not import: {extra_files}"
    return None


def _file_fingerprint(file_path):
    """Return a cheap (size, mtime) identity for a file, or None if it cannot be read."""
    try:
        stat_result = os.stat(file_path)
    except OSError:
        return None
    return stat_result.st_size, int(stat_result.st_mtime)


def _backoff_seconds(failures):
    """Exponential backoff: 15m, 30m, 1h, 2h ... capped at a day."""
    return min(FAILURE_BACKOFF_BASE_SECONDS * (2 ** (failures - 1)), FAILURE_BACKOFF_MAX_SECONDS)


def in_failure_backoff(file_path, force=False):
    """
    Check whether a previously-failed file should be skipped this pass.

    A file whose content changed since it failed is retried immediately, as is one the user forced.
    """
    if force:
        return False

    with _failure_backoff_lock:
        entry = _failure_backoff.get(file_path)
        if not entry:
            return False

        fingerprint, failures, last_attempt = entry
        if _file_fingerprint(file_path) != fingerprint:
            _failure_backoff.pop(file_path, None)
            return False

        return (time.time() - last_attempt) < _backoff_seconds(failures)


def record_processing_failure(file_path):
    """Remember that this file failed, lengthening its backoff each consecutive time."""
    fingerprint = _file_fingerprint(file_path)
    if not fingerprint:
        return

    with _failure_backoff_lock:
        entry = _failure_backoff.get(file_path)
        failures = entry[1] + 1 if entry and entry[0] == fingerprint else 1

        if len(_failure_backoff) >= _FAILURE_BACKOFF_MAX_ENTRIES:
            for stale_path in [path for path in _failure_backoff if not os.path.exists(path)]:
                _failure_backoff.pop(stale_path, None)
            if len(_failure_backoff) >= _FAILURE_BACKOFF_MAX_ENTRIES:
                _failure_backoff.pop(next(iter(_failure_backoff)), None)

        _failure_backoff[file_path] = (fingerprint, failures, time.time())


def record_processing_success(file_path):
    """Clear any remembered failure for a file that has now been processed."""
    with _failure_backoff_lock:
        _failure_backoff.pop(file_path, None)


class ProcessResult(object):
    def __init__(self):
        self.result = True
        self.output = ""
        self.missed_files = []
        self.aggresult = True


def delete_folder(folder, check_empty=True):
    """
    Removes a folder from the filesystem

    param folder: Path to folder to remove
    param check_empty: Boolean, check if the folder is empty before removing it, defaults to True
    return: True on success, False on failure
    """

    folder = Path(folder).resolve()
    # check if it's a folder
    if not folder.is_dir():
        return False

    # check if it isn't TV_DOWNLOAD_DIR
    if settings.TV_DOWNLOAD_DIR and str(Path(folder).resolve()) == str(Path(settings.TV_DOWNLOAD_DIR).resolve()):
        return False

    # check if it's empty folder when wanted to be checked
    if check_empty:
        found_files = [file for file in folder.iterdir()]
        if found_files:
            logger.info(f"Not deleting folder {folder} found the following files: {found_files}")
            return False

        try:
            logger.info(f"Deleting folder (if it's empty): {folder}")
            folder.rmdir()
        except (OSError, IOError) as error:
            logger.warning(f"Warning: unable to delete folder: {folder}: {error}")
            return False
    else:
        try:
            logger.info(f"Deleting folder: {folder}")
            shutil.rmtree(folder)
        except (OSError, IOError) as error:
            logger.warning(f"Warning: unable to delete folder: {folder}: {error}")
            return False

    return True


def delete_files(process_path, unwanted_files, result, force=False):
    """
    Remove files from filesystem

    param process_path: path to process
    param unwanted_files: files we do not want
    param result: Processor results
    param force: Boolean, force deletion, defaults to false
    """
    if not result.result and force:
        result.output += log_helper("Forcing deletion of files, even though last result was not success", logger.DEBUG)
    elif not result.result:
        return

    process_path = Path(process_path)
    # Delete all file not needed
    for current_file in unwanted_files:
        file_path = process_path / current_file
        if not file_path.is_file():
            continue  # Prevent error when a notwantedfiles is an associated files

        result.output += log_helper(f"Deleting file: {current_file}", logger.DEBUG)

        # check first the read-only attribute
        file_attribute = file_path.stat()[0]
        if not file_attribute & stat.S_IWRITE:
            # File is read-only, so make it writeable
            result.output += log_helper(f"Changing ReadOnly Flag for file: {current_file}", logger.DEBUG)
            try:
                file_path.chmod(stat.S_IWRITE)
            except OSError as error:
                result.output += log_helper(f"Cannot change permissions of {current_file}: {error}", logger.DEBUG)
        try:
            file_path.unlink(True)
        except OSError as error:
            result.output += log_helper(f"Unable to delete file {current_file}: {error}", logger.DEBUG)


def log_helper(message, level=logging.INFO):
    logger.log(level, message)
    return message + "\n"


def process_dir(process_path, release_name=None, process_method=None, force=False, is_priority=None, delete_on=False, failed=False, mode="auto"):
    """
    Scans through the files in process_path and processes whatever media files it finds

    param process_path: The folder name to look in
    param release_name: The NZB/Torrent name which resulted in this folder being downloaded
    param process_method: processing method, copy/move/symlink/link
    param force: True to process previously processed files
    param is_priority: whether to replace the file even if it exists at higher quality
    param delete_on: delete files and folders after they are processed (always happens with move and auto combination)
    param failed: Boolean for whether the download failed
    param mode: Type of postprocessing auto or manual
    """
    result = ProcessResult()
    try:
        # if they passed us a real dir then assume it's the one we want
        if os.path.isdir(process_path):
            process_path = os.path.realpath(process_path)
            result.output += log_helper(f"Processing in folder {process_path}", logger.DEBUG)

        # if the client and SickChill are not on the same machine translate the directory into a network directory
        elif all(
            [
                settings.TV_DOWNLOAD_DIR,
                Path(settings.TV_DOWNLOAD_DIR).is_dir(),
                str(Path(process_path).resolve()) == str(Path(settings.TV_DOWNLOAD_DIR).resolve()),
            ]
        ):
            process_path = os.path.join(settings.TV_DOWNLOAD_DIR, os.path.abspath(process_path).split(os.path.sep)[-1])
            result.output += log_helper(f"Trying to use folder: {process_path} ", logger.DEBUG)

        # if we didn't find a real dir then quit
        if not Path(process_path).is_dir():
            result.output += log_helper(
                "Unable to figure out what folder to process. "
                "If your downloader and SickChill aren't on the same PC "
                "make sure you fill out your TV download dir in the config.",
                logger.DEBUG,
            )
            return result.output

        process_method = process_method or settings.PROCESS_METHOD

        directories_from_rars = set()

        # If we have a release name (probably from nzbToMedia), and it is a rar/video, only process that file
        if release_name and valid_url(release_name) is True:
            result.output += log_helper(_("Processing {release_name}").format(release_name=release_name))
            generator_to_use = [("", [], [release_name])]
        elif release_name and (is_media_file(release_name) or is_rar_file(release_name)):
            result.output += log_helper(_("Processing {release_name}").format(release_name=release_name))
            generator_to_use = [(process_path, [], [release_name])]
        else:
            result.output += log_helper(_("Processing {process_path}").format(process_path=process_path))
            generator_to_use = os.walk(process_path, followlinks=settings.PROCESSOR_FOLLOW_SYMLINKS)

        rar_files = []

        for current_directory, directory_names, filenames in generator_to_use:
            result.result = True

            if current_directory:
                filenames = [f for f in filenames if not is_torrent_or_nzb_file(f)]
                rar_files = [x for x in filenames if is_rar_file(os.path.join(current_directory, x))]
                if rar_files:
                    extracted_directories = unrar(current_directory, rar_files, force, result)
                    if extracted_directories:
                        for extracted_directory in extracted_directories:
                            if extracted_directory.split(current_directory)[-1] not in directory_names:
                                result.output += log_helper(
                                    _("Adding extracted directory to the list of directories to process: {extracted_directory}").format(
                                        extracted_directory=extracted_directory
                                    ),
                                    logger.DEBUG,
                                )
                                directories_from_rars.add(extracted_directory)

            if not validate_dir(current_directory, release_name, failed, result):
                continue

            media_files = list(filter(is_media_file, filenames))

            # Creditless openings, commercials, promos and disc menus are not episodes. Left in the
            # list they parse against the batch folder's name and get filed as a whole season.
            extra_files = [filename for filename in media_files if is_anime_extra(filename)]
            video_files = [filename for filename in media_files if filename not in extra_files]
            # One line per folder, at DEBUG: auto-processing re-walks this folder every couple of
            # minutes for as long as the extras stay there, and per-file INFO would just replace the
            # old post-processing loop with a logging loop.
            failed_files = []
            if video_files:
                if extra_files:
                    result.output += log_helper(f"{current_directory}: skipping {len(extra_files)} bonus file(s), not episodes: {extra_files}", logger.DEBUG)
                failed_files = process_media(current_directory, video_files, release_name, process_method, force, is_priority, result)
            elif extra_files:
                # Nothing here but bonus content. Nothing was imported and nothing failed, so do not
                # report a failure, and do not reap: reaping would delete the extras the user kept.
                result.output += log_helper(f"{current_directory}: only bonus content ({len(extra_files)} file(s)), nothing to process", logger.DEBUG)
                continue
            else:
                result.result = False

            # Season-aware reaping: never delete a folder while it still holds a video we did not
            # capture -- a failed parse/match, an AI-match cooldown, or bonus content we declined to
            # import. Files that were moved, matched an existing destination, or were already
            # processed are "handled". See reap_blocker for the full decision.
            blocker = reap_blocker(process_method, mode, delete_on, video_files, failed_files, extra_files)
            if blocker:
                result.output += log_helper(f"Not reaping {current_directory}: {blocker}", logger.DEBUG)
                continue

            # The Synology metadata subfolder never holds wanted media: remove it and drop it
            # from the walk list so os.walk does not try to descend into a now-deleted dir.
            if "@eaDir" in directory_names:
                delete_folder(os.path.join(current_directory, "@eaDir"), False)
                directory_names[:] = [name for name in directory_names if name != "@eaDir"]

            # Only reap a LEAF release folder. os.walk is top-down, so any remaining child dirs
            # have NOT been processed yet; rmtree-ing the parent now could delete uncaptured
            # nested media. Leave such folders for their own pass (the parent is retained).
            if directory_names:
                result.output += log_helper(
                    f"Not reaping {current_directory}: unprocessed subdirectories remain: {directory_names}", logger.DEBUG
                )
                continue

            # Leaf folder, all videos handled -> remove it and any leftover junk (samples, nfo,
            # stray associated files, consumed duplicate sources, etc.).
            if delete_folder(current_directory, check_empty=False):
                result.output += log_helper(_("Deleted folder: {current_directory}").format(current_directory=current_directory), logger.DEBUG)

        # For processing extracted rars, only allow methods 'move' and 'copy'.
        # On different methods fall back to 'move'.
        method_fallback = ("move", process_method)[process_method in ("move", "copy")]

        for directory_from_rar in directories_from_rars:
            process_dir(
                process_path=directory_from_rar,
                release_name=os.path.basename(directory_from_rar),
                process_method=method_fallback,
                force=force,
                is_priority=is_priority,
                delete_on=settings.DELRARCONTENTS or delete_on or method_fallback == "move",
                failed=failed,
                mode=mode,
            )

            # Delete rar file only if the extracted dir was successfully processed
            if mode == "auto" and method_fallback == "move" or mode == "manual" and delete_on:
                this_rar = [rar_file for rar_file in rar_files if Path(directory_from_rar).name == Path(rar_file).stem]
                delete_files(current_directory, this_rar, result)  # Deletes only if result.result == True

            delete_folder(directory_from_rar, settings.DELRARCONTENTS)

        result.output += log_helper((_("Processing Failed"), _("Successfully processed"))[result.aggresult], (logger.WARNING, logger.INFO)[result.aggresult])
        if result.missed_files:
            result.output += log_helper(_("Some items were not processed."))
            for missed_file in result.missed_files:
                result.output += log_helper(missed_file)

        return result.output
    except Exception:
        logger.debug(traceback.format_exc())
        return result.output


def validate_dir(process_path, release_name, failed, result):
    """
    Check if directory is valid for processing

    param process_path: Directory to check
    param release_name: Original NZB/Torrent name
    param failed: Previously failed objects
    param result: Previous results
    returns True if dir is valid for processing, False if not
    """

    result.output += log_helper("Processing folder " + process_path, logger.DEBUG)
    upper_name = os.path.basename(process_path).upper()
    if upper_name.startswith("_FAILED_") or upper_name.endswith("_FAILED_") or (os.sep + "_FAILED_") in upper_name or ("_FAILED_" + os.sep) in upper_name:
        result.output += log_helper(_("The directory name indicates it failed to extract."), logger.DEBUG)
        failed = True
    elif (
        upper_name.startswith("_UNDERSIZED_")
        or upper_name.endswith("_UNDERSIZED_")
        or (os.sep + "_UNDERSIZED_") in upper_name
        or ("_UNDERSIZED_" + os.sep) in upper_name
    ):
        result.output += log_helper(_("The directory name indicates that it was previously rejected for being undersized."), logger.DEBUG)
        failed = True
    elif upper_name.startswith("_UNPACK") or upper_name.endswith("_UNPACK") or (os.sep + "_UNPACK") in upper_name or ("_UNPACK" + os.sep) in upper_name:
        result.output += log_helper(_("The directory name indicates that this release is in the process of being unpacked."), logger.DEBUG)
        result.missed_files.append(f"{process_path} : Being unpacked")
        return False

    if failed:
        process_failed(process_path, release_name, result)
        result.missed_files.append(f"{process_path} : Failed download")
        return False

    if (
        settings.TV_DOWNLOAD_DIR
        and str(Path(process_path).resolve()) != str(Path(settings.TV_DOWNLOAD_DIR).resolve())
        and helpers.is_hidden_folder(process_path)
    ):
        result.output += log_helper(f"Ignoring hidden folder: {process_path}", logger.DEBUG)
        if not process_path.endswith("@eaDir"):
            result.missed_files.append(f"{process_path} : Hidden folder")
        return False

    # make sure the dir isn't inside a show dir
    main_db_con = db.DBConnection()
    sql_results = main_db_con.select("SELECT location FROM tv_shows")

    for sqlShow in sql_results:
        if (
            process_path.lower().startswith(os.path.realpath(sqlShow["location"]).lower() + os.sep)
            or process_path.lower() == os.path.realpath(sqlShow["location"]).lower()
        ):
            result.output += log_helper("Cannot process an episode that's already been moved to its show dir, skipping " + process_path, logger.WARNING)
            return False

    media_seen = False
    for current_directory, directory_names, filenames in os.walk(process_path, topdown=False, followlinks=settings.PROCESSOR_FOLLOW_SYMLINKS):
        sync_files = list(filter(is_sync_file, filenames))
        if sync_files and settings.POSTPONE_IF_SYNC_FILES:
            result.output += log_helper(f"Found temporary sync files: {sync_files} in path: {os.path.join(process_path, sync_files[0])}")
            result.output += log_helper(f"Skipping post processing for folder: {process_path}")
            result.missed_files.append(f"{os.path.join(process_path, sync_files[0])} : Sync files found")
            continue

        found_files = list(filter(is_media_file, filenames))
        if settings.UNPACK == settings.UNPACK_PROCESS_CONTENTS:
            found_files += list(filter(is_rar_file, filenames))

        if found_files:
            media_seen = True

        for found_file in found_files:
            if current_directory != settings.TV_DOWNLOAD_DIR and found_files:
                # pass 'current directory/filename' as one string to NameParser
                found_file = f"{os.path.basename(current_directory)}/{found_file}"

            if postProcessor.guessit_findit(found_file):
                return True

    # Rule-based parsing identified nothing. If AI post-process matching is enabled, still
    # allow the folder through so the AI fallback in PostProcessor can attempt to identify
    # the file(s) (e.g. anime releases whose names don't parse to the SC show, like
    # "[Moozzi2] Working S3-09 ..." -> Wagnaria!!). The matcher is throttled per-file
    # (cooldown + budget + response cache), so this does not spam the AI provider on
    # repeated scheduler passes.
    if media_seen and settings.AI_ENABLED and settings.AI_POSTPROCESS_MATCH_ENABLED:
        result.output += log_helper(f"{process_path} : no rule-based match; deferring to AI post-process matcher", logger.DEBUG)
        return True

    result.output += log_helper(f"{process_path} : No processable items found in folder", logger.DEBUG)
    return False


def unrar(path, rar_files, force, result):
    """
    Extracts RAR files

    param path: Path to look for files in
    param rar_files: Names of RAR files
    param force: process currently processing items
    param result: Previous results
    returns List of unpacked file names
    """

    unpacked_dirs = []

    if settings.UNPACK == settings.UNPACK_PROCESS_CONTENTS and rar_files:
        result.output += log_helper(f"Packed Releases detected: {rar_files}", logger.DEBUG)
        for archive in rar_files:
            failure = None
            rar_handle = None
            try:
                archive_path = os.path.join(path, archive)
                if already_processed(path, archive, force, result):
                    result.output += log_helper(f"Archive file already post-processed, extraction skipped: {archive_path}", logger.DEBUG)
                    continue

                if not is_rar_file(archive_path):
                    continue

                result.output += log_helper(f"Checking if archive is valid and contains a video: {archive_path}", logger.DEBUG)
                rar_handle = RarFile(archive_path)
                if rar_handle.needs_password():
                    # TODO: Add support in settings for a list of passwords to try here with rar_handle.set_password(x)
                    result.output += log_helper(f"Archive needs a password, skipping: {archive_path}")
                    continue

                rar_handle.testrar()

                # If there are no video files in the rar, don't extract it
                rar_media_files = list(filter(is_media_file, rar_handle.namelist()))
                if not rar_media_files:
                    continue

                rar_release_name = Path(archive).stem

                # Choose the directory we'll unpack to:
                if settings.UNPACK_DIR and os.path.isdir(settings.UNPACK_DIR):  # verify that the unpacked dir exists
                    unpack_base_dir = settings.UNPACK_DIR
                else:
                    unpack_base_dir = path
                    if settings.UNPACK_DIR:  # Let user know if we can't unpack there
                        result.output += log_helper(f"Unpack directory cannot be verified. Using {path}", logger.DEBUG)

                # Fix up the list for checking if already processed
                rar_media_files = [os.path.join(unpack_base_dir, rar_release_name, rar_media_file) for rar_media_file in rar_media_files]

                skip_rar = False
                for rar_media_file in rar_media_files:
                    check_path, check_file = os.path.split(rar_media_file)
                    if already_processed(check_path, check_file, force, result):
                        result.output += log_helper(f"Archive file already post-processed, extraction skipped: {rar_media_file}", logger.DEBUG)
                        skip_rar = True
                        break

                if skip_rar:
                    continue

                rar_extract_path = os.path.join(unpack_base_dir, rar_release_name)
                result.output += log_helper(f"Unpacking archive: {archive}", logger.DEBUG)
                rar_handle.extractall(path=rar_extract_path)
                unpacked_dirs.append(rar_extract_path)

            except RarCRCError:
                failure = ("Archive Broken", "Unpacking failed because of a CRC error")
            except RarWrongPassword:
                failure = ("Incorrect RAR Password", "Unpacking failed because of an Incorrect Rar Password")
            except PasswordRequired:
                failure = ("Rar is password protected", "Unpacking failed because it needs a password")
            except RarOpenError:
                failure = (
                    "Rar Open Error, check the parent folder and destination file permissions.",
                    "Unpacking failed with a File Open Error (file permissions?)",
                )
            except RarExecError:
                failure = ("Invalid Rar Archive Usage", "Unpacking Failed with Invalid Rar Archive Usage. Is unrar installed and on the system PATH?")
            except BadRarFile:
                failure = ("Invalid Rar Archive", "Unpacking Failed with an Invalid Rar Archive Error")
            except NeedFirstVolume:
                continue
            except (Exception, Error) as error:
                failure = (error, "Unpacking failed")
            finally:
                if rar_handle:
                    del rar_handle

            if failure:
                result.output += log_helper(f"Failed to extract the archive {archive}: {failure[0]}", logger.WARNING)
                result.missed_files.append(f"{archive} : Unpacking failed: {failure[1]}")
                result.result = False
                continue

    return unpacked_dirs


def _episode_scope(parse_result):
    """Restrict ``tv_episodes`` to the episode(s) a file maps to.

    Returns ``(clause, params, count_column, expected)`` using ``tv_episodes.``-qualified columns (safe
    when ``history`` is also joined), or ``None`` when we cannot tie the file to a show plus a concrete
    episode/absolute number. ``expected`` is the number of distinct episodes/absolute numbers the file
    maps to: **all** of them must be satisfied for ``already_processed`` to short-circuit, so a multi-
    episode file is never skipped just because one of its episodes is already present. Callers MUST treat
    ``None`` as "scope unknown" and never fall back to a global match.
    """
    show = getattr(parse_result, "show", None) if parse_result else None
    if not show or not show.indexerid:
        return None

    # An ambiguous name ("Ajin 2 - 12" could be absolute episode 2 or season 2 episode 12) has no
    # trustworthy scope. Scoping it anyway lets already_processed() report the file as handled, which
    # would drop it from failed_files and let the folder -- and the file -- be reaped.
    if getattr(parse_result, "ambiguous", False):
        return None

    if parse_result.season_number is not None and parse_result.episode_numbers:
        episodes = sorted(set(parse_result.episode_numbers))
        placeholders = ", ".join(["?"] * len(episodes))
        clause = f"tv_episodes.showid = ? AND tv_episodes.season = ? AND tv_episodes.episode IN ({placeholders})"
        return clause, [show.indexerid, parse_result.season_number, *episodes], "tv_episodes.episode", len(episodes)

    if parse_result.ab_episode_numbers:
        absolutes = sorted(set(parse_result.ab_episode_numbers))
        placeholders = ", ".join(["?"] * len(absolutes))
        clause = f"tv_episodes.showid = ? AND tv_episodes.absolute_number IN ({placeholders})"
        return clause, [show.indexerid, *absolutes], "tv_episodes.absolute_number", len(absolutes)

    return None


def already_processed(process_path, video_file, force, result):
    """
    Check if we already post processed a file.

    Only treats a file as already-processed when **every** episode it maps to is already
    Downloaded/Archived under this release name (durable ``tv_episodes.release_name`` guard) or recorded
    in the download ``history``. A release name that happens to sit on some *other* (or not-yet-downloaded)
    episode must never short-circuit processing: doing so previously let a stray/duplicated release name
    cause a real download to be skipped and its folder reaped, leaving the target episode stuck Snatched.

    param process_path: Directory a file resides in
    param video_file: File name
    param force: Force re-processing (skip these checks)
    param result: ProcessResult to log into
    :return: True if the file is already post processed, False otherwise
    """
    if force:
        return False

    # Parse the file itself (not just the folder) so the scope reflects this exact episode; folder-only
    # parses are under-scoped for season packs and generic parent directories.
    parse_result: "ParseResult" = postProcessor.guessit_findit(os.path.join(process_path, video_file))
    scope = _episode_scope(parse_result)
    if scope is None:
        # Can't tie this file to a concrete episode -> never skip on a global release-name match.
        return False
    scope_clause, scope_params, count_column, expected = scope

    downloaded_or_archived = common.Quality.DOWNLOADED + common.Quality.ARCHIVED
    status_placeholders = ", ".join(["?"] * len(downloaded_or_archived))
    main_db_con = db.DBConnection()

    # Durable guard (independent of the trimmable history table): all mapped episodes are already
    # downloaded/archived and carry this exact release name (folder path or file basename).
    release_sql = (
        f"SELECT COUNT(DISTINCT {count_column}) FROM tv_episodes "
        f"WHERE release_name IN (?, ?) AND release_name != '' AND {scope_clause} AND status IN ({status_placeholders})"
    )
    rows = main_db_con.select(release_sql, [process_path, remove_extension(video_file), *scope_params, *downloaded_or_archived])
    if rows and rows[0][0] >= expected:
        result.output += log_helper("You're trying to post process a dir that's already been processed, skipping", logger.DEBUG)
        return True

    # History-backed guard (handles the same episode re-downloaded @ different quality): all mapped
    # episodes are downloaded/archived and history records this resource for them.
    history_sql = (
        f"SELECT COUNT(DISTINCT {count_column}) FROM tv_episodes "
        "INNER JOIN history ON history.showid = tv_episodes.showid "
        "AND history.season = tv_episodes.season AND history.episode = tv_episodes.episode "
        f"WHERE {scope_clause} AND tv_episodes.status IN ({status_placeholders}) AND history.resource LIKE ?"
    )
    rows = main_db_con.select(history_sql, [*scope_params, *downloaded_or_archived, "%" + video_file])
    if rows and rows[0][0] >= expected:
        result.output += log_helper("You're trying to post process a video that's already been processed, skipping", logger.DEBUG)
        return True

    return False


def process_media(process_path, video_files, release_name, process_method, force, is_priority, result):
    """
    Postprocess mediafiles

    param process_path: Path to process in
    param video_files: Filenames to look for and postprocess
    param release_name: Name of NZB/Torrent file related
    param process_method: auto/manual
    param force: Postprocess currently postprocessing file
    param is_priority: Boolean, is this a priority download
    param result: Previous results
    """

    processor = None
    failed_files = []
    for cur_video_file in video_files:
        cur_video_file_path = os.path.join(process_path, cur_video_file)

        if already_processed(process_path, cur_video_file, force, result):
            result.output += log_helper(f"Skipping already processed file: {cur_video_file}", logger.DEBUG)
            continue

        if in_failure_backoff(cur_video_file_path, force):
            result.output += log_helper(f"Skipping {cur_video_file}: post-processing failed before, waiting before the next attempt", logger.DEBUG)
            # Still an uncaptured video, so the folder must not be reaped while we wait.
            failed_files.append(cur_video_file)
            continue

        try:
            processor = postProcessor.PostProcessor(cur_video_file_path, release_name, process_method, is_priority)
            result.result = processor.process()
            process_fail_message = ""
        except EpisodePostProcessingFailedException as error:
            result.result = False
            process_fail_message = error

        if processor:
            result.output += processor.log

        if result.result:
            record_processing_success(cur_video_file_path)
            result.output += log_helper(f"Processing succeeded for {cur_video_file_path}")
        else:
            record_processing_failure(cur_video_file_path)
            result.output += log_helper(f"Processing failed for {cur_video_file_path}: {process_fail_message}", logger.WARNING)
            result.missed_files.append(f"{cur_video_file_path} : Processing failed: {process_fail_message}")
            result.aggresult = False
            failed_files.append(cur_video_file)

    return failed_files


def process_failed(process_path, release_name, result):
    """Process a download that did not complete correctly"""

    if not settings.USE_FAILED_DOWNLOADS:
        return

    processor = None

    try:
        processor = failedProcessor.FailedProcessor(process_path, release_name)
        result.result = processor.process()
        process_fail_message = ""
    except FailedPostProcessingFailedException as error:
        result.result = False
        process_fail_message = error

    if processor:
        result.output += processor.log

    if settings.DELETE_FAILED and result.result:
        if delete_folder(process_path, check_empty=False):
            result.output += log_helper(f"Deleted folder: {process_path}", logger.DEBUG)

    if result.result:
        result.output += log_helper(f"Failed Download Processing succeeded: ({release_name}, {process_path})")
    else:
        result.output += log_helper(f"Failed Download Processing failed: ({release_name}, {process_path}): {process_fail_message}", logger.WARNING)
