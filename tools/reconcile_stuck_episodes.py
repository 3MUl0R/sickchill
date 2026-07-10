#!/usr/bin/env python3
"""Reconcile episodes stranded in a snatched status, once.

SickChill only learned to notice failed downloads recently. Episodes snatched before that have no
pending_downloads row, so nothing will ever reconcile them: an episode at SNATCHED_BEST is invisible to
searchBacklog._get_segments, and one at SNATCHED whose quality is already preferred is skipped there too.
They sit forever.

This walks them once and puts each back into a state the rest of the system understands:

    a real file on disk   -> DOWNLOADED at the file's own quality, exactly as a rescan would set it
    no file               -> WANTED, so the searchers can see it again
    a file that disagrees
    with the database     -> nothing. Reported for a human.

A file "is" the file only when its byte size matches what the database recorded. A same-named file of a
different size is a different file, and has been, on this install, in ways that cost a season of anime.

Dry run by default: it opens the database read-only and writes nothing. Read the report before --apply.

    docker cp tools/reconcile_stuck_episodes.py sickChill:/tmp/
    docker exec sickChill /lsiopy/bin/python3 /tmp/reconcile_stuck_episodes.py --datadir /config
    docker exec sickChill /lsiopy/bin/python3 /tmp/reconcile_stuck_episodes.py --datadir /config --apply
"""

import argparse
import csv
import os
import sqlite3
import sys
import time

# Composite status = base + 100 * quality (Quality.compositeStatus).
SNATCHED = 2
WANTED = 3
DOWNLOADED = 4
ARCHIVED = 6
SNATCHED_PROPER = 9
SNATCHED_BEST = 12

STRANDED = (SNATCHED, SNATCHED_PROPER, SNATCHED_BEST)

RESTORE = "restore"
REVERT = "revert"
REVIEW = "review"
SKIP_TRACKED = "skip-tracked"
SKIP_RECENT = "skip-recent"


def base_status(composite):
    return composite % 100


def split_quality(composite):
    return composite // 100


def load_quality():
    """Imported late: it pulls in mediainfo bindings, and only the on-disk branch needs them."""
    from sickchill.oldbeard.common import Quality

    return Quality


def sickchill_is_running():
    """True when a SickChill process is alive in this namespace.

    A running SickChill holds TVEpisode objects in memory, and its in-memory status is authoritative --
    it will happily write a cached SNATCHED_BEST back over whatever this script wrote.
    """
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit() or entry == str(os.getpid()):
                continue
            try:
                with open(f"/proc/{entry}/cmdline", "rb") as handle:
                    cmdline = handle.read().decode("utf-8", "replace")
            except OSError:
                continue
            if "SickChill" in cmdline and "reconcile_stuck_episodes" not in cmdline:
                return True
    except OSError:
        pass
    return False


def has_table(connection, name):
    return bool(connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone())


def tracked_episodes(connection):
    """Episodes the new reconciler already owns. It, not us, decides their fate."""
    if not has_table(connection, "pending_downloads"):
        return set()
    return {(row[0], row[1], row[2]) for row in connection.execute("SELECT showid, season, episode FROM pending_downloads")}


def recent_snatches(connection, min_age_days):
    """Episodes snatched recently enough that the download may still be running."""
    cutoff = time.strftime("%Y%m%d%H%M%S", time.localtime(time.time() - min_age_days * 86400))
    rows = connection.execute(
        "SELECT showid, season, episode FROM history WHERE action % 100 IN (?, ?, ?) GROUP BY showid, season, episode HAVING MAX(date) > ?",
        (SNATCHED, SNATCHED_PROPER, SNATCHED_BEST, int(cutoff)),
    )
    return {(row[0], row[1], row[2]) for row in rows}


def classify(episode, quality, on_disk_status):
    """What should happen to this episode, and why."""
    location = episode["location"] or ""

    if not location:
        return REVERT, WANTED, "no file on disk"

    # Mirror refresh_dir's own test (tv.py:1164) exactly: a location it considers outside the show
    # directory gets cleared and re-statused on the next refresh, so restoring it would not stick.
    show_location = episode["show_location"] or ""
    if not show_location:
        return REVIEW, None, "the show has no location; refresh_dir would reject this path"
    if not os.path.normpath(location).startswith(os.path.normpath(show_location)):
        return REVIEW, None, f"file is outside the show directory ({show_location}); refresh_dir would clear it"

    try:
        actual_size = os.path.getsize(location)
    except OSError as error:
        return REVIEW, None, f"location set but unreadable: {error.__class__.__name__}"

    recorded_size = episode["file_size"] or 0
    if recorded_size <= 0:
        return REVIEW, None, f"file exists ({actual_size} bytes) but the database recorded no size"
    if actual_size != recorded_size:
        return REVIEW, None, f"size mismatch: disk {actual_size} != database {recorded_size}"

    try:
        restored = quality.statusFromName(location, anime=bool(episode["anime"]))
    except Exception as error:  # libmediainfo has segfaulted on this install before; one bad file must not abort the run
        return REVIEW, None, f"could not read quality: {error.__class__.__name__}: {error}"

    if on_disk_status == "archived":
        restored = quality.compositeStatus(ARCHIVED, split_quality(restored))

    return RESTORE, restored, f"file verified at {actual_size} bytes"


