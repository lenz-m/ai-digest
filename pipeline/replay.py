"""Persist a run's scored items so the digest can be re-rendered offline.

WHY THIS EXISTS: nothing about a run used to survive it. ScoredItem lived in
memory, the rendered previews were the only artifact on disk, and the logs
carry a response body only when parsing FAILED. So every presentation change
-- a header rename, a dropped line, a new section blurb -- had to be judged
against synthetic fixtures, or cost a full ~$0.64 re-run to see against real
content. Worse, a digest that raised a question ("why did THAT rank first?")
could not be re-examined at all once the process exited.

WHAT IS PERSISTED, and what is deliberately not: the INPUTS to select() --
the scored items plus the four Selection counts -- never a rendered Selection.
Storing the inputs means a replay re-runs select() and therefore reflects the
CURRENT select_org_count / select_fluency_count / skipped_cap, so the cache is
useful for tuning selection too, not only for rendering. select() is pure and
deterministic over the same list, so a replay with unchanged config reproduces
the original digest exactly.

THE COUNTS ARE NOT OPTIONAL. filtered_out_count, scoring_failed_count,
score_attempted_count and filter_passed_count are what drive the footer's
operator diagnostics ("9 of 60 could not be scored", "60 of 312 were scored
(max_survivors cap)"). Replaying without them renders a digest that looks
healthy when the original run was not -- silently converting the exact defect
those lines exist to surface into an invisible one.

This file is derived data (full summaries and scores for ~60 articles), and is
covered by .gitignore's `cache/*` rule. It must stay that way; see the
scored_cache comment in config.py.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pipeline.config import CONFIG
from pipeline.dedupe import Candidate
from pipeline.score_stage import ScoredItem

logger = logging.getLogger(__name__)

# Bumped whenever the on-disk shape changes. A replay refuses a version it
# does not recognise rather than half-reading it: ScoredItem gained fields
# twice already (clean_title, trust_tier), and silently defaulting a missing
# field would render a digest that is subtly wrong with no symptom.
CACHE_VERSION = 1


class ReplayError(Exception):
    """The cache is missing, unreadable, or of an unrecognised version."""


@dataclass(frozen=True)
class RunCounts:
    """The four Selection counts, carried alongside the items.

    Same names as the select() keyword arguments on purpose -- they are passed
    straight through, so a new count added to Selection surfaces here as an
    obvious omission rather than a silent zero.
    """

    filtered_out_count: int = 0
    scoring_failed_count: int = 0
    score_attempted_count: int = 0
    filter_passed_count: int = 0

    def as_kwargs(self) -> dict[str, int]:
        return {
            "filtered_out_count": self.filtered_out_count,
            "scoring_failed_count": self.scoring_failed_count,
            "score_attempted_count": self.score_attempted_count,
            "filter_passed_count": self.filter_passed_count,
        }


@dataclass(frozen=True)
class ReplayPayload:
    scored: list[ScoredItem]
    counts: RunCounts
    run_at: datetime
    # Provenance, so a replay can say what it is replaying. `applied` matters
    # most: a cached --apply run is a digest that actually reached the inbox,
    # which is precisely the one worth re-examining.
    applied: bool = False
    log_path: str = ""

    def age_days(self, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        return max(0, (now - self.run_at).days)

    def is_stale(self, now: datetime | None = None) -> bool:
        return self.age_days(now) > CONFIG.replay_stale_days


# --------------------------------------------------------------------------
# Serialization
# --------------------------------------------------------------------------
#
# Written out by hand rather than via dataclasses.asdict(). ScoredItem exposes
# title/raw_title/url/source as PROPERTIES derived from its candidate; writing
# them out would create a second copy that a later edit could contradict, and
# reading them back would collide with the real fields.


def _candidate_to_dict(c: Candidate) -> dict:
    return {
        "title": c.title,
        "url": c.url,
        "source": c.source,
        "published": c.published.isoformat() if c.published else None,
        "excerpt": c.excerpt,
    }


def _candidate_from_dict(d: dict) -> Candidate:
    published = d.get("published")
    return Candidate(
        title=d["title"],
        url=d["url"],
        source=d["source"],
        published=datetime.fromisoformat(published) if published else None,
        excerpt=d.get("excerpt", ""),
    )


def _item_to_dict(item: ScoredItem) -> dict:
    return {
        "candidate": _candidate_to_dict(item.candidate),
        "org_score": item.org_score,
        "org_reason": item.org_reason,
        "fluency_score": item.fluency_score,
        "fluency_reason": item.fluency_reason,
        "summary": item.summary,
        "so_what": item.so_what,
        "vendor_marketing": item.vendor_marketing,
        "clean_title": item.clean_title,
        "trust_tier": item.trust_tier,
    }


def _item_from_dict(d: dict) -> ScoredItem:
    return ScoredItem(
        candidate=_candidate_from_dict(d["candidate"]),
        org_score=d["org_score"],
        org_reason=d["org_reason"],
        fluency_score=d["fluency_score"],
        fluency_reason=d["fluency_reason"],
        summary=d["summary"],
        so_what=d["so_what"],
        vendor_marketing=d["vendor_marketing"],
        clean_title=d.get("clean_title", ""),
        trust_tier=d.get("trust_tier", ""),
    )


def save_scored_run(
    scored: list[ScoredItem],
    counts: RunCounts,
    *,
    run_at: datetime | None = None,
    applied: bool = False,
    log_path: str = "",
    path: Path | None = None,
) -> Path:
    """Dump the scored items + counts. Raises on failure -- see save_quietly().

    Written on EVERY run, --apply included. The run most worth re-examining is
    a real send that produced a digest with a questionable ranking in it, and
    that is exactly the run a dry-run-only cache would not have captured.
    """
    path = path or CONFIG.scored_cache
    run_at = run_at or datetime.now(timezone.utc)

    payload = {
        "version": CACHE_VERSION,
        "run_at": run_at.isoformat(),
        "applied": applied,
        "log_path": log_path,
        "counts": counts.as_kwargs(),
        "scored": [_item_to_dict(i) for i in scored],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("wrote %d scored item(s) to %s for --render-only", len(scored), path)
    return path


def save_quietly(
    scored: list[ScoredItem],
    counts: RunCounts,
    *,
    applied: bool = False,
    log_path: str = "",
) -> None:
    """save_scored_run, but a failure is logged and swallowed.

    This cache is a debugging convenience. It is written mid-run, moments
    before an --apply run sends the actual email, so a full disk or a
    permissions fault here must never be able to abort a delivery that would
    otherwise have succeeded. Losing the replay cache costs one re-run; losing
    the send costs the week.
    """
    try:
        save_scored_run(scored, counts, applied=applied, log_path=log_path)
    except Exception as exc:  # noqa: BLE001 -- deliberately total
        logger.warning(
            "could not write the scored-items cache (%s): %s. The run is "
            "unaffected; --render-only just won't be able to replay it.",
            CONFIG.scored_cache, exc,
        )


def load_scored_run(path: Path | None = None) -> ReplayPayload:
    path = path or CONFIG.scored_cache

    if not path.exists():
        raise ReplayError(
            f"no scored-items cache at {path}. It is written by every run that "
            "reaches the scoring stage, so run the pipeline once first "
            "(a dry run is enough)."
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReplayError(f"could not read the scored-items cache at {path}: {exc}") from exc

    version = raw.get("version")
    if version != CACHE_VERSION:
        raise ReplayError(
            f"scored-items cache at {path} is version {version!r}, but this "
            f"build reads version {CACHE_VERSION}. Its item shape has changed; "
            "re-run the pipeline to write a fresh one rather than replaying "
            "a partially-understood file."
        )

    try:
        scored = [_item_from_dict(d) for d in raw["scored"]]
        counts = RunCounts(**raw["counts"])
        run_at = datetime.fromisoformat(raw["run_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ReplayError(
            f"scored-items cache at {path} claims version {CACHE_VERSION} but "
            f"does not match that shape: {exc}"
        ) from exc

    return ReplayPayload(
        scored=scored,
        counts=counts,
        run_at=run_at,
        applied=bool(raw.get("applied", False)),
        log_path=raw.get("log_path", ""),
    )


def provenance_lines(payload: ReplayPayload, now: datetime | None = None) -> list[str]:
    """What am I actually looking at? -- printed before the replayed digest.

    A stale replay is the one real hazard of this feature: the output is
    byte-identical in shape to a fresh run, so nothing in the digest itself
    says the news in it is a fortnight old. Hence a plain age line always, and
    a loud one past CONFIG.replay_stale_days.
    """
    age = payload.age_days(now)
    when = payload.run_at.astimezone().strftime("%a %d %b %Y %H:%M")
    kind = "--apply (email was sent)" if payload.applied else "dry run"

    lines = [
        f"replaying the run of {when}  ({age}d ago, {kind})",
        f"  {len(payload.scored)} scored item(s), {payload.counts.score_attempted_count} attempted",
    ]
    if payload.log_path:
        lines.append(f"  original log: {payload.log_path}")

    if payload.is_stale(now):
        # One list entry per physical line: the caller indents each one, so an
        # embedded \n here comes out ragged.
        lines += [
            "",
            f"*** WARNING: this run is {age} DAYS OLD "
            f"(stale past {CONFIG.replay_stale_days}d). ***",
            "You are re-rendering OLD news. Nothing in the digest below will say",
            "so -- it renders exactly like a fresh one. Run the pipeline for real",
            "if you wanted this week's stories.",
        ]
    return lines
