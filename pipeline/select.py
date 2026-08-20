"""Stage 3d: two-objective selection -- pure logic.

Top CONFIG.select_org_count by org_score are picked FIRST and removed from
the pool; only then are the top CONFIG.select_fluency_count by fluency_score
picked from what remains. That removal order is what makes "For you"
actually protected: if fluency were ranked against the full pool including
the org picks, a slow AI-fluency week could still get crowded out by
overlapping org-relevant stories. Ranking fluency against a pool with the
org picks already removed is the entire mechanism.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from pipeline.config import CONFIG
from pipeline.score_stage import ScoredItem


@dataclass(frozen=True)
class Selection:
    """The finished digest, plus the counts the email footer reports on.

    Those counts used to be a single `filtered_out_count` computed as
    len(candidates) - len(scored), which lumped together three unrelated
    things: filter rejects, items cut by the max_survivors cap, and
    score-stage parse failures. render.py showed that number to the reader as
    "N more filtered below the cut", so a scoring-stage BREAKAGE was reported
    as successful CURATION -- on the first real run it would have read "22
    more filtered below the cut" when the truth was "22 responses failed to
    parse". They are split here so each can be reported as what it is.
    """

    for_org: list[ScoredItem]
    for_you: list[ScoredItem]
    considered_and_skipped: list[ScoredItem]

    # Filter rejects ONLY -- genuine curation, reader-facing.
    filtered_out_count: int = 0

    # Machinery, not curation. scoring_failed_count without its denominator is
    # unjudgeable (9-of-12 and 9-of-300 want opposite responses), so the
    # attempted count travels with it.
    scoring_failed_count: int = 0
    score_attempted_count: int = 0

    # How many the filter passed, vs. score_attempted_count actually scored.
    # The gap is the max_survivors cap, which binds on every full run and is
    # currently the larger silent loss of the two.
    filter_passed_count: int = 0


def select(
    items: list[ScoredItem],
    org_count: int | None = None,
    fluency_count: int | None = None,
    skipped_cap: int | None = None,
    filtered_out_count: int = 0,
    scoring_failed_count: int = 0,
    score_attempted_count: int = 0,
    filter_passed_count: int = 0,
) -> Selection:
    org_count = CONFIG.select_org_count if org_count is None else org_count
    fluency_count = CONFIG.select_fluency_count if fluency_count is None else fluency_count
    skipped_cap = CONFIG.select_skipped_cap if skipped_cap is None else skipped_cap

    remaining = list(items)

    by_org = sorted(remaining, key=lambda i: i.org_score, reverse=True)
    for_org = by_org[:org_count]
    for_org_urls = {i.url for i in for_org}
    remaining = [i for i in remaining if i.url not in for_org_urls]

    by_fluency = sorted(remaining, key=lambda i: i.fluency_score, reverse=True)
    for_you = by_fluency[:fluency_count]
    for_you_urls = {i.url for i in for_you}
    remaining = [i for i in remaining if i.url not in for_you_urls]

    considered = sorted(remaining, key=lambda i: max(i.org_score, i.fluency_score), reverse=True)
    considered = considered[:skipped_cap]

    return Selection(
        for_org=for_org,
        for_you=for_you,
        considered_and_skipped=considered,
        filtered_out_count=filtered_out_count,
        scoring_failed_count=scoring_failed_count,
        score_attempted_count=score_attempted_count,
        filter_passed_count=filter_passed_count,
    )
