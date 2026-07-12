#!/usr/bin/env python3
"""Queue a SickChill refresh for every show that has episodes stranded in a snatched status.

Run inside the container. Reads the API key from config.ini locally; it never leaves the box.

    docker exec sickChill /lsiopy/bin/python3 /tmp/refresh_stranded_shows.py --datadir /config

A refresh makes SickChill itself attach any parseable on-disk file to a location-empty episode
(make_ep_from_file sets it DOWNLOADED), so the reconciliation afterwards only reverts episodes that
SickChill genuinely cannot recover from disk.
"""

import argparse
import configparser
import json
import os
import sqlite3
import sys
import time
import urllib.request

STRANDED = (2, 9, 12)  # SNATCHED, SNATCHED_PROPER, SNATCHED_BEST


def api_settings(datadir):
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.read(os.path.join(datadir, "config.ini"))
    general = parser["General"]
    key = general.get("api_key", "").strip().strip('"')
    port = general.get("web_port", "8081").strip().strip('"')
    root = general.get("web_root", "").strip().strip('"')
    if not key:
        sys.exit("no api_key in config.ini; enable the API in the web UI first")
    return key, port, root


def stranded_show_ids(datadir):
    connection = sqlite3.connect(f"file:{os.path.join(datadir, 'sickchill.db')}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            """
            SELECT DISTINCT e.showid, s.show_name
            FROM tv_episodes e JOIN tv_shows s ON s.indexer_id = e.showid
            WHERE e.status % 100 IN (?, ?, ?)
            ORDER BY s.show_name
            """,
            STRANDED,
        ).fetchall()
    finally:
        connection.close()
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datadir", required=True)
    args = parser.parse_args()

    key, port, root = api_settings(args.datadir)
    shows = stranded_show_ids(args.datadir)
    print(f"{len(shows)} shows have stranded episodes")

    queued = failed = 0
    for showid, name in shows:
        url = f"http://localhost:{port}{root}/api/{key}/?cmd=show.refresh&indexerid={showid}"
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                payload = json.load(response)
        except Exception as error:
            print(f"  FAILED {name}: {error.__class__.__name__}: {error}")
            failed += 1
            continue
        if payload.get("result") == "success":
            queued += 1
        else:
            # "already being refreshed" style answers land here; they are fine
            print(f"  {name}: {payload.get('message', payload)}")
            failed += 1
        time.sleep(0.2)  # do not hammer the queue lock

    print(f"\nqueued {queued}, not queued {failed}. Watch the log for 'Performing refresh on' lines.")


if __name__ == "__main__":
    main()
