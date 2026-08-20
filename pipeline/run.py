"""End-to-end run through stage 3: ingest -> dedupe -> fetch -> filter ->
score -> select. Stage 4 (render/send email/write outbox note) doesn't
exist yet, so this prints the resulting selection to stdout.

Console output is a clean stage-by-stage progress view. All the verbose
detail (every HTTP request, batch poll, warning) goes to a timestamped log
file under logs/ instead of scrolling the terminal -- the run prints that
path at the end, so "paste me the log" is one `cat` rather than a scroll-
back hunt. Use --verbose to also mirror the full detail to the console.

Deliberately does NOT call seen.save() -- the persistent seen-set is only
committed once an actual send happens (stage 4), so running this repeatedly
while stage 4 gets built doesn't burn through candidates that were never
actually delivered anywhere. Matches the "caller persists only once a run
is committed" contract documented in dedupe.dedupe().

Usage:
    ./scripts/run_with_secrets.sh uv run python -m pipeline.run
    ./scripts/run_with_secrets.sh uv run python -m pipeline.run --limit-sources 5
    ./scripts/run_with_secrets.sh uv run python -m pipeline.run --verbose
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone

import anthropic
import httpx

from pipeline.config import CONFIG, ROOT
from pipeline.cost import BudgetExceededError, CostTracker
from pipeline.dedupe import SeenStore, candidate_from_raw_item, dedupe
from pipeline.fetch import fetch_all, fetch_article_texts
from pipeline.filter_stage import (
    FILTER_SYSTEM_PROMPT,
    build_filter_requests,
    interleave_by_source,
    parse_filter_results,
)
from pipeline.ingest import load_sources
from pipeline.llm_client import run_batch, run_sync
from pipeline.render import render_email_html, render_vault_note, vault_note_filename
from pipeline.score_stage import SCORE_SYSTEM_PROMPT, build_score_requests, parse_score_results
from pipeline.select import select
from pipeline.trust import TrustStore

logger = logging.getLogger(__name__)


def _setup_logging(verbose: bool):
    """Full detail always goes to a timestamped log file; the console stays
    clean unless --verbose. Returns the log file path so the run can point
    the user at it."""
    log_dir = ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):  # clear any inherited handlers
        root.removeHandler(h)

    file_handler = logging.FileHandler(log_path)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(file_handler)

    if verbose:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(console)

    return log_path


class _Progress:
    """Minimal in-place progress line for the console (\\r overwrite), so a
    37-source fetch or a multi-minute batch poll shows movement instead of
    silence -- without spamming a new line per event."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled and sys.stdout.isatty()

    def update(self, msg: str):
        if self.enabled:
            sys.stdout.write("\r\033[K" + msg)
            sys.stdout.flush()

    def done(self, msg: str):
        if self.enabled:
            sys.stdout.write("\r\033[K")
        print(msg)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit-sources", type=int, help="Only fetch the first N sources")
    parser.add_argument("--verbose", action="store_true", help="Mirror full log detail to the console")
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Use synchronous API calls instead of the async Batch API -- "
        "full price but returns in seconds, for fast iteration. The weekly "
        "production run should omit this and use the 50%%-cheaper batch path.",
    )
    args = parser.parse_args()

    log_path = _setup_logging(args.verbose)
    prog = _Progress(enabled=not args.verbose)

    print("ai-digest run starting" + ("  [SYNC mode -- full price, no batch discount]" if args.sync else ""))
    print(f"  (full detail logging to {log_path})\n")

    # --- ingest ---
    ingest_result = load_sources()
    sources = ingest_result.sources
    if args.limit_sources:
        sources = sources[: args.limit_sources]
    cache_note = ""
    if ingest_result.from_cache:
        cache_note = f"  [from {'STALE ' if ingest_result.stale else ''}cache, {ingest_result.cache_age_days}d old]"
    print(f"[1/6] ingest      {len(sources)} source(s){cache_note}")

    # --- fetch ---
    total = len(sources)
    counter = {"n": 0}

    def on_fetch_progress(source_name: str):
        counter["n"] += 1
        prog.update(f"[2/6] fetch       {counter['n']}/{total} sources  ({source_name})")

    raw_items, fetch_report = fetch_all(sources, on_progress=on_fetch_progress)
    degraded = [name for name, status in fetch_report.items() if status not in ("rss", "listing", "youtube", "unsupported")]
    prog.done(f"[2/6] fetch       {len(raw_items)} raw item(s) from {total} sources"
              + (f"  ({len(degraded)} degraded -- see log)" if degraded else ""))

    # --- dedupe ---
    candidates = [candidate_from_raw_item(item) for item in raw_items]
    seen = SeenStore()
    new_candidates, dropped = dedupe(candidates, seen)
    print(f"[3/6] dedupe      {len(new_candidates)} new  ({len(dropped)} already-seen/duplicate)")

    if not new_candidates:
        print("\nnothing new this week -- done.")
        print(f"\nlog: {log_path}")
        return 0

    cost_tracker = CostTracker()
    client = anthropic.Anthropic()
    trust_store = TrustStore()
    trust_store.materialize_seed()  # write cache/trust_tiers.json so it can be hand-edited

    def run_llm(requests, system_prompt, model, stage_label):
        """Dispatch to sync or batch transport based on --sync, wiring the
        right progress line for each. Same return shape either way."""
        if args.sync:
            def on_prog(done, total):
                prog.update(f"{stage_label}  calling {done}/{total} (sync)")
            return run_sync(requests, system_prompt, model, cost_tracker, client=client, on_progress=on_prog)

        def on_poll(elapsed, status):
            prog.update(f"{stage_label}  waiting on batch ({len(requests)} req)  {elapsed}s  [{status}]")
        return run_batch(requests, system_prompt, model, cost_tracker, client=client, on_poll=on_poll)

    # --- filter ---
    filter_requests = build_filter_requests(new_candidates)
    transport = "sync" if args.sync else "batched"
    print(f"[4/6] filter      {len(new_candidates)} candidates -> {len(filter_requests)} {transport} request(s)")
    try:
        filter_results = run_llm(
            filter_requests, FILTER_SYSTEM_PROMPT, CONFIG.llm_model_filter, "[4/6] filter     "
        )
    except BudgetExceededError as e:
        prog.done("")
        print(f"\nBUDGET CEILING HIT during filter stage, stopping:\n  {e}")
        print(cost_tracker.report())
        print(f"\nlog: {log_path}")
        return 1

    verdicts = parse_filter_results(filter_requests, new_candidates, filter_results)
    passed = [v.candidate for v in verdicts if v.passed]
    # Round-robin across sources BEFORE the cap. `passed` is in source order
    # and the cap binds every run, so a flat slice cut the same tail of
    # sources.tsv every week -- see interleave_by_source() for the measurement.
    survivors = interleave_by_source(passed)[: CONFIG.max_survivors]
    cap_note = f", capped to {len(survivors)}" if len(passed) > len(survivors) else ""
    prog.done(f"[4/6] filter      {len(passed)} passed{cap_note}")

    # --- fetch full article text for survivors, then score ---
    http_client = httpx.Client()
    try:
        text_counter = {"n": 0}

        def _one(url):
            text_counter["n"] += 1
            prog.update(f"[5/6] score       fetching article text {text_counter['n']}/{len(survivors)}")
            return url

        article_texts = fetch_article_texts(http_client, [_one(c.url) for c in survivors])
    finally:
        http_client.close()

    score_requests = build_score_requests(survivors, article_texts, trust_store=trust_store)
    prog.done(f"[5/6] score       {len(survivors)} survivor(s) -> {len(score_requests)} {transport} request(s)")
    try:
        score_results = run_llm(
            score_requests, SCORE_SYSTEM_PROMPT, CONFIG.llm_model_score, "[5/6] score      "
        )
    except BudgetExceededError as e:
        prog.done("")
        print(f"\nBUDGET CEILING HIT during score stage, stopping:\n  {e}")
        print(cost_tracker.report())
        print(f"\nlog: {log_path}")
        return 1

    outcome = parse_score_results(score_requests, survivors, score_results, trust_store=trust_store)
    selection = select(
        outcome.scored,
        # Filter REJECTS only. This number is shown to the reader as "N more
        # filtered below the cut", i.e. as curation -- so cap losses and
        # scoring failures must be counted separately, not folded in here.
        filtered_out_count=len(new_candidates) - len(passed),
        scoring_failed_count=len(outcome.failures),
        score_attempted_count=outcome.attempted,
        filter_passed_count=len(passed),
    )
    prog.done(f"[6/6] select      {len(selection.for_org)} for org, {len(selection.for_you)} for you")

    _print_digest(selection, cost_tracker)

    # --- render the reader-facing digest to preview files (no send yet) ---
    now = datetime.now(timezone.utc)
    CONFIG.outbox_dir.mkdir(parents=True, exist_ok=True)
    email_path = CONFIG.outbox_dir / f"digest-preview-{now.strftime('%Y%m%d-%H%M%S')}.html"
    email_path.write_text(render_email_html(selection, now), encoding="utf-8")
    note_path = CONFIG.outbox_dir / vault_note_filename(now)
    note_path.write_text(render_vault_note(selection, now), encoding="utf-8")

    print(f"\nlog:            {log_path}")
    print(f"email preview:  {email_path}   <- open this to see the actual digest")
    print(f"vault note:     {note_path}")
    print(
        "(preview only -- nothing sent, vault not written, seen-set not "
        "committed. Re-running shows the same items.)"
    )
    return 0


