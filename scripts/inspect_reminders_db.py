#!/usr/bin/env python3
"""Diagnostic (read-only, no writes): dump the real schema of the Reminders
app's private SQLite database, since AppleScript and EventKit can't read the
per-reminder URL field (confirmed Apple bug -- EKReminder.url returns nil
even when the Reminders UI shows a link).

This does NOT extract sources.tsv. It just prints table/column names and a
few sample rows so we can see this Mac's actual schema before writing real
extraction logic against it -- undocumented Core Data schemas like this can
differ across macOS versions, so guessing column names from a blog post
elsewhere would risk silently pulling the wrong data.

Usage:
    python3 scripts/inspect_reminders_db.py

Requires Full Disk Access granted to whatever runs this (Terminal, iTerm,
or your Python interpreter) -- System Settings > Privacy & Security > Full
Disk Access. Without it, sqlite3 will raise "unable to open database file"
even though the path exists.
"""
from __future__ import annotations

import glob
import os
import sqlite3
import sys

DB_GLOB = os.path.expanduser(
    "~/Library/Group Containers/group.com.apple.reminders/Container_v1/Stores/*.sqlite"
)

INTERESTING = ("REMIND", "URL", "TASK")


def inspect(db_path: str) -> None:
    print(f"\n{'=' * 70}\n{db_path}\n{'=' * 70}")
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError as e:
        print(f"  could not open (Full Disk Access likely not granted yet): {e}")
        return

    cur = con.cursor()
    tables = [
        row[0]
        for row in cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    ]
    print(f"  {len(tables)} tables total")

    relevant = [t for t in tables if any(k in t.upper() for k in INTERESTING)]
    if not relevant:
        print("  no table names matched REMIND / URL / TASK -- dumping full table list instead:")
        for t in tables:
            print(f"    {t}")
        con.close()
        return

    for t in relevant:
        cols = cur.execute(f'PRAGMA table_info("{t}")').fetchall()
        col_names = [c[1] for c in cols]
        print(f"\n  --- {t} ---")
        print(f"  columns: {col_names}")
        try:
            rows = cur.execute(f'SELECT * FROM "{t}" LIMIT 3').fetchall()
            for r in rows:
                print(f"    {r}")
        except sqlite3.OperationalError as e:
            print(f"    (could not sample rows: {e})")

    con.close()


def main() -> int:
    paths = glob.glob(DB_GLOB)
    if not paths:
        print(f"No Reminders database found at {DB_GLOB}")
        print("Reminders may store data elsewhere on this macOS version -- ")
        print("try: mdfind \"kMDItemFSName == '*.sqlite'\" | grep -i remind")
        return 1

    for p in paths:
        inspect(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
