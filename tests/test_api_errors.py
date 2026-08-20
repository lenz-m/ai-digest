"""The per-item vs run-ending classification.

The regression these guard is specific and measured: on 2026-08-20 a zero
credit balance produced 28 identical `400 invalid_request_error` responses
inside one second, and every one was recorded as an ordinary "could not use
this response about one article" drop. See pipeline/api_errors.py.
"""
from pipeline.api_errors import FatalAPIError, fatal_reason, is_fatal_api_error

# Verbatim from logs/run-20260820-182636.log, so a reworded copy in the test
# can't drift away from the real body the classifier has to recognise.
REAL_CREDIT_BALANCE_ERROR = (
    "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
    "'message': 'Your credit balance is too low to access the Anthropic API. "
    "Please go to Plans & Billing to upgrade or purchase credits.'}, "
    "'request_id': 'req_011CeEskEjiDBtorzZzccX9X'}"
)


def test_the_actual_aug_20_credit_balance_error_is_fatal():
    assert is_fatal_api_error(400, REAL_CREDIT_BALANCE_ERROR) is True


def test_authentication_and_permission_and_bad_model_are_fatal():
    # No message needed -- the status alone settles these.
    assert is_fatal_api_error(401, "") is True
    assert is_fatal_api_error(403, "") is True
    assert is_fatal_api_error(404, "model 'claude-sonnet-6' not found") is True


def test_a_400_about_one_item_is_NOT_fatal():
    """The distinction the whole module exists for: same status code, same
    error type, opposite correct response. An over-long prompt is about one
    article and must stay a per-item drop."""
    over_long = (
        "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
        "'message': 'prompt is too long: 245000 tokens > 200000 maximum'}}"
    )
    assert is_fatal_api_error(400, over_long) is False


def test_transient_failures_are_NOT_fatal():
    """429 and 5xx clear on their own and the SDK has already retried them.
    Aborting the run on a throttle would turn a recoverable blip into a
    missed week; a sustained outage is caught by deliver.py's floor instead."""
    assert is_fatal_api_error(429, "rate_limit_error: too many requests") is False
    assert is_fatal_api_error(500, "internal server error") is False
    assert is_fatal_api_error(529, "overloaded_error") is False


def test_connection_level_failure_with_no_status_is_not_fatal():
    assert is_fatal_api_error(None, "Connection error.") is False


def test_billing_match_is_case_insensitive():
    assert is_fatal_api_error(400, "YOUR CREDIT BALANCE IS TOO LOW") is True


def test_fatal_reason_names_the_operator_action():
    assert "credit balance" in fatal_reason(400, REAL_CREDIT_BALANCE_ERROR)
    assert "ANTHROPIC_API_KEY" in fatal_reason(401, "")
    assert "AI_DIGEST_MODEL_SCORE" in fatal_reason(404, "")


def test_fatal_api_error_message_says_where_the_run_stopped():
    e = FatalAPIError("billing/quota refused the request", "credit balance too low", "score-32")
    assert "score-32" in str(e)
    assert e.custom_id == "score-32"