def _raw_title_note(item) -> str | None:
    """Show the pre-cleanup title when the model rewrote it, so a UAT pass
    can spot a headline that was invented rather than recovered."""
    if item.title.strip() == item.raw_title.strip():
        return None
    raw = item.raw_title.strip()
    if len(raw) > 90:
        raw = raw[:90] + "..."
    return f"  (raw: {raw})"


def _print_digest(selection, cost_tracker) -> None:
    print("\n" + "=" * 60)
    print(f"FOR THE ORG ({len(selection.for_org)})")
    print("=" * 60)
    for item in selection.for_org:
        print(f"\n[org={item.org_score}] {item.title}  ({item.source} · {item.trust_tier})")
        print(f"  {item.url}")
        print(f"  reason: {item.org_reason}")
        print(f"  {item.summary}")
        print(f"  so what: {item.so_what}")
        if item.vendor_marketing:
            print("  (flagged: reads like vendor marketing)")
        note = _raw_title_note(item)
        if note:
            print(note)

    print("\n" + "=" * 60)
    print(f"FOR YOU ({len(selection.for_you)})")
    print("=" * 60)
    for item in selection.for_you:
        print(f"\n[fluency={item.fluency_score}] {item.title}  ({item.source} · {item.trust_tier})")
        print(f"  {item.url}")
        print(f"  reason: {item.fluency_reason}")
        print(f"  {item.summary}")
        note = _raw_title_note(item)
        if note:
            print(note)

    print("\n" + "=" * 60)
    print(f"CONSIDERED AND SKIPPED ({len(selection.considered_and_skipped)})")
    print("=" * 60)
    for item in selection.considered_and_skipped:
        print(f"  org={item.org_score:3d} fluency={item.fluency_score:3d}  {item.title} ({item.source})")

    print(f"\n{cost_tracker.report()}")


if __name__ == "__main__":
    raise SystemExit(main())
