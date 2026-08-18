"""Stage 3c: combined score + summary -- pure logic, no anthropic SDK import.

One request per surviving item, never a separate summarization round-trip:
scoring and summarizing both need to have read the article, so they happen
in the same call. The two scores (org_score, fluency_score) are independent
-- see select.py for why that matters and how the removal order protects
"For you" from being crowded out.

All requests for a run are submitted together as one Batch API job by
llm_client.py, not called synchronously one at a time.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from pipeline.config import CONFIG
from pipeline.dedupe import Candidate
from pipeline.llm_types import LLMRequest, LLMResult

logger = logging.getLogger(__name__)

SCORE_SYSTEM_PROMPT = """You are scoring and summarizing one article for a weekly AI-news \
digest. The reader is head of strategy for a global professional-services delivery \
organization (client work, staffing pyramids, offshore/GCC model) who is also working \
toward an AI-focused career pivot over the next couple of years.

Each article you score is tagged with its SOURCE TRUST TIER (shown in the user \
message, not here). Use it as directed below.

Score this article on TWO INDEPENDENT axes -- do not let one influence the other:

1. org_score (0-100): STRATEGIC relevance for the head of strategy at a delivery \
organization. This is a STRATEGY axis, not an implementation one. Two kinds of content \
earn a high score:
  (a) Directly about delivery economics -- staffing/pyramid and headcount shifts, \
pricing-model disruption, changes in buyer expectations or procurement behavior, \
M&A and margin/valuation signals for services firms, credible INDEPENDENT \
enterprise-adoption data. This is the strongest kind; score it highest when present.
  (b) AI-industry strategic context a delivery leader should track even when it's not \
about services firms specifically -- frontier-lab economics and valuations, \
competitor and market-structure moves, capex/compute cycles, and shifts in what \
enterprise buyers pay for and why. This is legitimate strategic backdrop; score it \
solidly, just below (a)-type content when both are present in a given week.
  Down-rank HARD (score under 20): "how to deploy / govern / evaluate / implement / \
build with an AI tool" content, product tutorials, feature announcements, model \
release notes, and vendor how-tos. This is work a CTO or an implementation team \
evaluates -- it is NOT strategy, even when the tool could automate a billable task. \
The test is: "does this tell the reader how the BUSINESS or the MARKET is changing, or \
just how to USE a new tool?" -- only the former earns a high org_score.
  Source trust matters here: vendor-published sources must clear a HIGHER bar for \
org_score, and their self-reported adoption/impact claims should be treated \
skeptically. Independent analysis and journalism is what this axis is for.

2. fluency_score (0-100): relevance to AI-practitioner fluency -- what practitioners \
actually debate, how the technology really works or fails, emerging AI-focused roles \
and skills. Score this ENTIRELY independently of org_score -- a piece can be highly \
fluency-relevant while having zero near-term business relevance, and that's fine. \
Judge fluency on TECHNICAL SUBSTANCE regardless of source: a vendor engineering post \
can be highly fluency-relevant on its merits, so do NOT apply the vendor trust penalty \
to this axis (it applies to org_score only).

Also flag vendor_marketing: true if this reads like promotional content from the \
vendor/company it's about, with unverified claims and no independent substance, false \
otherwise. This downweights nothing automatically -- it's shown to the reader so they \
can judge for themselves.

Write:
- clean_title: the article's actual headline. The title given to you was often \
scraped from a listing page and may have category labels, bylines, or read-time \
estimates concatenated onto it with no separator (e.g. \
"Data AnalyticsHow to Govern Gemini at ScaleBy Jane Doe • 9-minute read" should \
become "How to Govern Gemini at Scale"). Recover the real headline from the article \
text. If the given title is already clean, return it unchanged. Never invent a \
headline that isn't the article's own.
- summary: exactly 2 sentences, factual, no editorializing.
- so_what: one sentence on the specific implication for a delivery organization -- \
skip if org_score is low and this would be a stretch.
- org_reason: one sentence explaining the org_score.
- fluency_reason: one sentence explaining the fluency_score.

