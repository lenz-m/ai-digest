"""Stage 1a: parse the Reminders source list into Source objects.

The Mac dumps Apple Reminders (list "Daily Digest") to a TSV via
dump_sources.applescript: header row "title\\tnotes", one source per row.
title = source name. notes = URL, possibly with trailing free text, since
the applescript flattens multi-line note bodies into one line (newlines and
tabs become spaces) before writing the row.

The live TSV can be missing or stale if the Mac has been asleep and the
rsync hasn't run recently. That must never block a digest, so we always fall
back to the last-known-good cached copy -- loudly, but we still run.
"""
from __future__ import annotations

import csv
import logging
import re
import shutil
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from pipeline.config import CONFIG

logger = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://\S+")


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    extra: str = ""  # any text in the notes field beyond the first URL


class IngestError(Exception):
    """Raised only when neither the live TSV nor any cache is usable at all."""


@dataclass(frozen=True)
class IngestResult:
    sources: list[Source]
    from_cache: bool
    cache_age_days: int | None = None
    stale: bool = False


def _parse_tsv(text: str) -> list[Source]:
    reader = csv.DictReader(text.splitlines(), delimiter="\t")
    fieldnames = reader.fieldnames or []
    if "title" not in fieldnames or "notes" not in fieldnames:
        raise IngestError(f"sources TSV missing expected header 'title\\tnotes', got {fieldnames!r}")

    sources: list[Source] = []
    for i, row in enumerate(reader, start=2):  # header is line 1
        name = (row.get("title") or "").strip()
        notes = (row.get("notes") or "").strip()
        if not name:
            logger.warning("row %d: empty title, skipping", i)
            continue
        match = URL_RE.search(notes)
        if not match:
            logger.warning("row %d (%s): no URL found in notes, skipping", i, name)
            continue
        url = match.group(0).rstrip(".,;)")  # trailing punctuation picked up by flattening
        extra = (notes[: match.start()] + notes[match.end():]).strip()
        sources.append(Source(name=name, url=url, extra=extra))
    return sources


def _load_manual_sources() -> list[Source]:
    """Load the user's own manual sources (data/manual_sources.tsv), which are
    independent of the friend's Reminders export and never overwritten by the
    Reminders rsync. Missing file -> no manual sources (not an error)."""
    path = CONFIG.manual_sources_tsv
    if not path.exists():
        return []
    try:
        return _parse_tsv(path.read_text())
    except IngestError as e:
        logger.warning("manual sources file %s unparsable (%s); ignoring it", path, e)
        return []


def load_sources(now: datetime | None = None) -> IngestResult:
    """Load Reminders-derived sources merged with the user's manual sources.

    Manual sources (data/manual_sources.tsv) are always merged in and are
    never touched by the Reminders rsync -- that's how a source like HBR's
    feed stays put regardless of what the friend's Reminders list contains.

    On a name collision the MANUAL entry wins: the manual file is the user's
    deliberate override layer, used precisely to replace a friend's bare-URL
    scrape entry (e.g. "GCP" -> cloud.google.com/blog) with a real feed
    (e.g. "GCP" -> cloudblog.withgoogle.com/rss/). A same-named manual entry
    replaces the Reminders one in place; manual-only entries are appended.
    """
    result = _load_reminders_sources(now)
    manual = _load_manual_sources()
    if manual:
        manual_by_name = {m.name: m for m in manual}
        merged = [manual_by_name.get(s.name, s) for s in result.sources]  # override in place
        used = {s.name for s in result.sources}
        merged += [m for m in manual if m.name not in used]  # append manual-only, in file order
        result = replace(result, sources=merged)
    return result


def _load_reminders_sources(now: datetime | None = None) -> IngestResult:
    """Load sources from the Reminders export, preferring the live TSV and
    falling back to the last-known-good cache.

    Raises IngestError only when nothing usable exists at all (no live file
    and no cache) -- that's a genuine "cannot run" condition the caller
    should surface loudly rather than silently produce an empty digest.
    """
    now = now or datetime.now(timezone.utc)
    live = CONFIG.sources_tsv
    cache = CONFIG.sources_cache

    if live.exists():
        try:
            sources = _parse_tsv(live.read_text())
            if sources:
                cache.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(live, cache)
                return IngestResult(sources=sources, from_cache=False)
            logger.warning("live sources TSV parsed to zero sources; falling back to cache")
        except IngestError as e:
            logger.warning("live sources TSV unparsable (%s); falling back to cache", e)
    else:
        logger.warning("live sources TSV not found at %s; falling back to cache", live)

    if not cache.exists():
        raise IngestError(f"no live sources TSV at {live} and no cached fallback at {cache} -- cannot run")

    age_days = (now - datetime.fromtimestamp(cache.stat().st_mtime, tz=timezone.utc)).days
    stale = age_days > CONFIG.sources_stale_after_days
    if stale:
        logger.warning(
            "using STALE source cache (%d days old, threshold %d) -- Mac hasn't synced recently",
            age_days,
            CONFIG.sources_stale_after_days,
        )
    else:
        logger.warning("using cached sources (%d days old) -- live TSV unavailable", age_days)

    sources = _parse_tsv(cache.read_text())
    return IngestResult(sources=sources, from_cache=True, cache_age_days=age_days, stale=stale)
