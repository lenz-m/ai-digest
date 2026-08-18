"""Stage 2b: the I/O layer -- actual HTTP requests and feed parsing.

Deliberately thin. All the logic that can hide bugs (hostname
classification, HTML parsing, cache freshness) lives in fetch_strategy.py
and is unit-tested there without needing a network connection. This module
is mostly "call httpx / feedparser correctly" and is verified by actually
running it against real sources on a machine with network access, not by
unit tests against canned HTML.

One dead/slow/malformed source must never kill the run: fetch_source()
catches broadly and degrades to an empty result + a logged warning, per the
project's "one dead feed must not kill the run" requirement.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import feedparser
import httpx
import trafilatura

from pipeline.fetch_strategy import (
    FetchOutcome,
    RawItem,
    StrategyCache,
    classify_by_hostname,
    extract_alternate_feed_links,
    extract_listing_links,
    extract_youtube_channel_id,
    filter_recent,
    youtube_feed_url,
    FEED_SUFFIXES,
)
from pipeline.ingest import Source

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 10.0
# A realistic browser UA, not a bot string. Publishers like the Economist
# 403 an obvious bot UA even on their public RSS feeds (confirmed in stage 2:
# economist.com returned 403 to the old "ai-digest/0.1 bot" UA, while the
# same feed renders fine in a browser). This is a single weekly fetch of
# feeds the user subscribes to, so presenting as a normal browser is
# appropriate. If a source ever needs the honest bot UA instead, that's a
# per-source override we can add later.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


def _get(client: httpx.Client, url: str) -> httpx.Response | None:
    try:
        resp = client.get(
            url,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )
        if resp.status_code >= 400:
            logger.warning("GET %s -> HTTP %d", url, resp.status_code)
            return None
        return resp
    except httpx.HTTPError as e:
        logger.warning("GET %s failed: %s", url, e)
        return None


def _parse_feed(client: httpx.Client, feed_url: str):
    """Fetch a feed THROUGH httpx (so it uses our browser UA, redirects, and
    error handling) and hand the bytes to feedparser -- never let feedparser
    do its own fetch. feedparser's built-in fetch uses a bot User-Agent that
    publishers like the Economist 403, which silently misclassified their
    feeds as un-parseable and dropped them to a broken listing-scrape. Returns
    the parsed feedparser result, or None if the fetch failed."""
    resp = _get(client, feed_url)
    if resp is None:
        return None
    return feedparser.parse(resp.content)


def _feed_has_entries(client: httpx.Client, feed_url: str) -> bool:
    parsed = _parse_feed(client, feed_url)
    return bool(parsed and parsed.entries)


def discover_feed(client: httpx.Client, url: str) -> str | None:
    """Feed autodiscovery via <link rel="alternate"> on the page itself."""
    resp = _get(client, url)
    if resp is None:
        return None
    for feed_url in extract_alternate_feed_links(resp.text, str(resp.url)):
        if _feed_has_entries(client, feed_url):
            return feed_url
    return None


def probe_feed_suffixes(client: httpx.Client, url: str) -> str | None:
    """Try common feed paths (/feed, /rss, etc.) directly."""
    base = url.rstrip("/")
    for suffix in FEED_SUFFIXES:
        candidate = base + suffix
        if _feed_has_entries(client, candidate):
            return candidate
    return None


def resolve_youtube_channel(client: httpx.Client, url: str) -> str | None:
    resp = _get(client, url)
    if resp is None:
        return None
    return extract_youtube_channel_id(resp.text)


def detect_strategy(client: httpx.Client, source: Source) -> tuple[str, str | None]:
    """Full detection, including the network probing the hostname-only fast
    path in fetch_strategy.py can't do."""
    strategy, detail = classify_by_hostname(source.url)

    if strategy == "youtube":
        channel_id = resolve_youtube_channel(client, source.url)
        if channel_id is None:
            logger.warning("%s: couldn't resolve YouTube channel ID, marking unsupported", source.name)
            return ("unsupported", None)
        return ("rss", youtube_feed_url(channel_id))

    if strategy != "unknown":
        return (strategy, detail)

    # The source URL may already BE a feed (e.g. HBR's
    # feeds.harvardbusiness.org/harvardbusiness). Check that first, before
    # treating it as a web page to autodiscover/scrape -- otherwise we'd try
    # to parse feed XML as HTML and fall through to a broken listing scrape.
    if _feed_has_entries(client, source.url):
        return ("rss", source.url)

    feed_url = discover_feed(client, source.url) or probe_feed_suffixes(client, source.url)
    if feed_url:
        return ("rss", feed_url)

    return ("listing", None)


def fetch_rss(client: httpx.Client, feed_url: str, source_name: str) -> list[RawItem]:
    parsed = _parse_feed(client, feed_url)
    if parsed is None:
        return []
    items: list[RawItem] = []
    for entry in parsed.entries:
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        if not title or not link:
            continue
        published = None
        if entry.get("published_parsed"):
            published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        excerpt = (entry.get("summary") or "")[:500]
        items.append(RawItem(title=title, url=link, source=source_name, published=published, excerpt=excerpt))
    return items


def fetch_listing(client: httpx.Client, source: Source, limit: int = 30) -> list[RawItem]:
    resp = _get(client, source.url)
    if resp is None:
        return []
    links = extract_listing_links(resp.text, str(resp.url))
    return [RawItem(title=text, url=href, source=source.name) for href, text in links[:limit]]


