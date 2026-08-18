from __future__ import annotations

import json

import pytest

from pipeline.dedupe import Candidate
from pipeline.llm_types import LLMResult
from pipeline.score_stage import (
    build_score_prompt,
    build_score_requests,
    parse_score_response,
    parse_score_results,
)


def _candidate(name="Story", url="https://example.com/1") -> Candidate:
    return Candidate(title=name, url=url, source="Feed")


VALID_RESPONSE = {
    "clean_title": "How to Govern Gemini at Scale",
    "org_score": 80,
    "org_reason": "Directly about delivery pricing",
    "fluency_score": 40,
    "fluency_reason": "Some technical depth",
    "summary": "Sentence one. Sentence two.",
    "so_what": "Pricing models need revisiting.",
    "vendor_marketing": False,
}


# --- clean_title: fixes listing-scrape chrome glued onto headlines ---

def test_scored_item_prefers_clean_title_for_display():
    survivors = [
        Candidate(
            title="Data AnalyticsHow to Govern Gemini at ScaleBy Jane Doe • 9-minute read",
            url="https://example.com/1",
            source="GCP",
        )
    ]
    requests = build_score_requests(survivors, {})
    results = {
        requests[0].custom_id: LLMResult(
            custom_id=requests[0].custom_id, text=json.dumps(VALID_RESPONSE), input_tokens=1, output_tokens=1
        )
    }
    scored = parse_score_results(requests, survivors, results)
    assert scored[0].title == "How to Govern Gemini at Scale"
    # raw title preserved for auditing what the cleanup changed
    assert scored[0].raw_title.startswith("Data Analytics")


def test_scored_item_falls_back_to_raw_title_when_clean_title_missing():
    survivors = [Candidate(title="An Already Clean Headline", url="https://example.com/1", source="Feed")]
    requests = build_score_requests(survivors, {})
    resp = dict(VALID_RESPONSE)
    del resp["clean_title"]
    results = {
        requests[0].custom_id: LLMResult(
            custom_id=requests[0].custom_id, text=json.dumps(resp), input_tokens=1, output_tokens=1
        )
    }
    scored = parse_score_results(requests, survivors, results)
    assert scored[0].title == "An Already Clean Headline"


def test_scored_item_falls_back_when_clean_title_is_blank():
    survivors = [Candidate(title="Fallback Headline", url="https://example.com/1", source="Feed")]
    requests = build_score_requests(survivors, {})
    results = {
        requests[0].custom_id: LLMResult(
            custom_id=requests[0].custom_id,
            text=json.dumps(dict(VALID_RESPONSE, clean_title="   ")),
            input_tokens=1,
            output_tokens=1,
        )
    }
    scored = parse_score_results(requests, survivors, results)
    assert scored[0].title == "Fallback Headline"


# --- build_score_prompt ---

def test_build_score_prompt_includes_source_title_url_and_text():
    c = _candidate()
    prompt = build_score_prompt(c, "Full article body text here.")
    assert "Story" in prompt
    assert "https://example.com/1" in prompt
    assert "Feed" in prompt
    assert "Full article body text here." in prompt


def test_build_score_prompt_truncates_long_article_text(monkeypatch):
    from pipeline import config as config_module

    cfg = config_module.Config(article_text_max_chars=50)
    monkeypatch.setattr(config_module, "CONFIG", cfg)
    monkeypatch.setattr("pipeline.score_stage.CONFIG", cfg)

    prompt = build_score_prompt(_candidate(), "x" * 500)
    assert "x" * 50 in prompt
    assert "x" * 500 not in prompt
    assert "..." in prompt


def test_build_score_prompt_handles_no_article_text():
    prompt = build_score_prompt(_candidate(), "")
    assert "no article text available" in prompt.lower()


# --- build_score_requests: one request per survivor, never packed ---

def test_build_score_requests_one_per_survivor():
    survivors = [_candidate(f"Story {i}", f"https://example.com/{i}") for i in range(5)]
    texts = {c.url: f"text for {c.title}" for c in survivors}
    requests = build_score_requests(survivors, texts)
    assert len(requests) == 5
    assert len({r.custom_id for r in requests}) == 5  # all unique


def test_build_score_requests_missing_article_text_defaults_empty():
    survivors = [_candidate()]
    requests = build_score_requests(survivors, article_texts={})
    assert "no article text available" in requests[0].prompt.lower()


# --- trust tier integration ---

class _FakeTrustStore:
    def __init__(self, tier="vendor", desc="a vendor-published source"):
        self._tier = tier
        self._desc = desc

    def describe(self, source):
        return self._desc

    def get_tier(self, source):
        return self._tier


def test_build_score_requests_injects_trust_tier_into_prompt():
    survivors = [_candidate()]
    requests = build_score_requests(
        survivors, {}, trust_store=_FakeTrustStore(desc="a vendor-published source")
    )
    assert "Source trust tier: a vendor-published source" in requests[0].prompt


def test_build_score_requests_no_trust_store_omits_tier_line():
    survivors = [_candidate()]
    requests = build_score_requests(survivors, {})
    assert "Source trust tier:" not in requests[0].prompt


