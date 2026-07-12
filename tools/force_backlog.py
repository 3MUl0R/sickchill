#!/usr/bin/env python3
"""Force a backlog search run, the way the Manage -> Manage Searches button does.

Run inside the container. Reads web credentials from config.ini locally; they never leave the box.
Needed because writing WANTED with SQL queues nothing: only the UI setStatus path and the backlog
searcher turn WANTED rows into queued searches, so after a bulk status change force a run here.

A forced run is NOT automatically a full pass: BacklogSearcher decides full vs limited from the
last_backlog value in the info table, and a limited pass only covers episodes that aired in the
last BACKLOG_DAYS. Pass --full to reset last_backlog first so old episodes are included. That is
safe against a live SickChill because searchBacklog() re-reads last_backlog from the DB every run.

If config.ini has encryption_version != 0 the stored web_password is encrypted and cannot be used
for login as-is; pass the real password with --password in that case.

    docker exec sickChill /lsiopy/bin/python3 /tmp/force_backlog.py --datadir /config [--full]
"""

import argparse
import configparser
import http.cookiejar
import os
import sqlite3
import sys
import urllib.parse
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datadir", required=True)
    parser.add_argument("--full", action="store_true", help="reset last_backlog so the forced run is a full pass, not a limited BACKLOG_DAYS pass")
    parser.add_argument("--password", help="web password, for configs with encryption_version != 0")
    args = parser.parse_args()

    config = configparser.ConfigParser(strict=False, interpolation=None)
    config.read(os.path.join(args.datadir, "config.ini"))
    general = config["General"]
    username = general.get("web_username", "").strip().strip('"')
    password = args.password or general.get("web_password", "").strip().strip('"')
    encryption_version = general.get("encryption_version", "0").strip().strip('"')
    port = general.get("web_port", "8081").strip().strip('"')
    root = general.get("web_root", "").strip().strip('"')
    base = f"http://localhost:{port}{root}"

    if encryption_version not in ("", "0") and not args.password:
        sys.exit(f"config.ini has encryption_version={encryption_version}: the stored web_password is encrypted; pass the real one with --password")

    if args.full:
        connection = sqlite3.connect(os.path.join(args.datadir, "sickchill.db"), timeout=30)
        try:
            with connection:
                changed = connection.execute("UPDATE info SET last_backlog = 1").rowcount
        finally:
            connection.close()
        print(f"reset last_backlog on {changed} info row(s): the forced run will be a full pass")

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    login = urllib.parse.urlencode({"username": username, "password": password}).encode()
    with opener.open(f"{base}/login", data=login, timeout=30) as response:
        response.read()
    if not any(cookie.name == "sickchill_user" for cookie in jar):
        sys.exit("login failed: no session cookie granted")

    with opener.open(f"{base}/manage/manageSearches/forceBacklog", timeout=30) as response:
        response.read()
        print(f"forceBacklog: HTTP {response.status} (watch the log for 'Backlog search forced')")


if __name__ == "__main__":
    main()
