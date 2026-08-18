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
    for_org: list[ScoredItem]
    for_you: list[ScoredItem]
    considered_and_skipped: list[ScoredItem]
    filtered_out_count: int = 0


def select(
    items: list[ScoredItem],
    org_count: int | None = None,
    fluency_count: int | None = None,
    skipped_cap: int | None = None,
    filtered_out_count: int = 0,
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
    )
