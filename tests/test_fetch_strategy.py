from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pipeline import config as config_module
from pipeline.fetch_strategy import (
    RawItem,
    StrategyCache,
    classify_by_hostname,
    extract_alternate_feed_links,
    extract_listing_links,
    extract_youtube_channel_id,
    filter_recent,
    substack_feed_url,
    youtube_feed_url,
)


# --- classify_by_hostname, using the real sources.tsv URLs as ground truth ---

def test_classify_unsupported_x_twitter():
    assert classify_by_hostname("https://x.com/scmallaby?s=21") == ("unsupported", None)
    assert classify_by_hostname("https://twitter.com/someone") == ("unsupported", None)


def test_classify_youtube():
    assert classify_by_hostname("https://www.youtube.com/@georgevetticaden599") == ("youtube", None)
    assert classify_by_hostname("https://youtube.com/@jeffersonfisher?si=abc") == ("youtube", None)


def test_classify_substack_subdomain():
    strategy, detail = classify_by_hostname("https://tbpn.substack.com")
    assert strategy == "rss"
    assert detail == "https://tbpn.substack.com/feed"


def test_classify_open_substack_wrapper():
    strategy, detail = classify_by_hostname(
        "https://open.substack.com/pub/nlpnews?r=24lwo&utm_medium=ios"
    )
    assert strategy == "rss"
    assert detail == "https://nlpnews.substack.com/feed"


def test_classify_known_feed_hacker_news():
    assert classify_by_hostname("https://news.ycombinator.com") == (
        "rss",
        "https://news.ycombinator.com/rss",
    )


def test_classify_known_feed_techmeme():
    # Found via a real-source smoke test: TechMeme's homepage is JS-rendered
    # enough that both autodiscovery and the listing-scrape fallback only
    # picked up sponsor-post chrome, so this is hardcoded like Hacker News.
    assert classify_by_hostname("https://www.techmeme.com/m/") == (
        "rss",
        "https://www.techmeme.com/feed.xml",
    )


def test_classify_unknown_for_ordinary_site():
    strategy, detail = classify_by_hostname("https://stratechery.com")
    assert strategy == "unknown"
    assert detail is None


def test_substack_feed_url_non_substack_returns_none():
    assert substack_feed_url("https://stratechery.com") is None


# --- feed autodiscovery HTML parsing ---

def test_extract_alternate_feed_links_finds_rss():
    html = """
    <html><head>
      <link rel="stylesheet" href="/style.css">
      <link rel="alternate" type="application/rss+xml" title="RSS" href="/feed.xml">
    </head></html>
    """
    links = extract_alternate_feed_links(html, "https://example.com/")
    assert links == ["https://example.com/feed.xml"]


def test_extract_alternate_feed_links_atom_and_attribute_order():
    html = '<link href="/atom.xml" type="application/atom+xml" rel="alternate">'
    links = extract_alternate_feed_links(html, "https://example.com/")
    assert links == ["https://example.com/atom.xml"]


def test_extract_alternate_feed_links_none_present():
    html = "<html><head><title>No feed here</title></head></html>"
    assert extract_alternate_feed_links(html, "https://example.com/") == []


def test_extract_alternate_feed_links_relative_resolved_against_base():
    html = '<link rel="alternate" type="application/rss+xml" href="feed">'
    links = extract_alternate_feed_links(html, "https://example.com/blog/")
    assert links == ["https://example.com/blog/feed"]


# --- YouTube channel ID extraction ---

def test_extract_youtube_channel_id_from_json_blob():
    html = 'blah blah "channelId":"UC1234567890abcdef" more blah'
    assert extract_youtube_channel_id(html) == "UC1234567890abcdef"


def test_extract_youtube_channel_id_from_meta_tag():
    html = '<meta itemprop="channelId" content="UCabcdefghij1234">'
    assert extract_youtube_channel_id(html) == "UCabcdefghij1234"


def test_extract_youtube_channel_id_from_canonical_link():
    # Found necessary via a real fetch: this channel's page had neither the
    # JSON blob nor the itemprop meta tag that jeffersonfisher's page did.
    html = '<link rel="canonical" href="https://www.youtube.com/channel/UCabc123def456gh">'
    assert extract_youtube_channel_id(html) == "UCabc123def456gh"


def test_extract_youtube_channel_id_from_og_url():
    html = '<meta property="og:url" content="https://www.youtube.com/channel/UCzzz999yyy888x">'
    assert extract_youtube_channel_id(html) == "UCzzz999yyy888x"


def test_extract_youtube_channel_id_absent():
    assert extract_youtube_channel_id("<html>nothing here</html>") is None


def test_youtube_feed_url_format():
    assert (
        youtube_feed_url("UCabc123")
        == "https://www.youtube.com/feeds/videos.xml?channel_id=UCabc123"
    )


# --- listing-page link extraction ---

def test_extract_listing_links_skips_nav_and_footer():
    html = """
    <html><body>
      <nav><a href="/about">About Us</a></nav>
      <main>
        <a href="/posts/ai-pricing-models-shift">AI Is Reshaping Pricing Models</a>
        <a href="/x">Hi</a>
      </main>
      <footer><a href="/privacy">Privacy Policy Notice</a></footer>
    </body></html>
    """
    links = extract_listing_links(html, "https://example.com/")
    assert links == [("https://example.com/posts/ai-pricing-models-shift", "AI Is Reshaping Pricing Models")]


def test_extract_listing_links_filters_other_domains():
    html = '<a href="https://other.com/story">A Long Enough Headline Here</a>'
    assert extract_listing_links(html, "https://example.com/") == []


