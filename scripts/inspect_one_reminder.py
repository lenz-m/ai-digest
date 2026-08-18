#!/usr/bin/env python3
"""Diagnostic (read-only): look up ONE known reminder by title across all
Reminders SQLite stores, and try to decode the CKRecord archive blob
(ZCKSERVERRECORDDATA) to see whether the Reminders UI's "URL" field is
actually populated there, or just present as an unused schema key.

Usage:
    python3 scripts/inspect_one_reminder.py "Jefferson Phisher"
"""
from __future__ import annotations

import glob
import os
import plistlib
import sqlite3
import sys


def resolve(objects, val, depth=0, seen=None):
    seen = seen if seen is not None else set()
    if depth > 8:
        return "<max depth>"
    if isinstance(val, plistlib.UID):
        idx = val.data
        if idx in seen:
            return f"<cycle to {idx}>"
        return resolve(objects, objects[idx], depth + 1, seen | {idx})
    if isinstance(val, dict):
        return {k: resolve(objects, v, depth + 1, seen) for k, v in val.items() if k != "$class"}
    if isinstance(val, list):
        return [resolve(objects, v, depth + 1, seen) for v in val]
    if isinstance(val, bytes):
        return f"<{len(val)} bytes>"
    return val


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: inspect_one_reminder.py <exact reminder title>")
        return 1
    title = sys.argv[1]

    db_glob = os.path.expanduser(
        "~/Library/Group Containers/group.com.apple.reminders/Container_v1/Stores/*.sqlite"
    )
    for db_path in glob.glob(db_glob):
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cur = con.cursor()
        try:
            rows = cur.execute(
                'SELECT Z_PK, ZTITLE, ZNOTES, ZCKSERVERRECORDDATA FROM ZREMCDREMINDER WHERE ZTITLE = ?',
                (title,),
            ).fetchall()
        except sqlite3.OperationalError as e:
            print(f"{db_path}: query failed ({e})")
            con.close()
            continue

        for pk, ztitle, znotes, ckdata in rows:
            print(f"\n{db_path}")
            print(f"  Z_PK={pk} ZTITLE={ztitle!r} ZNOTES={znotes!r}")
            if ckdata is None:
                print("  ZCKSERVERRECORDDATA is NULL -- nothing to decode")
                continue

            try:
                top = plistlib.loads(ckdata)
            except Exception as e:
                print(f"  plistlib failed to parse blob at all: {e}")
                continue

            print(f"  top-level keys: {list(top.keys())}")
            print(f"  \\$archiver = {top.get('$archiver')!r}  \\$version = {top.get('$version')!r}")
            print(f"  \\$top raw value = {top.get('$top')!r}")

            objects = top.get("$objects")
            if objects is None:
                print("  no $objects table -- can't resolve further")
                continue

            # Try every entry in $top (not just "root") since CKRecord
            # archives may name it differently.
            for key, uid in (top.get("$top") or {}).items():
                print(f"\n  resolving \\$top[{key!r}]:")
                print(f"  {resolve(objects, uid)}")

            # Also just brute-force scan every string in $objects for
            # anything that looks like our field name or a URL, in case
            # the graph walk above misses it.
            print("\n  raw scan of \\$objects for 'URL' / 'http':")
            for i, obj in enumerate(objects):
                if isinstance(obj, str) and ("URL" in obj or "http" in obj.lower()):
                    print(f"    [{i}] {obj!r}")
        con.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
