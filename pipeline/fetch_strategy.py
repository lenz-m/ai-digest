"""Stage 2a: pure decision logic for fetching sources -- no network calls.

Deliberately stdlib-only (no httpx/feedparser/trafilatura) so this half of
the fetch stage is fully unit-testable without a live network connection or
those dependencies installed. pipeline/fetch.py imports from here and adds
the thin I/O layer (actual HTTP requests, actual feed parsing) on top.

Four strategies a source can end up with:
  - "rss": a discoverable RSS/Atom feed exists. detail = the feed URL.
  - "youtube": a YouTube channel URL. Needs one extra fetch to resolve the
    channel ID before a feed URL exists -- handled in fetch.py.
  - "listing": no feed exists anywhere. Fall back to scraping same-domain
    article-shaped links off the page itself. Noisier than RSS by nature --
    the stage 3 cheap filter is what cleans this up, not this stage.
  - "unsupported": a known dead end (e.g. X/Twitter has no free RSS and no
    reliable free alternative). Skipped every run, logged once.
  - "unknown": classify_by_hostname() couldn't decide from the hostname
    alone; fetch.py's I/O layer needs to actually probe the site (feed
    autodiscovery, common feed-path suffixes) before falling back to
    "listing".
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from pipeline.config import CONFIG

# Hosts with no free/reliable RSS available, confirmed dead ends -- don't
# waste a probe on these every week.
UNSUPPORTED_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}

YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com"}

# Sites with a well-known feed path that autodiscovery/suffix-probing would
# eventually find anyway, but hardcoding skips the extra round trip -- or,
# in TechMeme's case, sites where the real page is JS-rendered enough that
# the listing-scrape fallback found nothing but sponsor-post chrome, so the
# feed needed to be found by hand and hardcoded rather than discovered.
KNOWN_FEEDS = {
    "news.ycombinator.com": "https://news.ycombinator.com/rss",
    "www.techmeme.com": "https://www.techmeme.com/feed.xml",
    "techmeme.com": "https://www.techmeme.com/feed.xml",
}

# Common feed path suffixes to probe when a site doesn't self-advertise a
# feed via <link rel="alternate">. /feed.xml (TechMeme, among others) was
# missing from the original list -- found via a real-source smoke test.
FEED_SUFFIXES = (
    "/feed",
    "/feed/",
    "/feed.xml",
    "/rss",
    "/rss.xml",
    "/atom.xml",
    "/index.xml",
)


@dataclass(frozen=True)
class RawItem:
    """A candidate story pulled from a source, before dedupe/scoring."""

    title: str
    url: str
    source: str
    published: datetime | None = None
    excerpt: str = ""


@dataclass(frozen=True)
class FetchOutcome:
    items: list[RawItem]
    strategy: str
    error: str | None = None


def filter_recent(
    items: list[RawItem], max_age_days: int | None = None, now: datetime | None = None
) -> list[RawItem]:
    """Drop items older than max_age_days -- a weekly digest shouldn't
    surface a feed's entire back catalog just because a source's RSS feed
    returns full history rather than "recent" entries (found via a real
    OpenAI feed returning 1038 items on one fetch).

    Items with no known published date are kept as-is: we can't judge their
    age without fetching the article page, and listing-scraped items in
    particular almost never have one. The persistent seen-set and stage 3's
    filter are the backstop for those instead.
    """
    max_age_days = CONFIG.fetch_max_age_days if max_age_days is None else max_age_days
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max_age_days)
    return [item for item in items if item.published is None or item.published >= cutoff]


def substack_feed_url(url: str) -> str | None:
    """*.substack.com -> <host>/feed. open.substack.com/pub/<name> -> the
    underlying <name>.substack.com/feed (the open.substack.com URL is just a
    cross-newsletter reader wrapper)."""
    parts = urlsplit(url)
    host = parts.netloc.lower()
    if host.endswith(".substack.com") and host != "open.substack.com":
        return f"https://{host}/feed"
    if host == "open.substack.com":
        m = re.match(r"^/pub/([^/]+)", parts.path)
        if m:
            return f"https://{m.group(1)}.substack.com/feed"
    return None


def classify_by_hostname(url: str) -> tuple[str, str | None]:
    """Fast-path classification from the URL alone, no network needed.

    Returns (strategy, detail). detail is a feed URL when strategy == "rss",
    otherwise None. strategy == "unknown" means fetch.py needs to actually
    probe the site before a real answer exists.
    """
    host = urlsplit(url).netloc.lower()

    if host in UNSUPPORTED_HOSTS:
        return ("unsupported", None)
    if host in YOUTUBE_HOSTS:
        return ("youtube", None)

    feed = substack_feed_url(url)
    if feed:
        return ("rss", feed)

    if host in KNOWN_FEEDS:
        return ("rss", KNOWN_FEEDS[host])

    return ("unknown", None)


# --- HTML parsing helpers (stdlib html.parser, no BeautifulSoup dependency) ---

_LINK_TAG_RE = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_REL_ALTERNATE_RE = re.compile(r'rel=["\']alternate["\']', re.IGNORECASE)
_TYPE_FEED_RE = re.compile(r'type=["\']application/(?:rss|atom)\+xml["\']', re.IGNORECASE)
_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)


def extract_alternate_feed_links(html: str, base_url: str) -> list[str]:
    """Feed autodiscovery: find <link rel="alternate" type="application/
    (rss|atom)+xml" href="..."> tags, in any attribute order, resolved to
    absolute URLs."""
    links: list[str] = []
    for tag in _LINK_TAG_RE.findall(html):
        if _REL_ALTERNATE_RE.search(tag) and _TYPE_FEED_RE.search(tag):
            m = _HREF_RE.search(tag)
            if m:
                links.append(urljoin(base_url, m.group(1)))
    return links


_CHANNEL_ID_RE = re.compile(r'"channelId":"(UC[0-9A-Za-z_-]{10,})"')
_CHANNEL_ID_META_RE = re.compile(r'<meta itemprop="channelId" content="(UC[0-9A-Za-z_-]{10,})"')
# Found necessary via a real fetch: georgevetticaden599's channel page had
# neither of the two patterns above (jeffersonfisher's did), so these two
# more standard/stable SEO tags -- present on channel pages regardless of
# subscriber count, unlike the JSON blob -- are tried as well.
_CHANNEL_ID_CANONICAL_RE = re.compile(
    r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[0-9A-Za-z_-]{10,})"'
)
_CHANNEL_ID_OG_URL_RE = re.compile(
    r'<meta property="og:url" content="https://www\.youtube\.com/channel/(UC[0-9A-Za-z_-]{10,})"'
)


def extract_youtube_channel_id(html: str) -> str | None:
    for pattern in (
        _CHANNEL_ID_RE,
        _CHANNEL_ID_META_RE,
        _CHANNEL_ID_CANONICAL_RE,
        _CHANNEL_ID_OG_URL_RE,
    ):
        m = pattern.search(html)
        if m:
            return m.group(1)
    return None


def youtube_feed_url(channel_id: str) -> str:
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


class _ListingLinkParser(HTMLParser):
    """Collects (href, link text) pairs, skipping anything inside nav/
    header/footer since those are almost never the article content we
    want."""

    EXCLUDE_TAGS = {"nav", "footer", "header"}

    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links: list[tuple[str, str]] = []
        self._exclude_depth = 0
        self._current_href: str | None = None
        self._current_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.EXCLUDE_TAGS:
            self._exclude_depth += 1
        if tag == "a" and self._exclude_depth == 0:
            href = dict(attrs).get("href")
            if href:
                self._current_href = urljoin(self.base_url, href)
                self._current_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag in self.EXCLUDE_TAGS:
            self._exclude_depth = max(0, self._exclude_depth - 1)
        if tag == "a" and self._current_href is not None:
            text = " ".join("".join(self._current_text).split())
            if text:
                self.links.append((self._current_href, text))
            self._current_href = None
            self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._current_href is not None:
            self._current_text.append(data)


# Exact-match link text that's almost never a real headline, found by
# running the heuristic against real sites: A16Z ("Learn More" x25 on one
# page), iShares/Every ("Skip to content"), AI Courses ("Forgot password?",
# "Create account", "Sign in here!"), TechMeme ("Leaderboards"), etc.
_BOILERPLATE_PHRASES = {
    "learn more",
    "read more",
    "show more",
    "sign in",
    "sign up",
    "sign in here",
    "log in",
    "create account",
    "forgot password",
    "skip to content",
    "skip to main content",
    "buy tickets",
    "read the post",
    "leaderboards",
    "subscribe",
}


def extract_listing_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """Heuristic article-link extraction for sites with no feed at all:
    same-domain links outside nav/header/footer, with link text long enough
    to plausibly be a headline rather than "Home" / "Contact" / an icon, not
    a known boilerplate phrase, and not repeated more than twice on the same
    page (real headlines don't repeat -- CTAs and nav labels do). Still
    deliberately noisy-permissive beyond that -- stage 3's cheap filter is
    what actually separates real stories from nav junk, not this heuristic.
    """
    parser = _ListingLinkParser(base_url)
    parser.feed(html)

    base_host = urlsplit(base_url).netloc.lower()
    seen_hrefs: set[str] = set()
    candidates: list[tuple[str, str]] = []
    for href, text in parser.links:
        if urlsplit(href).netloc.lower() != base_host:
            continue
        if len(text) < 8:
            continue
        if text.strip().lower() in _BOILERPLATE_PHRASES:
            continue
        if href in seen_hrefs:
            continue
        seen_hrefs.add(href)
        candidates.append((href, text))

    text_counts = Counter(text for _, text in candidates)
    return [(href, text) for href, text in candidates if text_counts[text] <= 2]


class StrategyCache:
    """Persistent JSON cache of detected fetch strategy per source, keyed by
    source name. Same pattern as SeenStore / the vault scripts' confidence
    caches: stable keys, survives re-runs, human-correctable.

    A cached entry older than CONFIG.fetch_strategy_max_age_days is still
    returned but flagged "stale" so the caller can decide to re-probe --
    unless human_override is set, which never expires.
    """

    def __init__(self, path: Path | None = None):
        self.path = path or CONFIG.fetch_strategy_cache
        self._data: dict[str, dict] = {}
        if self.path.exists():
            self._data = json.loads(self.path.read_text(encoding="utf-8"))

    def get(self, source_name: str, now: datetime | None = None) -> dict | None:
        entry = self._data.get(source_name)
        if entry is None:
            return None
        entry = dict(entry)
        if entry.get("human_override"):
            entry["stale"] = False
            return entry
        now = now or datetime.now(timezone.utc)
        detected_at = datetime.fromisoformat(entry["detected_at"])
        age_days = (now - detected_at).days
        entry["stale"] = age_days > CONFIG.fetch_strategy_max_age_days
        return entry

    def set(
        self,
        source_name: str,
        strategy: str,
        detail: str | None,
        human_override: bool = False,
        now: datetime | None = None,
        url: str | None = None,
    ) -> None:
        now = now or datetime.now(timezone.utc)
        self._data[source_name] = {
            "strategy": strategy,
            "detail": detail,
            "detected_at": now.isoformat(),
            "human_override": human_override,
            "url": url,  # source URL at detection time; a change invalidates the entry
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8")
