from __future__ import annotations

import json

import pytest

from datetime import datetime, timezone

from pipeline import config as config_module
from pipeline.dedupe import (
    Candidate,
    SeenStore,
    candidate_from_raw_item,
    canonicalize_url,
    content_hash,
    dedupe,
    title_similarity,
)
from pipeline.fetch_strategy import RawItem


def test_candidate_from_raw_item_carries_all_fields():
    published = datetime(2026, 7, 16, tzinfo=timezone.utc)
    item = RawItem(
        title="A story", url="https://example.com/x", source="Feed A", published=published, excerpt="an excerpt"
    )
    c = candidate_from_raw_item(item)
    assert c.title == "A story"
    assert c.url == "https://example.com/x"
    assert c.source == "Feed A"
    assert c.published == published
    assert c.excerpt == "an excerpt"


def test_canonicalize_url_strips_tracking_params():
    a = canonicalize_url("https://example.com/post?utm_source=x&utm_medium=y&id=1")
    b = canonicalize_url("https://example.com/post?id=1")
    assert a == b


def test_canonicalize_url_normalizes_scheme_www_and_trailing_slash():
    a = canonicalize_url("http://www.example.com/post/")
    b = canonicalize_url("https://example.com/post")
    assert a == b


def test_content_hash_stable_across_tracking_variants():
    c1 = Candidate(title="A story", url="https://example.com/post?utm_source=twitter", source="Feed A")
    c2 = Candidate(title="A story (syndicated)", url="https://www.example.com/post/", source="Feed B")
    assert content_hash(c1) == content_hash(c2)


def test_title_similarity_high_for_near_duplicates():
    assert title_similarity(
        "OpenAI announces new model for enterprise coding",
        "OpenAI Announces New Model For Enterprise Coding",
    ) >= 0.90


def test_title_similarity_low_for_unrelated_titles():
    assert title_similarity("OpenAI announces new model", "Layoffs hit consulting firms") < 0.5


@pytest.fixture
def isolated_seen_store(tmp_path, monkeypatch):
    cfg = config_module.Config(
        sources_tsv=tmp_path / "data" / "sources.tsv",
        sources_cache=tmp_path / "cache" / "sources_last_good.tsv",
        sources_stale_after_days=10,
        seen_cache=tmp_path / "cache" / "seen.json",
        title_similarity_threshold=0.90,
        outbox_dir=tmp_path / "outbox",
    )
    monkeypatch.setattr(config_module, "CONFIG", cfg)
    monkeypatch.setattr("pipeline.dedupe.CONFIG", cfg)
    return cfg


def test_dedupe_drops_items_already_in_seen_store(isolated_seen_store):
    seen = SeenStore(path=isolated_seen_store.seen_cache)
    old = Candidate(title="Old story", url="https://example.com/old", source="Feed A")
    seen.mark_seen(old)
    seen.save()

    seen_reloaded = SeenStore(path=isolated_seen_store.seen_cache)
    new_item = Candidate(title="New story", url="https://example.com/new", source="Feed A")
    repeat_item = Candidate(title="Old story, resurfaced", url="https://example.com/old", source="Feed B")

    new, dropped = dedupe([new_item, repeat_item], seen_reloaded)
    assert new == [new_item]
    assert dropped == [repeat_item]


def test_dedupe_clusters_near_duplicate_titles_within_a_run(isolated_seen_store):
    seen = SeenStore(path=isolated_seen_store.seen_cache)
    a = Candidate(title="Anthropic ships Claude update", url="https://a.example.com/x", source="Feed A")
    b = Candidate(title="Anthropic Ships Claude Update", url="https://b.example.com/y", source="Feed B")
    unrelated = Candidate(title="Consulting pyramid shrinks again", url="https://c.example.com/z", source="Feed C")

    new, dropped = dedupe([a, b, unrelated], seen)
    assert new == [a, unrelated]
    assert dropped == [b]


def test_dedupe_does_not_cluster_recurring_titles_from_the_same_source(isolated_seen_store):
    # Found via a real Exponential View fetch: "Data to start your week" is
    # a recurring section title reused across different weekly editions --
    # different URLs, different content, same title. Clustering these
    # within one source would silently drop real editions, which is a much
    # worse failure than occasionally missing a genuine same-source dupe.
    seen = SeenStore(path=isolated_seen_store.seen_cache)
    ed1 = Candidate(title="Data to start your week", url="https://ev.example.com/ed1", source="Exponential View")
    ed2 = Candidate(title="Data to start your week", url="https://ev.example.com/ed2", source="Exponential View")
    ed3 = Candidate(title="Data to start your week", url="https://ev.example.com/ed3", source="Exponential View")

    new, dropped = dedupe([ed1, ed2, ed3], seen)
    assert new == [ed1, ed2, ed3]
    assert dropped == []


def test_dedupe_never_persists_by_itself(isolated_seen_store):
    seen = SeenStore(path=isolated_seen_store.seen_cache)
    item = Candidate(title="Some story", url="https://example.com/x", source="Feed A")
    dedupe([item], seen)
    assert not isolated_seen_store.seen_cache.exists()


def test_seen_store_round_trips_through_disk(isolated_seen_store):
    seen = SeenStore(path=isolated_seen_store.seen_cache)
    item = Candidate(title="Some story", url="https://example.com/x", source="Feed A")
    seen.mark_seen(item)
    seen.save()

    reloaded = SeenStore(path=isolated_seen_store.seen_cache)
    assert content_hash(item) in reloaded
    data = json.loads(isolated_seen_store.seen_cache.read_text())
    assert len(data) == 1


# --- mark_seen idempotence ---
# The stage-4 commit set includes items dropped BECAUSE they were already
# seen, so mark_seen gets called on entries that already exist.

def test_mark_seen_preserves_original_first_seen(tmp_path):
    store = SeenStore(path=tmp_path / "seen.json")
    c = Candidate(title="Story", url="https://example.com/1", source="Feed")

    store.mark_seen(c)
    first = json.loads(json.dumps(store._data))  # snapshot

    store.mark_seen(Candidate(title="Restyled Title", url="https://example.com/1", source="Other Feed"))

    assert store._data == first, "re-marking must not reset first_seen or overwrite the record"
    assert len(store) == 1
