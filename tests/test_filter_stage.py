from __future__ import annotations

import json
import math

import pytest

from pipeline.dedupe import Candidate
from pipeline.filter_stage import (
    build_filter_prompt,
    build_filter_requests,
    parse_filter_response,
    parse_filter_results,
)
from pipeline.llm_types import LLMResult


def _candidates(n: int) -> list[Candidate]:
    # Zero-padded index deliberately: "Story 4" is a substring of "Story 40"
    # through "Story 49", which produced a false-positive in
    # test_every_candidate_appears_in_exactly_one_batch before this was
    # padded -- caught by the sandbox smoke test, not a real production bug.
    return [
        Candidate(
            title=f"Story {i:04d}", url=f"https://example.com/{i}", source="Feed", excerpt=f"excerpt {i}"
        )
        for i in range(n)
    ]


# --- THE regression test: N candidates must never become N API requests ---

@pytest.mark.parametrize("n_candidates,batch_size", [(1, 40), (40, 40), (41, 40), (97, 40), (445, 40), (1038, 40)])
def test_n_candidates_never_produce_n_requests(n_candidates, batch_size):
    candidates = _candidates(n_candidates)
    requests = build_filter_requests(candidates, batch_size=batch_size)
    expected = math.ceil(n_candidates / batch_size)
    assert len(requests) == expected, (
        f"{n_candidates} candidates produced {len(requests)} requests, expected {expected} "
        f"-- this is exactly the per-item-call regression that blew the budget before"
    )
    if n_candidates > batch_size:
        assert len(requests) < n_candidates


def test_zero_candidates_produce_zero_requests():
    assert build_filter_requests([], batch_size=40) == []


def test_every_candidate_appears_in_exactly_one_batch():
    candidates = _candidates(97)
    requests = build_filter_requests(candidates, batch_size=40)
    # every candidate's title should appear in exactly one request's prompt
    for c in candidates:
        appearances = sum(1 for r in requests if c.title in r.prompt)
        assert appearances == 1, f"{c.title!r} appeared in {appearances} prompts, expected 1"


def test_request_prompts_pack_multiple_candidates_together():
    candidates = _candidates(5)
    requests = build_filter_requests(candidates, batch_size=40)
    assert len(requests) == 1
    for c in candidates:
        assert c.title in requests[0].prompt


# --- build_filter_prompt formatting ---

def test_build_filter_prompt_includes_id_source_title_excerpt():
    batch = [(0, Candidate(title="AI thing happens", url="https://x.com/1", source="TechMeme", excerpt="details here"))]
    prompt = build_filter_prompt(batch)
    assert "0." in prompt
    assert "[TechMeme]" in prompt
    assert "AI thing happens" in prompt
    assert "details here" in prompt


def test_build_filter_prompt_handles_missing_excerpt():
    batch = [(3, Candidate(title="No excerpt here", url="https://x.com/1", source="Blog"))]
    prompt = build_filter_prompt(batch)
    assert "3." in prompt
    assert "No excerpt here" in prompt


# --- parse_filter_response ---

def test_parse_filter_response_valid_json_array():
    text = json.dumps([{"id": 0, "pass": True, "reason": "relevant"}, {"id": 1, "pass": False, "reason": "off-topic"}])
    parsed = parse_filter_response(text, expected_ids={0, 1})
    assert parsed == {0: (True, "relevant"), 1: (False, "off-topic")}


def test_parse_filter_response_strips_markdown_code_fence():
    text = '```json\n[{"id": 0, "pass": true, "reason": "ok"}]\n```'
    parsed = parse_filter_response(text, expected_ids={0})
    assert parsed == {0: (True, "ok")}


def test_parse_filter_response_malformed_json_returns_empty():
    assert parse_filter_response("not json at all", expected_ids={0, 1}) == {}


def test_parse_filter_response_ignores_ids_outside_expected_set():
    text = json.dumps([{"id": 99, "pass": True, "reason": "x"}])
    assert parse_filter_response(text, expected_ids={0, 1}) == {}


def test_parse_filter_response_skips_malformed_entries_keeps_valid_ones():
    text = json.dumps([{"id": 0, "pass": True, "reason": "ok"}, {"not_id": 1}, {"id": 2, "pass": False, "reason": "no"}])
    parsed = parse_filter_response(text, expected_ids={0, 1, 2})
    assert parsed == {0: (True, "ok"), 2: (False, "no")}


# --- parse_filter_results: fail-open behavior + mapping back to candidates ---

def test_parse_filter_results_maps_verdicts_back_to_candidates():
    candidates = _candidates(3)
    requests = build_filter_requests(candidates, batch_size=40)
    response_text = json.dumps(
        [{"id": 0, "pass": True, "reason": "a"}, {"id": 1, "pass": False, "reason": "b"}, {"id": 2, "pass": True, "reason": "c"}]
    )
    results = {requests[0].custom_id: LLMResult(custom_id=requests[0].custom_id, text=response_text, input_tokens=10, output_tokens=10)}
    verdicts = parse_filter_results(requests, candidates, results, batch_size=40)
    assert [v.passed for v in verdicts] == [True, False, True]
    assert verdicts[0].candidate == candidates[0]


def test_parse_filter_results_fails_open_on_missing_batch_result():
    candidates = _candidates(2)
    requests = build_filter_requests(candidates, batch_size=40)
    verdicts = parse_filter_results(requests, candidates, results={}, batch_size=40)
    assert all(v.passed for v in verdicts), "unparsed/missing results should pass through, not be silently dropped"


def test_parse_filter_results_fails_open_on_errored_batch():
    candidates = _candidates(2)
    requests = build_filter_requests(candidates, batch_size=40)
    results = {requests[0].custom_id: LLMResult(custom_id=requests[0].custom_id, text=None, error="rate limited")}
    verdicts = parse_filter_results(requests, candidates, results, batch_size=40)
    assert all(v.passed for v in verdicts)


def test_parse_filter_results_fails_open_on_partial_response_missing_an_id():
    candidates = _candidates(3)
    requests = build_filter_requests(candidates, batch_size=40)
    # model only returned verdicts for id 0 and 2, dropped id 1
    response_text = json.dumps([{"id": 0, "pass": False, "reason": "a"}, {"id": 2, "pass": False, "reason": "c"}])
    results = {requests[0].custom_id: LLMResult(custom_id=requests[0].custom_id, text=response_text, input_tokens=1, output_tokens=1)}
    verdicts = parse_filter_results(requests, candidates, results, batch_size=40)
    assert verdicts[0].passed is False
    assert verdicts[1].passed is True  # missing from response -> failed open
    assert verdicts[2].passed is False
