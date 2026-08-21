"""The replay cache: round-trip fidelity, refusal to half-read, staleness.

The load-bearing test here is the counts round-trip. Replaying without the
four Selection counts renders a digest that looks HEALTHY when the original
run was not -- it silently deletes the footer's operator diagnostics, which
are the only symptom the pipeline has for score-stage failures and the
max_survivors cap.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from pipeline import config as config_module
from pipeline import replay as replay_module
from pipeline.dedupe import Candidate
from pipeline.render import render_email_html
from pipeline.replay import (
    CACHE_VERSION,
    ReplayError,
    ReplayPayload,
    RunCounts,
    load_scored_run,
    provenance_lines,
    save_quietly,
    save_scored_run,
)
from pipeline.score_stage import ScoredItem
from pipeline.select import select

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def cache_path(tmp_path, monkeypatch):
    """Point scored_cache at a tmp dir. CONFIG is frozen, so this swaps the
    whole object the way tests/test_deliver.py does."""
    path = tmp_path / "cache" / "last_run_scored.json"
    monkeypatch.setattr(replay_module, "CONFIG", config_module.Config(scored_cache=path))
    return path


def _item(title="A story", org=70, flu=20, url=None, published=None):
    return ScoredItem(
        candidate=Candidate(
            title=title,
            url=url or f"https://example.com/{title.replace(' ', '-')}",
            source="Stratechery",
            published=published,
            excerpt="An excerpt.",
        ),
        org_score=org, org_reason="Strategy signal.",
        fluency_score=flu, fluency_reason="Practitioner depth.",
        summary="Sentence one. Sentence two.", so_what="Matters for pricing.",
        vendor_marketing=False, clean_title=f"Clean {title}",
        trust_tier="independent_analysis",
    )


def _counts():
    return RunCounts(
        filtered_out_count=129, scoring_failed_count=9,
        score_attempted_count=60, filter_passed_count=312,
    )


# --- round trip ---

def test_every_scored_item_field_survives_the_round_trip(cache_path):
    original = _item(published=datetime(2026, 8, 19, 9, 30, tzinfo=timezone.utc))
    save_scored_run([original], _counts(), path=cache_path)
    [restored] = load_scored_run(cache_path).scored

    assert restored == original  # frozen dataclass -- field-by-field equality
    # The derived properties are the ones render.py actually reads.
    assert restored.title == original.title == "Clean A story"
    assert restored.raw_title == "A story"
    assert restored.url == original.url
    assert restored.source == "Stratechery"
    assert restored.candidate.published == original.candidate.published


def test_counts_survive_the_round_trip(cache_path):
    """Without these the replay renders a falsely healthy week -- the exact
    defect the footer diagnostics exist to surface."""
    save_scored_run([_item()], _counts(), path=cache_path)
    assert load_scored_run(cache_path).counts == _counts()


def test_replayed_digest_reports_the_same_losses_as_the_live_run(cache_path):
    """End to end, at the level that matters: cache -> select -> render still
    carries '9 of 60 could not be scored' and the max_survivors line."""
    items = [_item(f"Story {n}", org=90 - n) for n in range(6)]
    save_scored_run(items, _counts(), path=cache_path)

    payload = load_scored_run(cache_path)
    html = render_email_html(select(payload.scored, **payload.counts.as_kwargs()), NOW)

    assert "9 of 60 items could not be scored" in html
    assert "60 of 312 items that passed the filter were scored" in html
    assert "129 more filtered below the cut" in html


def test_replay_reproduces_the_live_selection_exactly(cache_path):
    """select() is re-run rather than stored, so this has to be checked, not
    assumed: same items + same config must give the same digest."""
    items = [_item(f"Story {n}", org=n * 7 % 100, flu=n * 13 % 100) for n in range(20)]
    live = select(items, **_counts().as_kwargs())

    save_scored_run(items, _counts(), path=cache_path)
    payload = load_scored_run(cache_path)
    replayed = select(payload.scored, **payload.counts.as_kwargs())

    assert [i.url for i in replayed.for_org] == [i.url for i in live.for_org]
    assert [i.url for i in replayed.for_you] == [i.url for i in live.for_you]
    assert render_email_html(replayed, NOW) == render_email_html(live, NOW)


def test_provenance_records_whether_the_run_actually_sent(cache_path):
    """An --apply run is the one most worth re-examining, so it must be
    distinguishable from a dry run in the cache."""
    save_scored_run([_item()], _counts(), path=cache_path, applied=True, log_path="logs/x.log")
    payload = load_scored_run(cache_path)
    assert payload.applied is True
    assert payload.log_path == "logs/x.log"
    assert "email was sent" in "\n".join(provenance_lines(payload, NOW))


# --- refusing to half-read ---

def test_unrecognised_version_is_refused_not_partially_read(cache_path):
    """ScoredItem has gained fields twice. Defaulting a missing one would
    render a subtly wrong digest with no symptom."""
    save_scored_run([_item()], _counts(), path=cache_path)
    raw = json.loads(cache_path.read_text())
    raw["version"] = CACHE_VERSION + 1
    cache_path.write_text(json.dumps(raw))

    with pytest.raises(ReplayError, match="version"):
        load_scored_run(cache_path)


def test_missing_cache_names_the_path_and_says_how_to_make_one(cache_path):
    with pytest.raises(ReplayError) as e:
        load_scored_run(cache_path)
    assert str(cache_path) in str(e.value)
    assert "run the pipeline once" in str(e.value)


def test_corrupt_json_is_a_replay_error_not_a_traceback(cache_path):
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text("{not json")
    with pytest.raises(ReplayError):
        load_scored_run(cache_path)


def test_right_version_but_wrong_shape_is_refused(cache_path):
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(json.dumps({
        "version": CACHE_VERSION, "run_at": NOW.isoformat(),
        "counts": {}, "scored": [{"candidate": {"title": "t"}}],  # missing fields
    }))
    with pytest.raises(ReplayError, match="does not match that shape"):
        load_scored_run(cache_path)


# --- a cache failure must never break a run ---

def test_save_quietly_swallows_failures(cache_path, monkeypatch, caplog):
    """It is written moments before an --apply run sends. Losing the cache
    costs one re-run; letting it abort the send costs the week."""
    def boom(*a, **kw):
        raise OSError("no space left on device")

    monkeypatch.setattr("pipeline.replay.save_scored_run", boom)
    with caplog.at_level("WARNING"):
        save_quietly([_item()], _counts())  # must not raise

    assert "could not write the scored-items cache" in caplog.text
    assert "The run is unaffected" in caplog.text


# --- staleness ---

def _payload(age_days: int, applied=False):
    return ReplayPayload(
        scored=[_item()], counts=_counts(),
        run_at=NOW - timedelta(days=age_days), applied=applied,
    )


def test_a_recent_replay_states_its_age_without_shouting():
    out = "\n".join(provenance_lines(_payload(2), NOW))
    assert "2d ago" in out
    assert "WARNING" not in out


def test_a_replay_older_than_the_threshold_warns_loudly():
    """The output is shaped identically to a fresh run -- nothing in the digest
    itself says the news is a fortnight old."""
    out = "\n".join(provenance_lines(_payload(14), NOW))
    assert "WARNING" in out
    assert "14 DAYS OLD" in out
    assert "OLD news" in out


def test_staleness_threshold_is_seven_days():
    assert _payload(7).is_stale(NOW) is False
    assert _payload(8).is_stale(NOW) is True
