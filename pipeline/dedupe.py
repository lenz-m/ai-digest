"""Stage 1b: persistent, cross-run dedupe.

Two layers:
  1. Exact dedupe via a canonical-URL hash, persisted forever in a JSON
     seen-set, so an item never reappears even weeks later or via a
     different source.
  2. Fuzzy cross-source dedupe via title similarity (difflib, stdlib only)
     for the same story syndicated under different URLs by two sources in
     the same run. Threshold matches the 0.90 convention already used in
     the notion-to-obsidian scripts' title_similarity().

This runs BEFORE any LLM call reaches an item. The cheapest API call is the
one never made, and this is also what makes "never repeat yourself" a hard
guarantee rather than a ranking preference.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pipeline.config import CONFIG
from pipeline.fetch_strategy import RawItem

logger = logging.getLogger(__name__)

# Query params that don't change the underlying content -- stripped before
# hashing so tracking-tagged links from different sources still collide.
_TRACKING_PARAM_PREFIXES = ("utm_", "ref", "fbclid", "gclid", "mc_cid", "mc_eid", "igshid")


@dataclass(frozen=True)
class Candidate:
    """A dedupe-able unit: one story pulled from one source.

    published/excerpt carry through from fetch_strategy.RawItem (dedupe only
    ever keys off title/url/source, but stage 3 needs the rest -- excerpt
    for the cheap filter's judgment, since it doesn't have full article text).
    """

    title: str
    url: str
    source: str
    published: datetime | None = None
    excerpt: str = ""


def candidate_from_raw_item(item: RawItem) -> Candidate:
    """Converts a fetch_strategy.RawItem (stage 2's output) into a Candidate
    (stage 1b/3's input)."""
    return Candidate(
        title=item.title,
        url=item.url,
        source=item.source,
        published=item.published,
        excerpt=item.excerpt,
    )


def canonicalize_url(url: str) -> str:
    """Strip tracking params, fragment, scheme/www/trailing-slash variance."""
    parts = urlsplit(url.strip())
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = parts.path.rstrip("/") or "/"
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not any(k.lower().startswith(p) for p in _TRACKING_PARAM_PREFIXES)
    ]
    return urlunsplit(("https", netloc, path, urlencode(sorted(query)), ""))


def content_hash(candidate: Candidate) -> str:
    canon = canonicalize_url(candidate.url)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def _normalize_title(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def title_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _normalize_title(a), _normalize_title(b)).ratio()


class SeenStore:
    """Persistent JSON seen-set, keyed by content hash. Same cache pattern
    (stable keys, survives re-runs, JSON alongside the script) as
    add_covers.py / enrich_media.py in the vault scripts."""

    def __init__(self, path: Path | None = None):
        self.path = path or CONFIG.seen_cache
        self._data: dict[str, dict] = {}
        if self.path.exists():
            self._data = json.loads(self.path.read_text(encoding="utf-8"))

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def __len__(self) -> int:
        return len(self._data)

    def mark_seen(self, candidate: Candidate, key: str | None = None) -> None:
        """Idempotent: re-marking an item already in the store is a no-op.

        Needed because the commit set includes items that were dropped
        BECAUSE they were already seen -- overwriting would reset first_seen
        on every run and lose the one piece of history this store keeps.
        """
        key = key or content_hash(candidate)
        if key in self._data:
            return
        self._data[key] = {
            "title": candidate.title,
            "url": candidate.url,
            "source": candidate.source,
            "first_seen": datetime.now(timezone.utc).isoformat(),
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8")


def dedupe(
    candidates: list[Candidate],
    seen: SeenStore,
    threshold: float | None = None,
) -> tuple[list[Candidate], list[Candidate]]:
    """Split candidates into (new, dropped).

    Drops anything already in the persistent seen-set (exact canonical-URL
    match), then clusters the remainder by title similarity *across
    different sources* within this run, so the same story covered by two
    sources only survives once.

    Cross-source only is deliberate: some newsletters reuse a templated
    section title every single edition (e.g. Exponential View's recurring
    "Data to start your week", or "Top AI Papers of the Week") -- those are
    different editions with different URLs, not duplicates, and clustering
    within the same source would silently drop real content. Cross-source
    syndication of the same story is the actual thing this is for.

    Does NOT call seen.save() or mark anything seen -- the caller decides
    when a run is committed enough to persist (e.g. only after the email
    actually sends), so a crashed mid-run doesn't burn an item's one shot.
    """
    threshold = CONFIG.title_similarity_threshold if threshold is None else threshold

    new: list[Candidate] = []
    dropped: list[Candidate] = []
    kept_hashes: set[str] = set()

    for c in candidates:
        h = content_hash(c)
        if h in seen or h in kept_hashes:
            dropped.append(c)
            continue

        if any(
            kept.source != c.source and title_similarity(c.title, kept.title) >= threshold
            for kept in new
        ):
            dropped.append(c)
            continue

        new.append(c)
        kept_hashes.add(h)

    return new, dropped