def orphan_count(connection):
    """Stranded rows whose show no longer exists. Left alone, but never silently."""
    return connection.execute(
        """
        SELECT COUNT(*) FROM tv_episodes e LEFT JOIN tv_shows s ON s.indexer_id = e.showid
        WHERE s.indexer_id IS NULL AND e.status % 100 IN (?, ?, ?)
        """,
        STRANDED,
    ).fetchone()[0]


def gather(connection, args):
    quality = load_quality()
    tracked = tracked_episodes(connection)
    recent = recent_snatches(connection, args.min_age_days) if has_table(connection, "history") else set()

    rows = connection.execute(
        """
        SELECT e.showid, e.season, e.episode, e.status, e.location, e.file_size,
               s.show_name, s.anime, s.paused, s.location AS show_location, s.quality AS show_quality
        FROM tv_episodes e JOIN tv_shows s ON s.indexer_id = e.showid
        WHERE e.status % 100 IN (?, ?, ?)
        ORDER BY s.show_name, e.season, e.episode
        """,
        STRANDED,
    ).fetchall()

    findings = []
    for episode in rows:
        key = (episode["showid"], episode["season"], episode["episode"])

        if key in tracked:
            action, target, reason = SKIP_TRACKED, None, "the download-status reconciler owns this episode"
        elif key in recent:
            action, target, reason = SKIP_RECENT, None, f"snatched within {args.min_age_days} day(s); may still be downloading"
        else:
            action, target, reason = classify(episode, quality, args.on_disk_status)

        findings.append(
            {
                "show": episode["show_name"],
                "showid": episode["showid"],
                "season": episode["season"],
                "episode": episode["episode"],
                "current_status": episode["status"],
                "current_base": base_status(episode["status"]),
                "action": action,
                "target_status": target,
                "reason": reason,
                "location": episode["location"] or "",
                "anime": episode["anime"],
                "show_quality": episode["show_quality"],
            }
        )

    return findings


def upgrade_split(findings):
    """After a restore, which episodes does searchBacklog._get_segments treat as upgrade targets?

    Mirrors its quality skip exactly: with preferred qualities set, skip when the current quality is
    preferred; with none set, skip when it is allowed. (BACKLOG_MISSING_ONLY would skip all DOWNLOADED;
    it is off on the target box, so this reports the on-by-default behaviour.)
    """
    searched, left_alone = {}, {}
    for finding in findings:
        if finding["action"] != RESTORE:
            continue
        if base_status(finding["target_status"]) != DOWNLOADED:
            bucket = left_alone  # ARCHIVED never enters _get_segments
        else:
            episode_quality = split_quality(finding["target_status"])
            allowed = finding["show_quality"] & 0xFFFF
            preferred = finding["show_quality"] >> 16
            skip = (episode_quality & preferred) if preferred else (episode_quality & allowed)
            bucket = left_alone if skip else searched
        bucket[finding["show"]] = bucket.get(finding["show"], 0) + 1
    return searched, left_alone


