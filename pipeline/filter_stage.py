"""Stage 3a: cheap batched filter -- pure logic, no anthropic SDK import.

This is the stage a previous version of this pipeline got wrong: calling
the API per-item for a cheap classification step ran ~3x over budget before
anyone noticed. The fix is structural, not a config flag: build_filter_requests()
physically cannot produce more than ceil(N / CONFIG.filter_batch_size)
requests for N candidates, because each request's prompt is built by packing
up to filter_batch_size candidates into it. tests/test_filter_stage.py
asserts this directly.

Deliberately permissive: this stage only needs to decide "is this even
plausibly worth spending Sonnet tokens on," not make the final call. It
sees title + short excerpt only (no full article text -- that's fetched
later, only for survivors, in score_stage.py) so it stays cheap.
"""
from __future__ import annotations

import json
import logging

from pipeline.config import CONFIG
from pipeline.dedupe import Candidate
from pipeline.llm_types import LLMRequest, LLMResult

logger = logging.getLogger(__name__)

FILTER_SYSTEM_PROMPT = """You are a fast relevance triage step for a weekly AI-news digest. \
The reader is head of strategy for a global professional-services delivery organization \
(client work, staffing pyramids, offshore/GCC model) who is also working toward an \
AI-focused career pivot over the next couple of years.

For each numbered candidate below (title + short excerpt only -- you do not have the \
full article), decide whether it is even PLAUSIBLY relevant to either audience:

1. "org" -- AI's impact on delivery-economics: automation of billable tasks, \
staffing/pyramid implications, pricing model disruption, buyer expectations, \
competitor moves, credible enterprise AI adoption data (not vendor marketing).
2. "fluency" -- AI-practitioner fluency: what practitioners actually debate, how the \
technology really works or really fails, emerging AI-focused roles and skills. This \
can have ZERO near-term relevance to delivery economics -- that's the point of this \
category, don't filter it out for being "not business relevant."

Be permissive, not precise. This is a cheap first pass meant to discard only the \
obviously-irrelevant (unrelated personal-interest content with no AI angle, sports, \
pure marketing fluff with zero substance) -- not to make the final editorial call. \
When genuinely unsure, pass it through; the next stage makes the real judgment with \
full article text.

Respond with ONLY a JSON array, one object per candidate in the same order given, each \
with exactly these fields: {"id": <int>, "pass": <bool>, "reason": <short phrase>}. \
No other text before or after the array."""


def _truncate(text: str, max_chars: int) -> str:
    text = text.strip()
    return text if len(text) <= max_chars else text[:max_chars].rstrip() + "..."


def _chunk(seq: list, size: int) -> list[list]:
    return [seq[i : i + size] for i in range(0, len(seq), size)]


def build_filter_prompt(batch: list[tuple[int, Candidate]]) -> str:
    lines = []
    for id_, c in batch:
        excerpt = _truncate(c.excerpt, 300) if c.excerpt else ""
        line = f"{id_}. [{c.source}] {c.title}"
        if excerpt:
            line += f" -- {excerpt}"
        lines.append(line)
    return "\n".join(lines)


def build_filter_requests(
    candidates: list[Candidate], batch_size: int | None = None
) -> list[LLMRequest]:
    """Packs candidates into groups of at most batch_size, one LLMRequest
    per group. For N candidates this returns ceil(N / batch_size) requests,
    never N -- that's the entire point of this function existing separately
    from a naive per-item loop.
    """
    batch_size = CONFIG.filter_batch_size if batch_size is None else batch_size
    indexed = list(enumerate(candidates))
    requests = []
    for i, batch in enumerate(_chunk(indexed, batch_size)):
        requests.append(
            LLMRequest(
                custom_id=f"filter-{i}",
                prompt=build_filter_prompt(batch),
                max_tokens=200 + 60 * len(batch),
            )
        )
    return requests


def _extract_json_array(text: str) -> list | None:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else None