Respond with ONLY a JSON object with exactly these fields: {"clean_title": <string>, \
"org_score": <int 0-100>, "org_reason": <string>, "fluency_score": <int 0-100>, \
"fluency_reason": <string>, "summary": <string>, "so_what": <string>, \
"vendor_marketing": <bool>}. No other text."""


@dataclass(frozen=True)
class ScoredItem:
    candidate: Candidate
    org_score: int
    org_reason: str
    fluency_score: int
    fluency_reason: str
    summary: str
    so_what: str
    vendor_marketing: bool
    clean_title: str = ""
    trust_tier: str = ""  # source trust tier at scoring time, for display/audit

    @property
    def title(self) -> str:
        """Display title: the model's cleaned headline when it recovered one,
        else the raw scraped title. Listing-scraped sources glue category
        labels/bylines/read-times onto the headline with no separator, which
        is very visible in the final email."""
        return self.clean_title.strip() or self.candidate.title

    @property
    def raw_title(self) -> str:
        """The original scraped title, kept for debugging/auditing the clean-up."""
        return self.candidate.title

    @property
    def url(self) -> str:
        return self.candidate.url

    @property
    def source(self) -> str:
        return self.candidate.source


def build_score_prompt(candidate: Candidate, article_text: str, trust_tier_desc: str = "") -> str:
    text = article_text.strip()
    max_chars = CONFIG.article_text_max_chars
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "..."
    tier_line = f"Source trust tier: {trust_tier_desc}\n" if trust_tier_desc else ""
    return (
        f"Source: {candidate.source}\n"
        f"{tier_line}"
        f"Title: {candidate.title}\n"
        f"URL: {candidate.url}\n\n"
        f"Article text:\n{text or '(no article text available -- score from title/source alone)'}"
    )


def build_score_requests(
    survivors: list[Candidate], article_texts: dict[str, str], trust_store=None
) -> list[LLMRequest]:
    """One request per survivor. article_texts maps candidate.url -> full
    text (fetched separately, only for survivors -- see fetch.fetch_article_text).
    trust_store, if given, tags each prompt with the source's trust tier so
    the org rubric can hold vendor-published content to a higher bar."""
    requests = []
    for i, c in enumerate(survivors):
        text = article_texts.get(c.url, "")
        tier_desc = trust_store.describe(c.source) if trust_store is not None else ""
        requests.append(
            LLMRequest(
                custom_id=f"score-{i}",
                prompt=build_score_prompt(c, text, tier_desc),
                max_tokens=CONFIG.score_max_tokens,
            )
        )
    return requests


def _first_balanced_object(text: str) -> str | None:
    """Return the first balanced {...} substring, ignoring braces inside JSON
    strings. Lets us recover the object even if the model wrapped it in
    prose ("Here is the analysis: {...}") -- which strict json.loads on the
    whole response would reject."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None  # ran off the end -> object was truncated, unrecoverable


def _extract_json_object(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    # Try the whole thing first, then fall back to the first balanced object
    # in case the model added prose before/after the JSON.
    for candidate in (text, _first_balanced_object(text)):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _clamp_score(value, default: int = 0) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, min(100, n))


def parse_score_response(text: str) -> dict | None:
    data = _extract_json_object(text)
    if data is None:
        return None
    return {
        "clean_title": str(data.get("clean_title", "")),
        "org_score": _clamp_score(data.get("org_score")),
        "org_reason": str(data.get("org_reason", "")),
        "fluency_score": _clamp_score(data.get("fluency_score")),
        "fluency_reason": str(data.get("fluency_reason", "")),
        "summary": str(data.get("summary", "")),
        "so_what": str(data.get("so_what", "")),
        "vendor_marketing": bool(data.get("vendor_marketing", False)),
    }


def parse_score_results(
    requests: list[LLMRequest],
    survivors: list[Candidate],
    results: dict[str, LLMResult],
    trust_store=None,
) -> list[ScoredItem]:
    """An item whose response fails to parse, or whose request errored, is
    DROPPED here rather than included with fabricated scores -- unlike the
    filter stage's fail-open behavior, showing a bogus score to the reader
    is worse than the item silently not appearing this week.

    trust_store, if given, stamps each ScoredItem with its source's tier for
    display/audit (the tier already influenced scoring via the prompt).
    """
    scored: list[ScoredItem] = []
    for req, c in zip(requests, survivors):
        result = results.get(req.custom_id)
        if result is None or result.text is None:
            if result is not None and result.error:
                logger.warning("score request for %r errored: %s -- dropping", c.title, result.error)
            continue
        parsed = parse_score_response(result.text)
        if parsed is None:
            # Include the raw text (bounded) and its length so a recurring
            # parse failure is diagnosable from the log -- e.g. truncation
            # (text ends mid-JSON) vs. an unexpected refusal or wrapper.
            raw = result.text or ""
            logger.warning(
                "could not parse score response for %r -- dropping. "
                "len=%d out_tokens=%d raw_tail=%r",
                c.title, len(raw), result.output_tokens, raw[-200:],
            )
            continue
        tier = trust_store.get_tier(c.source) if trust_store is not None else ""
        scored.append(ScoredItem(candidate=c, trust_tier=tier, **parsed))
    return scored
