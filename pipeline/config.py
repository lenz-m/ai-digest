"""Central configuration for the ai-digest pipeline.

All paths default to locations relative to the project root, and every one
of them can be overridden by an environment variable. That's what lets the
exact same code run unmodified on the dev Mac and on the Pi -- only the .env
differs.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# Loads .env (ANTHROPIC_API_KEY, SMTP creds once stage 4 exists, etc.) into
# the environment. Every other module gets CONFIG from this one, so this
# runs exactly once, on first import, before anything reads os.environ.
# Silently a no-op if .env doesn't exist -- nothing here should be
# required for the pure-logic modules/tests to work standalone.
load_dotenv(ROOT / ".env")


def _path(env_var: str, default: str) -> Path:
    return Path(os.environ.get(env_var, str(ROOT / default))).expanduser()


@dataclass(frozen=True)
class Config:
    # Reminders TSV, dropped here by the Mac-side rsync job.
    sources_tsv: Path = _path("AI_DIGEST_SOURCES_TSV", "data/sources.tsv")

    # Last-known-good copy the pipeline falls back to if sources_tsv is
    # missing, empty, or unparsable. A shut laptop must never block a run.
    sources_cache: Path = _path("AI_DIGEST_SOURCES_CACHE", "cache/sources_last_good.tsv")

    # Manually-maintained sources, INDEPENDENT of the friend's Reminders
    # export -- these are merged in on every run and are never touched by the
    # Reminders rsync (which only overwrites sources_tsv). Same TSV format.
    # This is where direct-feed sources the user owns (e.g. HBR's Atom feed)
    # live, so a Reminders sync can't clobber them.
    manual_sources_tsv: Path = _path("AI_DIGEST_MANUAL_SOURCES_TSV", "data/manual_sources.tsv")

    # How old the fallback cache can be before we still use it, but escalate
    # the log warning from "using cache" to "using STALE cache".
    sources_stale_after_days: int = int(os.environ.get("AI_DIGEST_SOURCES_STALE_DAYS", "10"))

    # Persistent seen-set, keyed by content hash. Survives forever.
    seen_cache: Path = _path("AI_DIGEST_SEEN_CACHE", "cache/seen.json")

    # Title-similarity threshold for cross-source duplicate clustering within
    # a run. Matches the 0.90 convention already used in the
    # notion-to-obsidian scripts' title_similarity().
    title_similarity_threshold: float = float(
        os.environ.get("AI_DIGEST_TITLE_SIM_THRESHOLD", "0.90")
    )

    # Where the finished .md lands for the Mac to rsync into the vault.
    outbox_dir: Path = _path("AI_DIGEST_OUTBOX", "outbox")

    # Per-source fetch strategy (rss/youtube/listing/unsupported), cached so
    # a weekly run doesn't re-probe feed autodiscovery every time. Same
    # human-correctable-cache convention as add_covers.py's confidence cache.
    fetch_strategy_cache: Path = _path("AI_DIGEST_FETCH_STRATEGY_CACHE", "cache/fetch_strategy.json")

    # Re-probe a source's strategy after this many days even if cached,
    # in case a site adds/removes/moves its feed. Ignored for entries with
    # human_override set -- a manual correction never silently expires.
    fetch_strategy_max_age_days: int = int(
        os.environ.get("AI_DIGEST_FETCH_STRATEGY_MAX_AGE_DAYS", "30")
    )

    # Drop candidate items older than this many days (when a published date
    # is known) -- a weekly digest shouldn't surface a feed's full archive.
    # 10 rather than 7 gives slack for a run that's a bit late or a slightly
    # delayed publish timestamp.
    fetch_max_age_days: int = int(os.environ.get("AI_DIGEST_FETCH_MAX_AGE_DAYS", "10"))

    # --- Stage 3: filter / score / summarize ---

    # Cheap triage model. Sees title + short excerpt only, decides pass/skip
    # for the expensive stage. Packed many-per-prompt -- see filter_stage.py.
    llm_model_filter: str = os.environ.get("AI_DIGEST_MODEL_FILTER", "claude-haiku-4-5-20251001")

    # Combined score+summary model. Sees full article text, one per prompt
    # (each item needs distinct full text, so packing many per prompt isn't
    # practical the way the filter stage does).
    llm_model_score: str = os.environ.get("AI_DIGEST_MODEL_SCORE", "claude-sonnet-5")

    # Max candidates packed into a single filter prompt. Bounds context size
    # per request; the actual API-call-count reduction comes from this packing
    # PLUS submitting all resulting prompts as one Batch API job.
    filter_batch_size: int = int(os.environ.get("AI_DIGEST_FILTER_BATCH_SIZE", "40"))

    # Hard cap on how many filter-survivors proceed to the expensive score
    # stage, regardless of how many the filter passes. Protects cost even if
    # the cheap filter is unexpectedly permissive one week.
    max_survivors: int = int(os.environ.get("AI_DIGEST_MAX_SURVIVORS", "60"))

    # Hard per-run budget ceiling in USD. A call that would push cumulative
    # cost past this stops the run (raises BudgetExceededError) and logs,
    # rather than silently spending past it. Generous relative to the
    # ~$1/week estimate -- this is a circuit breaker for genuine anomalies
    # (a runaway feed, a pricing change), not a tight weekly budget.
    cost_ceiling_usd: float = float(os.environ.get("AI_DIGEST_COST_CEILING_USD", "5.00"))

    # Full article text fed into the score+summary prompt is truncated to
    # this many characters (~4 chars/token) to bound per-item cost even if
    # trafilatura pulls back an unusually long article.
    article_text_max_chars: int = int(os.environ.get("AI_DIGEST_ARTICLE_TEXT_MAX_CHARS", "6000"))

    # Max output tokens for a score+summary response. Was 500, which the
    # first real run showed to be too tight -- Sonnet averaged ~474 out
    # tokens/call against that cap, so many responses were truncated
    # mid-JSON and dropped as unparseable. 1000 gives comfortable headroom
    # for the full JSON object (four reason/summary fields) without being
    # so large it materially changes cost. Bounds cost; doesn't drive it.
    score_max_tokens: int = int(os.environ.get("AI_DIGEST_SCORE_MAX_TOKENS", "1000"))

    # Source-level trust tier (independent_analysis / independent_news /
    # vendor), JSON-cached and human-correctable. Down-weights vendor-
    # published content for org/strategy relevance -- see trust.py.
    trust_cache: Path = _path("AI_DIGEST_TRUST_CACHE", "cache/trust_tiers.json")

    # Batch API polling.
    batch_poll_interval_seconds: int = int(os.environ.get("AI_DIGEST_BATCH_POLL_INTERVAL", "30"))
    batch_max_wait_seconds: int = int(os.environ.get("AI_DIGEST_BATCH_MAX_WAIT", "7200"))  # 2h

    # Selection: top N by org_score (picked and removed first), then top M
    # by fluency_score from what's left -- see CLAUDE.md for why the removal
    # order matters. skipped_cap bounds the "Considered and skipped" audit
    # list so it stays readable instead of becoming a second inbox.
    select_org_count: int = int(os.environ.get("AI_DIGEST_SELECT_ORG_COUNT", "5"))
    select_fluency_count: int = int(os.environ.get("AI_DIGEST_SELECT_FLUENCY_COUNT", "3"))
    select_skipped_cap: int = int(os.environ.get("AI_DIGEST_SELECT_SKIPPED_CAP", "15"))


CONFIG = Config()
