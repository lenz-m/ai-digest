from __future__ import annotations

from pipeline.dedupe import Candidate
from pipeline.score_stage import ScoredItem
from pipeline.select import select


def _item(title, url, org_score, fluency_score) -> ScoredItem:
    return ScoredItem(
        candidate=Candidate(title=title, url=url, source="Feed"),
        org_score=org_score,
        org_reason="r",
        fluency_score=fluency_score,
        fluency_reason="r",
        summary="s",
        so_what="w",
        vendor_marketing=False,
    )


def test_top_org_items_selected_by_org_score():
    items = [_item(f"item{i}", f"https://x.com/{i}", org_score=i * 10, fluency_score=0) for i in range(10)]
    result = select(items, org_count=5, fluency_count=3, skipped_cap=15)
    assert [i.org_score for i in result.for_org] == [90, 80, 70, 60, 50]


def test_protected_allocation_fluency_ranked_against_remaining_pool_only():
    # An item that would be top-3 fluency AND top-5 org gets taken by org
    # first. The fluency slot it would have filled must still go to the
    # next-best fluency item from what's left -- not disappear.
    items = [
        _item("org1", "https://x.com/org1", org_score=100, fluency_score=100),  # wins both, org takes it
        _item("org2", "https://x.com/org2", org_score=95, fluency_score=10),
        _item("org3", "https://x.com/org3", org_score=90, fluency_score=10),
        _item("org4", "https://x.com/org4", org_score=85, fluency_score=10),
        _item("org5", "https://x.com/org5", org_score=80, fluency_score=10),
        _item("fluency1", "https://x.com/f1", org_score=5, fluency_score=99),
        _item("fluency2", "https://x.com/f2", org_score=5, fluency_score=98),
        _item("fluency3", "https://x.com/f3", org_score=5, fluency_score=97),
    ]
    result = select(items, org_count=5, fluency_count=3, skipped_cap=15)

    org_titles = {i.title for i in result.for_org}
    assert org_titles == {"org1", "org2", "org3", "org4", "org5"}

    # "For you" is still full (3 items) even though "org1" had the single
    # highest fluency_score of all -- it's protected from ever showing up
    # here because it already got claimed by org, and the pool for fluency
    # selection is what's left, not the original full pool.
    fluency_titles = [i.title for i in result.for_you]
    assert fluency_titles == ["fluency1", "fluency2", "fluency3"]
    assert "org1" not in fluency_titles


def test_no_item_appears_in_both_sections():
    items = [_item(f"item{i}", f"https://x.com/{i}", org_score=100 - i, fluency_score=100 - i) for i in range(10)]
    result = select(items, org_count=5, fluency_count=3, skipped_cap=15)
    org_urls = {i.url for i in result.for_org}
    fluency_urls = {i.url for i in result.for_you}
    assert org_urls.isdisjoint(fluency_urls)


def test_considered_and_skipped_gets_the_leftovers_sorted_by_best_score():
    items = [_item(f"item{i}", f"https://x.com/{i}", org_score=i, fluency_score=0) for i in range(10)]
    result = select(items, org_count=2, fluency_count=1, skipped_cap=15)
    # 10 items, 2 to org, 1 to fluency -> 7 remain, sorted by best axis desc
    remaining_scores = [i.org_score for i in result.considered_and_skipped]
    assert remaining_scores == sorted(remaining_scores, reverse=True)
    assert len(result.considered_and_skipped) == 7


def test_considered_and_skipped_respects_cap():
    items = [_item(f"item{i}", f"https://x.com/{i}", org_score=i, fluency_score=0) for i in range(50)]
    result = select(items, org_count=5, fluency_count=3, skipped_cap=15)
    assert len(result.considered_and_skipped) == 15


def test_handles_fewer_items_than_slots_gracefully():
    items = [_item("only1", "https://x.com/1", org_score=50, fluency_score=50)]
    result = select(items, org_count=5, fluency_count=3, skipped_cap=15)
    assert len(result.for_org) == 1
    assert len(result.for_you) == 0
    assert result.considered_and_skipped == []


def test_handles_zero_items():
    result = select([], org_count=5, fluency_count=3, skipped_cap=15)
    assert result.for_org == []
    assert result.for_you == []
    assert result.considered_and_skipped == []


def test_filtered_out_count_passed_through():
    result = select([], filtered_out_count=42)
    assert result.filtered_out_count == 42
