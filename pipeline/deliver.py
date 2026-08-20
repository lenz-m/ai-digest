"""Stage 4d: the delivery transaction and the degraded-run floor.

Separate from run.py deliberately. The commit-ordering rules ARE the design
decisions of stage 4, and run.py is ~290 lines of untested glue; with send_fn
injected, "send failed -> seen not saved, outbox empty" is a two-line test
with no sockets.

THE ORDERING (and it matters more than the rule it implements):

    1. render                        pure
    2. write note to a staging path  local disk -- proves render + disk + encoding
    3. send email via SMTP           the genuinely fallible step, LAST
    4. os.replace() staging -> outbox atomic rename
    5. seen.save()

Writing to disk BEFORE the send means a render or encoding fault aborts the
run before an email is spent and before any candidate is burned; next week
retries cleanly. Steps 4 and 5 are past the point of no return -- once SMTP
accepts, the transaction has happened and nothing after it may abort the
commit.

WHY SMTP ACCEPTANCE IS THE COMMIT POINT, not "send and outbox write both
succeeded": if the outbox write is part of the transaction, then an outbox
failure leaves the seen-set uncommitted and next Monday re-sends LAST WEEK'S
stories as a fresh digest. A missing archive note is a gap in a secondary
channel (recoverable by hand from the log, which is why the failure path logs
the full note); a duplicate digest is the product failing at the one
guarantee it makes. Email is the primary channel, so the commit point
follows it.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pipeline.config import CONFIG
from pipeline.dedupe import Candidate, SeenStore
from pipeline.email_build import build_digest_message
from pipeline.render import (
    render_email_html,
    render_email_text,
    render_vault_note,
    vault_note_filename,
)
from pipeline.score_stage import ScoreOutcome
from pipeline.select import Selection

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeliveryResult:
    sent: bool
    committed: bool
    reason: str = ""
    note_path: Path | None = None
    marked_seen: int = 0
    # Whether seen.save() actually wrote to disk. Distinct from `committed`:
    # the transaction can reach its commit point and still not persist, when
    # CONFIG.commit_seen is off. See _commit_seen.
    seen_persisted: bool = False
    preview_paths: tuple[Path, ...] = ()


def scoring_is_degraded(outcome: ScoreOutcome) -> tuple[bool, str]:
    """Did the scoring MACHINERY work? -- not "was the news interesting".

    These need opposite handling and are today indistinguishable: a fully
    failed scoring stage produces an empty Selection that is byte-identical
    to a genuinely thin week. A thin week must commit (those items WERE
    judged and found wanting); a degraded run must not (they were never
    evaluated, and committing would bury them permanently on the basis of a
    crash).

    The 0.30 ceiling comes from the two observed regimes, not from taste: a
    healthy run fails ~0% and the one real breakage was 22/27 = 81%. The
    min_sample guard stops a small --limit-sources run tripping on one odd
    response.

    Note what is NOT here: a minimum selected-item count. A week with 2 org
    and 1 fluency item that all scored cleanly is a real thin week, and the
    digest already says so. An item-count floor measures the news; the
    failure rate measures the pipeline. Only the second should gate delivery.
    """
    n = outcome.attempted
    if n == 0:
        return False, ""
    failed = len(outcome.failures)
    if failed == n:
        return True, f"all {n} score request(s) failed"
    if n >= CONFIG.score_failure_min_sample:
        rate = failed / n
        if rate > CONFIG.score_failure_rate_ceiling:
            return True, f"{failed}/{n} score requests failed ({rate:.0%})"
    return False, ""


def _commit_seen(seen: SeenStore, candidates: list[Candidate], commit: bool) -> tuple[int, bool]:
    """Mark the commit set, then persist -- but only if `commit`.

    The seen-set ships NOT PERSISTING, and that is a deliberate operational
    gate rather than an unfinished feature: the full transaction above runs
    normally, and only this final write is skipped. Two known defects
    currently discard content silently (score-stage failures at 15-23% per
    run, and the max_survivors cap until the round-robin fix has had a UAT
    pass). While the seen-set is never persisted, both merely defer an item
    to next week; the moment it persists, both become permanent deletion.
    Ship the email first, fix those, then turn this on.

    Marking still happens in memory so the count is real and the code path is
    exercised -- what is skipped is exactly one write.
    """
    for c in candidates:
        seen.mark_seen(c)
    marked = len(candidates)

    if not commit:
        logger.warning(
            "seen-set NOT persisted: %d item(s) were marked in memory and will be "
            "discarded when this process exits. commit_seen is off "
            "(AI_DIGEST_COMMIT_SEEN / --commit-seen). This is deliberate -- "
            "score-stage failures and the max_survivors cap still drop content, "
            "and persisting would turn those from 'returns next week' into "
            "permanent deletion. Every item in this run will be re-ingested next run.",
            marked,
        )
        return marked, False

    seen.save()
    logger.info("seen-set committed: %d item(s) marked, store now holds %d", marked, len(seen))
    return marked, True


def _write_previews(selection: Selection, generated_at: datetime) -> DeliveryResult:
    """Dry-run artifacts go to preview_dir, NEVER outbox_dir.

    Stage 5's Mac-side job rsyncs outbox/ into the vault, so a preview left
    there would be archived as though it had actually been delivered. Keeping
    them apart also makes the dry-run/apply distinction physically visible: a
    file in outbox/ means something was sent.

    Fixed filenames, not timestamped, so repeated dry runs overwrite instead
    of accumulating.
    """
    CONFIG.preview_dir.mkdir(parents=True, exist_ok=True)
    html_path = CONFIG.preview_dir / "digest-preview.html"
    md_path = CONFIG.preview_dir / "digest-preview.md"
    txt_path = CONFIG.preview_dir / "digest-preview.txt"

    html_path.write_text(render_email_html(selection, generated_at), encoding="utf-8")
    md_path.write_text(render_vault_note(selection, generated_at), encoding="utf-8")
    txt_path.write_text(render_email_text(selection, generated_at), encoding="utf-8")

    return DeliveryResult(
        sent=False, committed=False, reason="dry run",
        preview_paths=(html_path, md_path, txt_path),
    )


def deliver(
    selection: Selection,
    generated_at: datetime,
    *,
    send_fn,
    seen: SeenStore,
    mark_seen_candidates: list[Candidate],
    score_outcome: ScoreOutcome,
    apply: bool,
    to_addrs: list[str] | None = None,
    from_addr: str = "",
    commit_seen: bool | None = None,
) -> DeliveryResult:
    if commit_seen is None:
        commit_seen = CONFIG.commit_seen

    if not apply:
        return _write_previews(selection, generated_at)

    # --- GATE 1: did the machinery work? ---
    # MUST precede the empty-selection branch below. A fully failed scoring
    # stage yields an empty Selection indistinguishable from a thin week, and
    # committing it would bury ~450 never-evaluated candidates with no symptom
    # but a missing email. If these two branches are ever reordered, the test
    # named test_floor_is_checked_before_the_empty_selection_branch catches it.
    degraded, why = scoring_is_degraded(score_outcome)
    if degraded:
        logger.error(
            "REFUSING to send or commit -- scoring stage degraded: %s. "
            "Nothing marked seen; these items are re-ingested next run.", why
        )
        return DeliveryResult(sent=False, committed=False, reason=f"scoring stage degraded: {why}")

    # --- GATE 2: a genuinely thin week ---
    # Machinery fine, nothing cleared the bar. Commit: these items WERE
    # judged, and re-judging them next week is exactly the waste the seen-set
    # exists to prevent.
    if not selection.for_org and not selection.for_you:
        marked, persisted = _commit_seen(seen, mark_seen_candidates, commit_seen)
        logger.info("nothing cleared the bar this week -- no email sent, items marked as judged")
        return DeliveryResult(
            sent=False, committed=True, reason="nothing cleared the bar",
            marked_seen=marked, seen_persisted=persisted,
        )

    # --- 1/2: render, then stage the note on local disk BEFORE sending ---
    dest = CONFIG.outbox_dir / "Digests" / vault_note_filename(generated_at)
    # .partial stays in the same directory so os.replace is same-filesystem
    # and therefore genuinely atomic.
    staged = dest.with_suffix(dest.suffix + ".partial")
    staged.parent.mkdir(parents=True, exist_ok=True)
    note = render_vault_note(selection, generated_at)
    staged.write_text(note, encoding="utf-8")

    # --- 3: the fallible step, last ---
    message = build_digest_message(
        selection, generated_at,
        to_addrs=to_addrs if to_addrs is not None else [],
        from_addr=from_addr,
    )
    try:
        send_fn(message)
    except Exception:
        # Undelivered -> leave nothing behind in the directory stage 5 sweeps,
        # or the vault gets an archive note for a digest nobody received, then
        # a second overlapping note next week.
        staged.unlink(missing_ok=True)
        logger.error("send failed -- staged note rolled back, seen-set NOT committed. "
                     "Not committing IS the retry: next run re-ingests these items normally.")
        raise

    # ================= PAST THE POINT OF NO RETURN =================
    # The email is out. The email is the transaction, so nothing below may
    # abort the commit -- failing to save here means next Monday re-sends this
    # week's stories, which is the worse of the two failures.
    try:
        os.replace(staged, dest)
        note_path = dest
    except OSError as exc:
        # Plausible rather than paranoid: stage 5's Mac-side job CLEARS the
        # Pi's outbox, making the Mac a concurrent actor on this exact
        # directory. A clear landing between the staging write and this
        # rename yields ENOENT on a real schedule.
        note_path = None
        logger.error(
            "email SENT but the outbox note could not be placed at %s: %s. "
            "Committing the seen-set anyway -- the email is the transaction. "
            "Full note content follows so the archive entry can be recovered "
            "by hand:\n%s", dest, exc, note,
        )

    marked, persisted = _commit_seen(seen, mark_seen_candidates, commit_seen)
    return DeliveryResult(
        sent=True, committed=True, note_path=note_path,
        marked_seen=marked, seen_persisted=persisted,
    )
