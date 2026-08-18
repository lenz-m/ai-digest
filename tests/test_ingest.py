from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pipeline import config as config_module
from pipeline.ingest import IngestError, IngestResult, Source, _parse_tsv, load_sources


def test_parse_tsv_basic():
    text = "title\tnotes\nExample Blog\thttps://example.com/feed\n"
    sources = _parse_tsv(text)
    assert sources == [Source(name="Example Blog", url="https://example.com/feed", extra="")]


def test_parse_tsv_extracts_url_from_notes_with_extra_text():
    text = "title\tnotes\nExample Blog\thttps://example.com/feed some extra tag text\n"
    sources = _parse_tsv(text)
    assert sources[0].url == "https://example.com/feed"
    assert sources[0].extra == "some extra tag text"


def test_parse_tsv_skips_row_with_no_url():
    text = "title\tnotes\nNo URL Here\tjust some notes, no link\nGood One\thttps://good.example.com\n"
    sources = _parse_tsv(text)
    assert [s.name for s in sources] == ["Good One"]


def test_parse_tsv_skips_row_with_empty_title():
    text = "title\tnotes\n\thttps://good.example.com\nGood One\thttps://good.example.com\n"
    sources = _parse_tsv(text)
    assert [s.name for s in sources] == ["Good One"]


def test_parse_tsv_rejects_bad_header():
    with pytest.raises(IngestError):
        _parse_tsv("name\turl\nfoo\thttps://example.com\n")


def test_parse_tsv_strips_trailing_punctuation_from_url():
    text = "title\tnotes\nBlog\thttps://example.com/post).\n"
    sources = _parse_tsv(text)
    assert sources[0].url == "https://example.com/post"


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Point CONFIG at a scratch directory for the duration of a test."""
    cfg = config_module.Config(
        sources_tsv=tmp_path / "data" / "sources.tsv",
        sources_cache=tmp_path / "cache" / "sources_last_good.tsv",
        manual_sources_tsv=tmp_path / "data" / "manual_sources.tsv",
        sources_stale_after_days=10,
        seen_cache=tmp_path / "cache" / "seen.json",
        title_similarity_threshold=0.90,
        outbox_dir=tmp_path / "outbox",
    )
    monkeypatch.setattr(config_module, "CONFIG", cfg)
    monkeypatch.setattr("pipeline.ingest.CONFIG", cfg)
    return cfg


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_load_sources_from_live_file_updates_cache(isolated_config):
    _write(isolated_config.sources_tsv, "title\tnotes\nBlog\thttps://example.com\n")
    result = load_sources()
    assert result.from_cache is False
    assert len(result.sources) == 1
    assert isolated_config.sources_cache.exists()


def test_load_sources_falls_back_to_cache_when_live_missing(isolated_config):
    _write(isolated_config.sources_cache, "title\tnotes\nCached Blog\thttps://cached.example.com\n")
    result = load_sources()
    assert result.from_cache is True
    assert result.stale is False
    assert [s.name for s in result.sources] == ["Cached Blog"]


def test_manual_sources_merged_and_independent_of_reminders(isolated_config):
    _write(isolated_config.sources_tsv, "title\tnotes\nReminders Blog\thttps://a.example.com\n")
    _write(isolated_config.manual_sources_tsv, "title\tnotes\nHBR\thttps://feeds.harvardbusiness.org/harvardbusiness\n")
    result = load_sources()
    names = [s.name for s in result.sources]
    assert "Reminders Blog" in names and "HBR" in names
    hbr = next(s for s in result.sources if s.name == "HBR")
    assert hbr.url == "https://feeds.harvardbusiness.org/harvardbusiness"


def test_manual_sources_load_even_when_reminders_only_in_cache(isolated_config):
    # manual sources are independent -- they show up even on the cache-fallback path
    _write(isolated_config.sources_cache, "title\tnotes\nCached Blog\thttps://cached.example.com\n")
    _write(isolated_config.manual_sources_tsv, "title\tnotes\nHBR\thttps://feeds.harvardbusiness.org/harvardbusiness\n")
    result = load_sources()
    names = [s.name for s in result.sources]
    assert names == ["Cached Blog", "HBR"]


def test_manual_entry_wins_on_name_collision(isolated_config):
    # The manual file is the user's override layer: a same-named manual entry
    # replaces the friend's Reminders entry (e.g. a real feed URL replacing a
    # bare-URL scrape entry), and does so in place (no duplicate).
    _write(isolated_config.sources_tsv, "title\tnotes\nGCP\thttps://cloud.google.com/blog\n")
    _write(isolated_config.manual_sources_tsv, "title\tnotes\nGCP\thttps://cloudblog.withgoogle.com/rss/\n")
    result = load_sources()
    gcp = [s for s in result.sources if s.name == "GCP"]
    assert len(gcp) == 1
    assert gcp[0].url == "https://cloudblog.withgoogle.com/rss/"


def test_load_sources_falls_back_when_live_unparsable(isolated_config):
    _write(isolated_config.sources_tsv, "not a valid tsv at all")
    _write(isolated_config.sources_cache, "title\tnotes\nCached Blog\thttps://cached.example.com\n")
    result = load_sources()
    assert result.from_cache is True


def test_load_sources_marks_stale_past_threshold(isolated_config):
    _write(isolated_config.sources_cache, "title\tnotes\nCached Blog\thttps://cached.example.com\n")
    far_future = datetime.now(timezone.utc) + timedelta(days=20)
    result = load_sources(now=far_future)
    assert result.from_cache is True
    assert result.stale is True
    assert result.cache_age_days >= 10


def test_load_sources_raises_when_nothing_available(isolated_config):
    with pytest.raises(IngestError):
        load_sources()
