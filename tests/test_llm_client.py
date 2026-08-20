"""llm_client's two behaviours that aren't "call the SDK correctly":
when a failure ends the run, and what gets sent for thinking.

Both are tested with an injected fake client, so no network and no API key --
the transport is stubbed, but the branching under test is the real code.
"""
import httpx
import pytest

import anthropic

from pipeline.api_errors import FatalAPIError
from pipeline.cost import CostTracker
from pipeline.llm_client import run_sync
from pipeline.llm_types import LLMRequest

CREDIT_BALANCE_MESSAGE = (
    "Your credit balance is too low to access the Anthropic API. "
    "Please go to Plans & Billing to upgrade or purchase credits."
)


def _api_error(status: int, message: str) -> anthropic.APIStatusError:
    """A real SDK exception, so the getattr(status_code) extraction in
    llm_client is exercised rather than mocked around."""
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status, request=request)
    cls = {400: anthropic.BadRequestError, 401: anthropic.AuthenticationError}.get(
        status, anthropic.APIStatusError
    )
    return cls(message, response=response, body={"error": {"message": message}})


class _FakeMessage:
    def __init__(self, text: str):
        self.content = [type("Block", (), {"type": "text", "text": text})()]
        self.usage = type("Usage", (), {"input_tokens": 10, "output_tokens": 20})()
        self.stop_reason = "end_turn"


class _FakeClient:
    """Records every create() kwargs and replays a scripted outcome list."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeMessage(outcome)


def _requests(n: int, **kw) -> list[LLMRequest]:
    return [LLMRequest(custom_id=f"score-{i}", prompt=f"p{i}", **kw) for i in range(n)]


def test_credit_balance_error_aborts_instead_of_dropping_the_remaining_items():
    """The Aug 20 regression, exactly: request 3 of 6 hits a zero balance.

    Before the fix, run_sync fired all six, logged three per-item drops, and
    handed a 50%-failed result set downstream. The run must stop at the first
    one, having made exactly 3 calls -- so the three unattempted items are
    never touched and never marked seen.
    """
    client = _FakeClient(["{}", "{}", _api_error(400, CREDIT_BALANCE_MESSAGE), "{}", "{}", "{}"])

    with pytest.raises(FatalAPIError) as excinfo:
        run_sync(_requests(6), "system", "claude-sonnet-5", CostTracker(), client=client)

    assert len(client.calls) == 3, "the loop kept firing requests that could not succeed"
    assert excinfo.value.custom_id == "score-2"
    assert "credit balance" in excinfo.value.reason


def test_authentication_failure_aborts_on_the_very_first_request():
    client = _FakeClient([_api_error(401, "invalid x-api-key")] * 3)
    with pytest.raises(FatalAPIError):
        run_sync(_requests(3), "system", "claude-sonnet-5", CostTracker(), client=client)
    assert len(client.calls) == 1


def test_an_ordinary_per_item_failure_still_degrades_and_the_run_continues():
    """The behaviour that must NOT regress: one bad response about one
    article is still a drop, not an abort."""
    client = _FakeClient(["{}", _api_error(400, "prompt is too long: 245000 tokens"), "{}"])

    results = run_sync(_requests(3), "system", "claude-sonnet-5", CostTracker(), client=client)

    assert len(client.calls) == 3
    assert results["score-1"].text is None
    assert results["score-1"].error is not None
    assert results["score-0"].text == "{}" and results["score-2"].text == "{}"


def test_rate_limit_does_not_abort_the_run():
    """429 clears on its own and the SDK already retried it -- aborting would
    turn a transient throttle into a missed week."""
    client = _FakeClient([_api_error(429, "rate_limit_error"), "{}"])
    results = run_sync(_requests(2), "system", "claude-sonnet-5", CostTracker(), client=client)
    assert len(client.calls) == 2
    assert results["score-0"].text is None


def test_disable_thinking_sends_thinking_disabled():
    client = _FakeClient(["{}"])
    run_sync(
        _requests(1, disable_thinking=True), "system", "claude-sonnet-5",
        CostTracker(), client=client,
    )
    assert client.calls[0]["thinking"] == {"type": "disabled"}


def test_thinking_is_only_ever_disabled_never_enabled():
    """Requests that don't opt out must not carry a thinking parameter at all
    -- the filter stage runs on a model with a different thinking API, and
    sending it one it doesn't accept would 400 the whole stage."""
    client = _FakeClient(["{}"])
    run_sync(_requests(1), "system", "claude-haiku-4-5-20251001", CostTracker(), client=client)
    assert "thinking" not in client.calls[0]