def test_extract_listing_links_dedupes_repeated_hrefs():
    html = """
    <a href="/story">A Long Enough Headline Here</a>
    <a href="/story">A Long Enough Headline Here (again in a teaser block)</a>
    """
    links = extract_listing_links(html, "https://example.com/")
    assert len(links) == 1


def test_extract_listing_links_short_text_excluded():
    html = '<a href="/x">Hi</a><a href="/y">More</a>'
    assert extract_listing_links(html, "https://example.com/") == []


def test_extract_listing_links_drops_boilerplate_phrases():
    # Found via a real A16Z fetch: "Learn More" repeated as the CTA on every
    # single portfolio-company card, not a headline.
    html = """
    <a href="/portfolio/a">Learn More</a>
    <a href="/portfolio/b">Learn More</a>
    <a href="/">Skip to content</a>
    <a href="/signup">Create Account</a>
    <a href="/news/real-story">Real Headline About Something Interesting</a>
    """
    links = extract_listing_links(html, "https://example.com/")
    assert links == [("https://example.com/news/real-story", "Real Headline About Something Interesting")]


def test_extract_listing_links_drops_text_repeated_more_than_twice():
    # Same failure mode as the boilerplate-phrase case but for text that
    # isn't on the fixed denylist -- if it shows up on a page more than
    # twice, it's a recurring label, not three different headlines.
    html = "".join(f'<a href="/item-{i}">Buy Now</a>' for i in range(5))
    html += '<a href="/real">A Perfectly Reasonable Headline Text</a>'
    links = extract_listing_links(html, "https://example.com/")
    assert links == [("https://example.com/real", "A Perfectly Reasonable Headline Text")]


def test_extract_listing_links_allows_text_repeated_twice():
    # Two is still allowed -- avoids being so aggressive that a source with
    # exactly two genuinely-titled stories sharing a coincidental headline
    # gets nuked. Three+ is the actual boilerplate signal.
    html = """
    <a href="/a">A Real Headline Repeated Twice</a>
    <a href="/b">A Real Headline Repeated Twice</a>
    """
    links = extract_listing_links(html, "https://example.com/")
    assert len(links) == 2


# --- filter_recent ---

def test_filter_recent_drops_items_older_than_cutoff():
    now = datetime(2026, 7, 16, tzinfo=timezone.utc)
    old = RawItem(title="Ancient", url="https://x.com/1", source="A", published=now - timedelta(days=400))
    recent = RawItem(title="Fresh", url="https://x.com/2", source="A", published=now - timedelta(days=2))
    kept = filter_recent([old, recent], max_age_days=10, now=now)
    assert kept == [recent]


def test_filter_recent_keeps_items_with_unknown_published_date():
    # Listing-scraped items almost never have a published date -- can't
    # judge their age without fetching the article page, so they pass
    # through here and rely on the seen-store / stage 3 filter instead.
    now = datetime(2026, 7, 16, tzinfo=timezone.utc)
    unknown = RawItem(title="No date", url="https://x.com/3", source="A", published=None)
    kept = filter_recent([unknown], max_age_days=10, now=now)
    assert kept == [unknown]


def test_filter_recent_boundary_is_inclusive():
    now = datetime(2026, 7, 16, tzinfo=timezone.utc)
    exactly_at_cutoff = RawItem(
        title="Boundary", url="https://x.com/4", source="A", published=now - timedelta(days=10)
    )
    kept = filter_recent([exactly_at_cutoff], max_age_days=10, now=now)
    assert kept == [exactly_at_cutoff]


# --- StrategyCache ---

@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    cfg = config_module.Config(
        sources_tsv=tmp_path / "data" / "sources.tsv",
        sources_cache=tmp_path / "cache" / "sources_last_good.tsv",
        sources_stale_after_days=10,
        seen_cache=tmp_path / "cache" / "seen.json",
        title_similarity_threshold=0.90,
        outbox_dir=tmp_path / "outbox",
        fetch_strategy_cache=tmp_path / "cache" / "fetch_strategy.json",
        fetch_strategy_max_age_days=30,
    )
    monkeypatch.setattr(config_module, "CONFIG", cfg)
    monkeypatch.setattr("pipeline.fetch_strategy.CONFIG", cfg)
    return cfg


def test_strategy_cache_round_trips(isolated_config):
    cache = StrategyCache(path=isolated_config.fetch_strategy_cache)
    cache.set("Stratechery", "rss", "https://stratechery.com/feed")
    cache.save()

    reloaded = StrategyCache(path=isolated_config.fetch_strategy_cache)
    entry = reloaded.get("Stratechery")
    assert entry["strategy"] == "rss"
    assert entry["detail"] == "https://stratechery.com/feed"
    assert entry["stale"] is False


def test_strategy_cache_missing_entry_returns_none(isolated_config):
    cache = StrategyCache(path=isolated_config.fetch_strategy_cache)
    assert cache.get("Nonexistent Source") is None


def test_strategy_cache_flags_stale_past_threshold(isolated_config):
    cache = StrategyCache(path=isolated_config.fetch_strategy_cache)
    old = datetime.now(timezone.utc) - timedelta(days=40)
    cache.set("Stratechery", "rss", "https://stratechery.com/feed", now=old)

    entry = cache.get("Stratechery")
    assert entry["stale"] is True


def test_strategy_cache_human_override_never_stale(isolated_config):
    cache = StrategyCache(path=isolated_config.fetch_strategy_cache)
    old = datetime.now(timezone.utc) - timedelta(days=400)
    cache.set("Stratechery", "listing", None, human_override=True, now=old)

    entry = cache.get("Stratechery")
    assert entry["stale"] is False
    assert entry["human_override"] is True