def test_parse_score_results_stamps_trust_tier_on_item():
    survivors = [_candidate()]
    requests = build_score_requests(survivors, {})
    results = {
        requests[0].custom_id: LLMResult(
            custom_id=requests[0].custom_id, text=json.dumps(VALID_RESPONSE), input_tokens=1, output_tokens=1
        )
    }
    scored = parse_score_results(requests, survivors, results, trust_store=_FakeTrustStore(tier="vendor"))
    assert scored[0].trust_tier == "vendor"


# --- parse_score_response ---

def test_parse_score_response_valid():
    parsed = parse_score_response(json.dumps(VALID_RESPONSE))
    assert parsed["org_score"] == 80
    assert parsed["fluency_score"] == 40
    assert parsed["vendor_marketing"] is False
    assert parsed["summary"] == "Sentence one. Sentence two."


def test_parse_score_response_strips_code_fence():
    text = "```json\n" + json.dumps(VALID_RESPONSE) + "\n```"
    parsed = parse_score_response(text)
    assert parsed is not None
    assert parsed["org_score"] == 80


def test_parse_score_response_malformed_returns_none():
    assert parse_score_response("not json") is None


def test_parse_score_response_recovers_object_wrapped_in_prose():
    # The model sometimes prepends a sentence before the JSON; strict
    # json.loads on the whole string would reject it, but the balanced-brace
    # fallback recovers the object.
    text = "Here is my analysis of the article:\n" + json.dumps(VALID_RESPONSE) + "\nLet me know if you need more."
    parsed = parse_score_response(text)
    assert parsed is not None
    assert parsed["org_score"] == 80


def test_parse_score_response_ignores_braces_inside_string_values():
    resp = dict(VALID_RESPONSE, summary="Uses a dict literal {like this} in prose. Second sentence.")
    parsed = parse_score_response(json.dumps(resp))
    assert parsed is not None
    assert "{like this}" in parsed["summary"]


def test_parse_score_response_truncated_json_still_returns_none():
    # A response cut off mid-object (the real bug from the first run) has no
    # balanced closing brace -- must still fail closed, not return partial.
    truncated = json.dumps(VALID_RESPONSE)[:60]
    assert parse_score_response(truncated) is None


def test_parse_score_response_clamps_out_of_range_scores():
    bad = dict(VALID_RESPONSE, org_score=150, fluency_score=-20)
    parsed = parse_score_response(json.dumps(bad))
    assert parsed["org_score"] == 100
    assert parsed["fluency_score"] == 0


def test_parse_score_response_missing_fields_default_sanely():
    parsed = parse_score_response(json.dumps({}))
    assert parsed["org_score"] == 0
    assert parsed["fluency_score"] == 0
    assert parsed["summary"] == ""
    assert parsed["vendor_marketing"] is False


# --- parse_score_results: drop-on-failure (unlike filter's fail-open) ---

def test_parse_score_results_builds_scored_items():
    survivors = [_candidate()]
    requests = build_score_requests(survivors, {})
    results = {
        requests[0].custom_id: LLMResult(
            custom_id=requests[0].custom_id, text=json.dumps(VALID_RESPONSE), input_tokens=10, output_tokens=10
        )
    }
    scored = parse_score_results(requests, survivors, results)
    assert len(scored) == 1
    assert scored[0].org_score == 80
    # VALID_RESPONSE includes clean_title; display title prefers it over raw scrape
    assert scored[0].title == VALID_RESPONSE["clean_title"]
    assert scored[0].raw_title == "Story"


def test_parse_score_results_drops_item_on_missing_result():
    survivors = [_candidate()]
    requests = build_score_requests(survivors, {})
    scored = parse_score_results(requests, survivors, results={})
    assert scored == [], "a missing result should drop the item, not fabricate scores"


def test_parse_score_results_drops_item_on_errored_request():
    survivors = [_candidate()]
    requests = build_score_requests(survivors, {})
    results = {requests[0].custom_id: LLMResult(custom_id=requests[0].custom_id, text=None, error="timeout")}
    scored = parse_score_results(requests, survivors, results)
    assert scored == []


def test_parse_score_results_drops_item_on_unparsable_response():
    survivors = [_candidate()]
    requests = build_score_requests(survivors, {})
    results = {requests[0].custom_id: LLMResult(custom_id=requests[0].custom_id, text="garbage", input_tokens=1, output_tokens=1)}
    scored = parse_score_results(requests, survivors, results)
    assert scored == []


def test_parse_score_results_partial_batch_keeps_the_good_ones():
    survivors = [_candidate("A", "https://x.com/a"), _candidate("B", "https://x.com/b")]
    requests = build_score_requests(survivors, {})
    results = {
        requests[0].custom_id: LLMResult(custom_id=requests[0].custom_id, text=json.dumps(VALID_RESPONSE), input_tokens=1, output_tokens=1),
        # requests[1] has no result at all
    }
    scored = parse_score_results(requests, survivors, results)
    assert len(scored) == 1
    # This test is about partial-batch survival, not title cleanup — match on url
    assert scored[0].url == "https://x.com/a"