def fetch_article_text(client: httpx.Client, url: str) -> str:
    """Full article text extraction -- deliberately NOT called during the
    main fetch pass (fetch_source/fetch_all only pull title/url/published/
    excerpt). Only called for the handful of items that survive the cheap
    filter stage, so bandwidth isn't spent extracting full text for the
    hundreds of candidates that get discarded anyway. Returns "" on any
    failure (network, extraction) -- the score stage degrades to scoring
    from title/excerpt alone rather than failing the item outright.
    """
    resp = _get(client, url)
    if resp is None:
        return ""
    try:
        text = trafilatura.extract(resp.text, url=str(resp.url), favor_recall=True)
        return text or ""
    except Exception as e:  # noqa: BLE001 -- extraction must not kill the run
        logger.warning("trafilatura extraction failed for %s: %s", url, e)
        return ""


def fetch_article_texts(client: httpx.Client, urls: list[str]) -> dict[str, str]:
    """Convenience wrapper for score_stage.build_score_requests(), which
    wants {url: text} for a batch of survivors."""
    return {url: fetch_article_text(client, url) for url in urls}


def fetch_source(client: httpx.Client, source: Source, cache: StrategyCache) -> FetchOutcome:
    try:
        cached = cache.get(source.name)
        human_override = bool(cached and cached.get("human_override"))
        # A cached entry is usable only if it's fresh AND was detected against
        # the current source URL. If the URL changed (e.g. a bare-URL scrape
        # entry was replaced with a real feed), the old "listing" verdict is
        # wrong and must be re-detected -- otherwise we'd try to scrape a feed
        # URL as HTML. Human overrides are deliberate, so they're kept as-is.
        # (Entries cached before URL-tracking existed have url=None, which !=
        # source.url, so they harmlessly re-detect once.)
        url_matches = cached is not None and cached.get("url") == source.url
        usable = cached is not None and not cached["stale"] and (human_override or url_matches)
        if usable:
            strategy, detail = cached["strategy"], cached.get("detail")
        else:
            strategy, detail = detect_strategy(client, source)
            if not human_override:
                cache.set(source.name, strategy, detail, url=source.url)

        if strategy == "unsupported":
            return FetchOutcome(items=[], strategy=strategy)
        if strategy == "rss":
            items = filter_recent(fetch_rss(client, detail, source.name))
            return FetchOutcome(items=items, strategy=strategy)
        if strategy == "listing":
            # filter_recent is a no-op here today (listing items never carry
            # a published date), applied anyway so this doesn't silently
            # stop working if that ever changes.
            items = filter_recent(fetch_listing(client, source))
            return FetchOutcome(items=items, strategy=strategy)
        return FetchOutcome(items=[], strategy="unknown", error="could not classify")
    except Exception as e:  # noqa: BLE001 -- per-source boundary, must not kill the run
        logger.warning("fetch failed for %s: %s", source.name, e)
        return FetchOutcome(items=[], strategy="error", error=str(e))


def fetch_all(
    sources: list[Source],
    client: httpx.Client | None = None,
    on_progress=None,
) -> tuple[list[RawItem], dict[str, str]]:
    """Fetch every source. Returns (all candidate items, {source_name:
    strategy-or-error}) -- the second is for the run report so degraded
    sources are visible, not silently missing.

    on_progress, if given, is called with each source's name just after it's
    fetched -- purely for a console progress line, has no effect on results.
    """
    owns_client = client is None
    client = client or httpx.Client()
    cache = StrategyCache()
    all_items: list[RawItem] = []
    report: dict[str, str] = {}
    try:
        for source in sources:
            outcome = fetch_source(client, source, cache)
            report[source.name] = outcome.error or outcome.strategy
            all_items.extend(outcome.items)
            if on_progress is not None:
                on_progress(source.name)
    finally:
        cache.save()
        if owns_client:
            client.close()
    return all_items, report


def _main() -> int:
    """Smoke-test entry point: `uv run python -m pipeline.fetch`.

    Fetches real sources from data/sources.tsv (or the cached fallback) and
    prints a per-source report -- strategy used, item count, first few
    titles -- so a human can eyeball whether e.g. the listing-scrape
    heuristic is pulling real headlines or nav junk on an actual site.
    Also exercises (and updates) the real cache/fetch_strategy.json.
    """
    import argparse
    from collections import Counter

    from pipeline.ingest import load_sources

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", help="Only fetch the source with this exact name")
    parser.add_argument("--limit", type=int, help="Only fetch the first N sources")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    result = load_sources()
    sources = result.sources
    if result.from_cache:
        stale_note = " -- STALE" if result.stale else ""
        print(f"(using cached source list, {result.cache_age_days} days old{stale_note})")

    if args.source:
        sources = [s for s in sources if s.name == args.source]
        if not sources:
            print(f"no source named {args.source!r} found in the loaded list")
            return 1
    elif args.limit:
        sources = sources[: args.limit]

    print(f"fetching {len(sources)} source(s)...")

    client = httpx.Client()
    cache = StrategyCache()
    outcomes: list[tuple[Source, FetchOutcome]] = []
    try:
        for source in sources:
            outcome = fetch_source(client, source, cache)
            outcomes.append((source, outcome))
    finally:
        cache.save()
        client.close()

    strategy_counts: Counter[str] = Counter()
    total_items = 0
    for source, outcome in outcomes:
        label = outcome.error or outcome.strategy
        strategy_counts[label] += 1
        total_items += len(outcome.items)
        print(f"\n{source.name}  [{label}]  ({len(outcome.items)} items)")
        for item in outcome.items[:5]:
            print(f"    - {item.title}")
        if len(outcome.items) > 5:
            print(f"    ... and {len(outcome.items) - 5} more")

    print(f"\n{'=' * 60}")
    print(f"{len(outcomes)} sources, {total_items} total candidate items")
    for label, count in strategy_counts.most_common():
        print(f"  {label}: {count}")

    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