def report(findings, quality, csv_path, orphans):
    counts = {}
    for finding in findings:
        counts[finding["action"]] = counts.get(finding["action"], 0) + 1

    print(f"\n{len(findings)} episodes stranded in a snatched status\n")
    for action in (RESTORE, REVERT, REVIEW, SKIP_TRACKED, SKIP_RECENT):
        if counts.get(action):
            print(f"  {action:<14} {counts[action]:>5}")
    if orphans:
        print(f"\n{orphans} stranded rows belong to shows no longer in tv_shows. Left alone.")

    specials = {}
    for finding in findings:
        if finding["season"] == 0 and finding["action"] in (RESTORE, REVERT):
            specials[finding["action"]] = specials.get(finding["action"], 0) + 1
    if specials:
        detail = ", ".join(f"{count} {action}" for action, count in sorted(specials.items()))
        print(f"\nSeason-0 specials touched: {detail}. Backlog searches specials; daily search never does.")

    restores = [f for f in findings if f["action"] == RESTORE]
    if restores:
        print("\nRestored to a downloaded status, by inferred quality:")
        by_quality = {}
        for finding in restores:
            name = quality.qualityStrings[split_quality(finding["target_status"])]
            by_quality[name] = by_quality.get(name, 0) + 1
        for name, count in sorted(by_quality.items(), key=lambda item: -item[1]):
            print(f"  {name:<20} {count:>5}")

        by_location = {}
        for finding in restores:
            by_location.setdefault(finding["location"], []).append(finding)
        shared = {loc: group for loc, group in by_location.items() if len(group) > 1}
        if shared:
            print(f"\n{len(shared)} files are shared by multiple restored episodes (multi-episode files):")
            for location, group in sorted(shared.items())[:10]:
                episodes = ", ".join(f"S{f['season']:02d}E{f['episode']:02d}" for f in group)
                print(f"  {group[0]['show']} {episodes}")

        searched, left_alone = upgrade_split(findings)
        print(f"\nAfter restore, backlog would try to UPGRADE {sum(searched.values())} of these:")
        for show, count in sorted(searched.items(), key=lambda item: -item[1])[:10]:
            print(f"  {show:<35} {count:>5}")
        print(f"and leave {sum(left_alone.values())} alone (already at a preferred quality, or archived):")
        for show, count in sorted(left_alone.items(), key=lambda item: -item[1])[:10]:
            print(f"  {show:<35} {count:>5}")

    reviews = [f for f in findings if f["action"] == REVIEW]
    if reviews:
        print(f"\n{len(reviews)} need a human. The database and the disk disagree:")
        for finding in reviews[:20]:
            print(f"  {finding['show']} S{finding['season']:02d}E{finding['episode']:02d}: {finding['reason']}")
        if len(reviews) > 20:
            print(f"  ... and {len(reviews) - 20} more (see the CSV)")

    print("\nBy show:")
    by_show = {}
    for finding in findings:
        entry = by_show.setdefault(finding["show"], {})
        entry[finding["action"]] = entry.get(finding["action"], 0) + 1
    for show, actions in sorted(by_show.items(), key=lambda item: -sum(item[1].values()))[:15]:
        summary = ", ".join(f"{count} {action}" for action, count in sorted(actions.items()))
        print(f"  {show:<35} {summary}")

    if csv_path:
        with open(csv_path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(findings[0].keys()))
            writer.writeheader()
            writer.writerows(findings)
        print(f"\nFull detail: {csv_path}")


def apply(database_path, findings):
    """Write the changes, each guarded on the status we saw during the dry run.

    The compare-and-swap is not paranoia about our own transaction; it is about the gap between reading
    and writing, and about a second run after somebody changed something in the UI.
    """
    changes = [f for f in findings if f["action"] in (RESTORE, REVERT)]
    if not changes:
        print("Nothing to do.")
        return 0

    connection = sqlite3.connect(database_path)
    applied = skipped = 0
    try:
        connection.execute("BEGIN IMMEDIATE")
        for finding in changes:
            cursor = connection.execute(
                "UPDATE tv_episodes SET status = ? WHERE showid = ? AND season = ? AND episode = ? AND status = ?",
                (finding["target_status"], finding["showid"], finding["season"], finding["episode"], finding["current_status"]),
            )
            if cursor.rowcount:
                applied += 1
            else:
                skipped += 1
                print(f"  changed underneath us, left alone: {finding['show']} S{finding['season']:02d}E{finding['episode']:02d}")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    print(f"\nApplied {applied} of {len(changes)}. {skipped} had changed since the dry run and were left alone.")
    return applied


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--datadir", required=True, help="SickChill data directory holding sickchill.db")
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run, database opened read-only)")
    parser.add_argument(
        "--on-disk-status",
        choices=("downloaded", "archived"),
        default="downloaded",
        help="what an episode with a verified file becomes. 'downloaded' restores it exactly as a rescan would, and SickChill may "
        "then try the quality upgrade it was attempting when it got stranded. 'archived' keeps the file and never upgrades it.",
    )
    parser.add_argument("--min-age-days", type=float, default=2.0, help="never touch an episode snatched more recently than this")
    parser.add_argument("--csv", default=None, help="write the full per-episode detail here")
    parser.add_argument("--i-know-sickchill-is-running", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    database_path = os.path.join(args.datadir, "sickchill.db")
    if not os.path.isfile(database_path):
        sys.exit(f"no database at {database_path}")

    if args.apply and sickchill_is_running() and not args.i_know_sickchill_is_running:
        sys.exit(
            "SickChill is running. It caches episodes in memory and its in-memory status wins, so it would\n"
            "write the old status back over this. Stop it first."
        )

    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        findings = gather(connection, args)
        orphans = orphan_count(connection)
    finally:
        connection.close()

    if not findings:
        print("Nothing is stranded.")
        return

    report(findings, load_quality(), args.csv, orphans)

    if not args.apply:
        print("\nDry run. Nothing was written. Re-run with --apply to write it.")
        return

    print(f"\nApplying to {database_path} ...")
    apply(database_path, findings)


if __name__ == "__main__":
    main()