def parse_filter_response(text: str, expected_ids: set[int]) -> dict[int, tuple[bool, str]]:
    """Tolerant parsing -- a malformed or partial response degrades to
    "nothing parsed for these ids" rather than raising, so one bad batch
    doesn't crash the run."""
    data = _extract_json_array(text)
    if data is None:
        return {}

    result: dict[int, tuple[bool, str]] = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        try:
            id_ = int(entry["id"])
            passed = bool(entry["pass"])
        except (KeyError, TypeError, ValueError):
            continue
        reason = str(entry.get("reason", ""))
        if id_ in expected_ids:
            result[id_] = (passed, reason)
    return result


class FilterVerdict:
    __slots__ = ("candidate", "passed", "reason")

    def __init__(self, candidate: Candidate, passed: bool, reason: str):
        self.candidate = candidate
        self.passed = passed
        self.reason = reason

    def __repr__(self) -> str:
        return f"FilterVerdict(passed={self.passed!r}, reason={self.reason!r}, title={self.candidate.title!r})"

    def __eq__(self, other) -> bool:
        return (
            isinstance(other, FilterVerdict)
            and self.candidate == other.candidate
            and self.passed == other.passed
            and self.reason == other.reason
        )


def parse_filter_results(
    requests: list[LLMRequest],
    candidates: list[Candidate],
    results: dict[str, LLMResult],
    batch_size: int | None = None,
) -> list[FilterVerdict]:
    """Maps batch results back onto the original candidate list. An id that
    never got a parseable verdict (bad JSON, model dropped it, the whole
    request errored) fails OPEN -- passed through to the expensive stage
    rather than silently lost, on the theory that a missed item costs a few
    cents at stage 3 while a wrongly-dropped item costs nothing but is
    invisible, which is the worse failure mode for a curation product.
    """
    batch_size = CONFIG.filter_batch_size if batch_size is None else batch_size
    indexed = list(enumerate(candidates))
    verdicts: list[FilterVerdict | None] = [None] * len(candidates)

    for i, batch in enumerate(_chunk(indexed, batch_size)):
        expected_ids = {id_ for id_, _ in batch}
        result = results.get(f"filter-{i}")
        parsed: dict[int, tuple[bool, str]] = {}
        if result is not None and result.text is not None:
            parsed = parse_filter_response(result.text, expected_ids)
        elif result is not None and result.error:
            logger.warning("filter batch %d errored: %s -- passing all through", i, result.error)

        for id_, c in batch:
            if id_ in parsed:
                passed, reason = parsed[id_]
                verdicts[id_] = FilterVerdict(candidate=c, passed=passed, reason=reason)
            else:
                verdicts[id_] = FilterVerdict(candidate=c, passed=True, reason="unparsed, passed through")

    return [v for v in verdicts if v is not None]


def interleave_by_source(candidates: list[Candidate]) -> list[Candidate]:
    """Reorder filter-survivors round-robin across sources, preserving each
    source's internal order.

    WHY this exists (measured 2026-08-20, see CLAUDE.md "Seen-set commit
    rule" and docs/stage4-send-plan.md §0.7): the caller slices this list to
    CONFIG.max_survivors, and the cap binds on every full run (441 candidates
    -> 312 passed -> exactly 60 scored). FilterVerdict carries only a
    pass/fail bool, so there is no score to rank by and the list arrives in
    *source order*. A flat slice therefore cuts the same tail of sources.tsv
    every single week, deterministically -- the Aug 16 run ran out around
    source row 14 of 51, and the six manual-only feeds added specifically to
    fix the delivery-economics gap (Economist x3, WSJ x3) sit at rows 46-51
    and had never been scored on any run.

    Round-robin fixes two things at once. Every source gets representation,
    so a high-volume feed near the front can't consume the whole budget; and
    the cut becomes genuinely merit-neutral and re-rolls week to week as feed
    contents change, which is what makes the seen-set rule's "leave cap-cut
    items unmarked so they get another shot" premise actually true.
    """
    by_source: dict[str, list[Candidate]] = {}
    for c in candidates:
        by_source.setdefault(c.source, []).append(c)

    # dict preserves insertion order, so the first round is still source
    # order -- an unbiased cut, not a reshuffled one.
    queues = list(by_source.values())
    out: list[Candidate] = []
    while queues:
        queues = [q for q in queues if q]
        for q in queues:
            out.append(q.pop(0))
    return out
