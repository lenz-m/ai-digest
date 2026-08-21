"""The delivery transaction: the floor, the ordering, and the commit rules.

These are the tests that matter most in stage 4 -- everything here encodes a
decision that is invisible in the code's shape and easy for a later refactor
to reverse without anything else failing.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from pipeline import config as config_module
from pipeline import deliver as deliver_module
from pipeline.cost import CostTracker
from pipeline.dedupe import Candidate, SeenStore, content_hash
from pipeline.render import ORG_HEADER
from pipeline.deliver import deliver, scoring_is_degraded
from pipeline.score_stage import ScoredItem, ScoreFailure, ScoreOutcome
from pipeline.select import Selection

NOW = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)
NOTE_NAME = "🗞️ AI Digest 2026-08-24.md"


# --------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------

@pytest.fixture
def dirs(tmp_path, monkeypatch):
    """Point outbox_dir and preview_dir at a tmp dir for every test here."""
    cfg = config_module.Config(
        outbox_dir=tmp_path / "outbox",
        preview_dir=tmp_path / "preview",
        seen_cache=tmp_path / "seen.json",
    )
    monkeypatch.setattr(deliver_module, "CONFIG", cfg)
    return cfg


@pytest.fixture
def seen(dirs):
    return SeenStore(path=dirs.seen_cache)


def _cand(name="Story", url=None):
    return Candidate(title=name, url=url or f"https://example.com/{name}", source="Feed")


def _item(title="Org story", url="https://example.com/o"):
    return ScoredItem(
        candidate=Candidate(title=title, url=url, source="Stratechery"),
        org_score=82, org_reason="Strong strategy signal.",
        fluency_score=40, fluency_reason="Some depth.",
        summary="Sentence one. Sentence two.", so_what="Matters for pricing.",
        vendor_marketing=False, clean_title=title, trust_tier="independent_analysis",
    )


def _sel(**kw):
    base = dict(for_org=[_item()], for_you=[], considered_and_skipped=[])
    base.update(kw)
    return Selection(**base)


def _outcome(scored=1, failed=0):
    return ScoreOutcome(
        scored=[_item(f"scored{i}", f"https://example.com/s{i}") for i in range(scored)],
        failures=[ScoreFailure(candidate=_cand(f"failed{i}"), reason="unparseable") for i in range(failed)],
    )


class Recorder:
    """Stands in for send.send_message."""

    def __init__(self, raises=None, on_call=None):
        self.calls = []
        self._raises = raises
        self._on_call = on_call

    def __call__(self, msg):
        self.calls.append(msg)
        if self._on_call:
            self._on_call(msg)
        if self._raises:
            raise self._raises


def _deliver(selection, *, seen, send_fn, outcome=None, mark=None, apply=True, commit_seen=True):
    return deliver(
        selection, NOW,
        send_fn=send_fn, seen=seen,
        mark_seen_candidates=mark if mark is not None else [],
        score_outcome=outcome if outcome is not None else _outcome(scored=10),
        apply=apply, to_addrs=["me@icloud.com"], from_addr="me@icloud.com",
        commit_seen=commit_seen,
    )


# --------------------------------------------------------------------------
# The degraded-run floor
# --------------------------------------------------------------------------

def test_floor_all_failed_is_degraded():
    degraded, why = scoring_is_degraded(_outcome(scored=0, failed=27))
    assert degraded
    assert "27" in why


def test_floor_22_of_27_is_degraded():
    """The one real observed breakage (81%)."""
    degraded, _ = scoring_is_degraded(_outcome(scored=5, failed=22))
    assert degraded


def test_floor_1_of_27_is_not_degraded():
    degraded, _ = scoring_is_degraded(_outcome(scored=26, failed=1))
    assert not degraded


def test_floor_ignores_small_samples():
    """2/4 is 50% but only four requests -- a --limit-sources run must not
    trip the floor on one odd response."""
    degraded, _ = scoring_is_degraded(_outcome(scored=2, failed=2))
    assert not degraded


def test_floor_measured_steady_state_does_not_trip_it():
    """15% and 23% are what the pipeline currently does every week. The floor
    is a catastrophe detector, not a drift detector -- those weeks must still
    send. (Their loss is reported in the email footer instead.)"""
    assert not scoring_is_degraded(_outcome(scored=51, failed=9))[0]   # 15%
    assert not scoring_is_degraded(_outcome(scored=46, failed=14))[0]  # 23%


def test_floor_with_nothing_attempted_is_not_degraded():
    assert scoring_is_degraded(ScoreOutcome(scored=[], failures=[])) == (False, "")


def test_degraded_run_neither_sends_nor_commits_nor_marks(dirs, seen):
    send = Recorder()
    result = _deliver(_sel(), seen=seen, send_fn=send, outcome=_outcome(scored=0, failed=27),
                      mark=[_cand("a"), _cand("b")])

    assert result.sent is False and result.committed is False
    assert "degraded" in result.reason
    assert send.calls == []
    assert result.marked_seen == 0
    assert len(seen) == 0
    assert not dirs.seen_cache.exists(), "seen.save() must never have run"


def test_floor_is_checked_before_the_empty_selection_branch(dirs, seen):
    """A fully failed scoring stage produces an empty Selection that is
    byte-identical to a genuinely thin week. If the empty branch is checked
    first, a 27/27 failure silently consumes ~450 candidates and sends
    nothing -- symptom: a missing email, no other trace. This assertion is
    the only thing standing between those two branches."""
    send = Recorder()
    result = _deliver(
        Selection(for_org=[], for_you=[], considered_and_skipped=[]),
        seen=seen, send_fn=send,
        outcome=_outcome(scored=0, failed=27),
        mark=[_cand("a")],
    )
    assert result.committed is False, "degraded must win over 'thin week'"
    assert len(seen) == 0


# --------------------------------------------------------------------------
# The transaction
# --------------------------------------------------------------------------

def test_dry_run_writes_previews_and_touches_nothing_else(dirs, seen):
    send = Recorder()
    result = _deliver(_sel(), seen=seen, send_fn=send, apply=False, mark=[_cand("a")])

    assert (dirs.preview_dir / "digest-preview.html").exists()
    assert (dirs.preview_dir / "digest-preview.md").exists()
    assert not dirs.outbox_dir.exists(), "a dry run must leave nothing in the directory stage 5 sweeps"
    assert send.calls == []
    assert result.sent is False and result.committed is False
    assert not dirs.seen_cache.exists()


def test_dry_run_preview_filenames_are_fixed_so_reruns_overwrite(dirs, seen):
    _deliver(_sel(), seen=seen, send_fn=Recorder(), apply=False)
    _deliver(_sel(), seen=seen, send_fn=Recorder(), apply=False)
    assert sorted(p.name for p in dirs.preview_dir.iterdir()) == [
        "digest-preview.html", "digest-preview.md", "digest-preview.txt",
    ]


def test_apply_success_places_the_note_and_leaves_no_partial(dirs, seen):
    send = Recorder()
    result = _deliver(_sel(), seen=seen, send_fn=send, mark=[_cand("a"), _cand("b")])

    note = dirs.outbox_dir / "Digests" / NOTE_NAME
    assert note.exists()
    assert "type: ai-digest" in note.read_text(encoding="utf-8")
    assert list((dirs.outbox_dir / "Digests").glob("*.partial")) == []
    assert result.sent is True and result.committed is True
    assert result.note_path == note
    assert len(send.calls) == 1


def test_staged_note_exists_at_the_moment_send_is_invoked(dirs, seen):
    """Q1's ordering as an executable assertion, checked from INSIDE the fake:
    the note must already be on disk when the send fires, so a render or
    encoding fault aborts before an email is spent. This is the property a
    refactor is most likely to break, and nothing else would notice."""
    observed = {}

    def inspect(msg):
        staged = dirs.outbox_dir / "Digests" / (NOTE_NAME + ".partial")
        observed["staged_exists"] = staged.exists()
        observed["dest_exists"] = (dirs.outbox_dir / "Digests" / NOTE_NAME).exists()

    _deliver(_sel(), seen=seen, send_fn=Recorder(on_call=inspect))

    assert observed["staged_exists"] is True, "the note must be written BEFORE the send"
    assert observed["dest_exists"] is False, "and only renamed into place AFTER it"


def test_send_failure_rolls_back_and_commits_nothing(dirs, seen):
    send = Recorder(raises=RuntimeError("smtp exploded"))
    with pytest.raises(RuntimeError):
        _deliver(_sel(), seen=seen, send_fn=send, mark=[_cand("a")])

    digests = dirs.outbox_dir / "Digests"
    assert list(digests.iterdir()) == [], "an undelivered run must leave nothing behind"
    assert len(seen) == 0
    assert not dirs.seen_cache.exists(), "not committing IS the retry mechanism"


def test_replace_failure_after_a_successful_send_still_commits(dirs, seen, monkeypatch, caplog):
    """The email is already out, so skipping the commit here would re-send
    this week's stories next Monday -- the worse of the two failures. And this
    is plausible rather than paranoid: stage 5's Mac-side job CLEARS the Pi's
    outbox, so it is a concurrent actor on this exact directory."""
    def boom(src, dst):
        raise OSError("ENOENT: outbox cleared under us")

    monkeypatch.setattr(deliver_module.os, "replace", boom)

    send = Recorder()
    with caplog.at_level("ERROR"):
        result = _deliver(_sel(), seen=seen, send_fn=send, mark=[_cand("a")])

    assert result.sent is True
    assert result.committed is True
    assert result.note_path is None
    assert len(seen) == 1
    assert dirs.seen_cache.exists()
    # "recoverable by hand from the log" has to actually be true
    assert "type: ai-digest" in caplog.text
    assert ORG_HEADER in caplog.text


def test_thin_week_sends_nothing_but_still_commits(dirs, seen):
    """Machinery healthy, nothing cleared the bar. These items WERE judged,
    so re-judging them next week is exactly the waste the seen-set prevents.
    Contrast with the degraded case above -- same empty Selection, opposite
    handling, which is the entire point of the floor."""
    send = Recorder()
    result = _deliver(
        Selection(for_org=[], for_you=[], considered_and_skipped=[]),
        seen=seen, send_fn=send, outcome=_outcome(scored=30, failed=0),
        mark=[_cand("a"), _cand("b")],
    )
    assert result.sent is False
    assert result.committed is True
    assert send.calls == []
    assert len(seen) == 2
    assert dirs.seen_cache.exists()


# --------------------------------------------------------------------------
# The mark-seen set
# --------------------------------------------------------------------------

def test_marks_exactly_the_candidates_it_was_given(dirs, seen):
    rejected = _cand("filter-rejected")
    scored = _cand("scored")
    dupe = _cand("fuzzy-dupe")
    never_scored = _cand("never-scored")

    _deliver(_sel(), seen=seen, send_fn=Recorder(), mark=[dupe, rejected, scored])

    assert content_hash(dupe) in seen
    assert content_hash(rejected) in seen
    assert content_hash(scored) in seen
    assert content_hash(never_scored) not in seen, (
        "an item the filter passed that never produced a score was never judged -- "
        "marking it would permanently bury content on the basis of a parse bug"
    )


def test_remarking_an_already_seen_item_preserves_first_seen(dirs, seen):
    c = _cand("recurring")
    seen.mark_seen(c)
    original = dict(seen._data[content_hash(c)])

    _deliver(_sel(), seen=seen, send_fn=Recorder(), mark=[c])

    assert seen._data[content_hash(c)] == original
    assert len(seen) == 1


# --------------------------------------------------------------------------
# commit_seen gate
# --------------------------------------------------------------------------

def test_commit_seen_off_runs_the_whole_transaction_but_skips_the_persist(dirs, seen, caplog):
    """The gate is operational, not structural: ordering, rollback, the floor
    and the mark set all behave normally -- exactly one write is skipped."""
    send = Recorder()
    with caplog.at_level("WARNING"):
        result = _deliver(_sel(), seen=seen, send_fn=send, mark=[_cand("a"), _cand("b")],
                          commit_seen=False)

    assert result.sent is True
    assert result.committed is True
    assert result.marked_seen == 2, "marking still happens, in memory"
    assert result.seen_persisted is False
    assert not dirs.seen_cache.exists(), "nothing may reach disk"
    assert (dirs.outbox_dir / "Digests" / NOTE_NAME).exists(), "everything else runs normally"
    assert "NOT persisted" in caplog.text
    assert "AI_DIGEST_COMMIT_SEEN" in caplog.text, "the log must say how to turn it on"


def test_commit_seen_on_persists_to_disk(dirs, seen):
    result = _deliver(_sel(), seen=seen, send_fn=Recorder(), mark=[_cand("a")], commit_seen=True)
    assert result.seen_persisted is True
    assert dirs.seen_cache.exists()

    reloaded = SeenStore(path=dirs.seen_cache)
    assert content_hash(_cand("a")) in reloaded


def test_commit_seen_off_also_skips_the_persist_on_a_thin_week(dirs, seen):
    result = _deliver(
        Selection(for_org=[], for_you=[], considered_and_skipped=[]),
        seen=seen, send_fn=Recorder(), outcome=_outcome(scored=30), mark=[_cand("a")],
        commit_seen=False,
    )
    assert result.committed is True and result.seen_persisted is False
    assert not dirs.seen_cache.exists()


def test_commit_seen_defaults_to_the_config_value_which_ships_off(dirs, seen, monkeypatch):
    monkeypatch.setattr(deliver_module, "CONFIG", config_module.Config(
        outbox_dir=dirs.outbox_dir, preview_dir=dirs.preview_dir, seen_cache=dirs.seen_cache,
    ))
    assert deliver_module.CONFIG.commit_seen is False, "must ship disabled"

    result = deliver(
        _sel(), NOW, send_fn=Recorder(), seen=seen, mark_seen_candidates=[_cand("a")],
        score_outcome=_outcome(scored=10), apply=True,
        to_addrs=["me@icloud.com"], from_addr="me@icloud.com",
        # commit_seen deliberately not passed
    )
    assert result.seen_persisted is False


# --------------------------------------------------------------------------
# Cost: delivery must never re-enter an LLM stage
# --------------------------------------------------------------------------

def test_delivery_with_retries_makes_zero_llm_calls(dirs, seen, monkeypatch):
    """Stage-4 analogue of test_n_candidates_never_produce_n_requests. All API
    spend has happened by the time deliver() runs; the SMTP retry wraps the
    transaction only and must never re-enter render, let alone a scoring pass."""
    import smtplib

    from pipeline import send as send_module
    from pipeline.send import send_message

    monkeypatch.setenv("SMTP_USERNAME", "me@icloud.com")
    monkeypatch.setenv("SMTP_APP_PASSWORD", "pw")

    tracker = CostTracker()
    tracker.record(model="claude-sonnet-5", input_tokens=1000, output_tokens=500)
    before = len(tracker.records)

    attempts = {"n": 0}

    class FlakySMTP:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, u, p):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise smtplib.SMTPServerDisconnected("reset")

        def send_message(self, msg):
            pass

    def send_fn(msg):
        send_message(msg, smtp_factory=FlakySMTP, sleep=lambda d: None)

    result = _deliver(_sel(), seen=seen, send_fn=send_fn, mark=[_cand("a")])

    assert result.sent is True
    assert attempts["n"] == 3, "the retry did run"
    assert len(tracker.records) == before, "delivery must not add a single API call"
