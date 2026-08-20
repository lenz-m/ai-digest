"""Is this API failure about ONE item, or about the whole account?

Stdlib-only on purpose (no anthropic import), so the classification rules --
the part that can be wrong -- are unit-testable without the SDK or a network
connection, exactly like fetch_strategy.py is to fetch.py. llm_client.py does
the SDK-typed extraction and hands the two primitives here.

WHY THIS EXISTS (measured, 2026-08-20). The Aug 20 --sync run hit a zero
credit balance mid-way through the score stage. Every remaining request came
back `400 invalid_request_error: "Your credit balance is too low..."`, and
score_stage's fail-closed drop treated each one as an ordinary unusable
response about one article. 28 requests bounced instantly (all inside the
same second, 18:34:13) and were logged as 28 individual drops. The score
stage reported a 48% failure rate, which reads like a content problem and is
actually a billing problem.

A per-item drop is the right response to a malformed answer about one
article: the other 59 requests are unaffected. It is the wrong response to an
account-level failure, where "try the next item" cannot possibly work and the
only honest outcome is to stop, say why, and exit non-zero. The degraded-run
floor in deliver.py would have refused to send that run anyway -- but it
would have refused it as "scoring stage degraded", never naming the cause,
and only after the pipeline had walked the whole remaining queue.

WHAT COUNTS AS FATAL. Only failures where no later request in the same run
can succeed, because the account or the configuration is wrong:

  401 authentication   -- the key is bad; it will be bad for request N+1.
  403 permission       -- likewise.
  404 not found        -- the model id doesn't exist (a typo'd
                          AI_DIGEST_MODEL_SCORE); every request names it.
  400 + billing text   -- zero balance. The observed case.

DELIBERATELY NOT FATAL, and this is the interesting half:

  429 rate limit  -- pointless to retry *immediately*, but it clears on its
      own, and the SDK already retried with backoff before the exception
      reached us. Aborting the run on a transient throttle would turn a
      recoverable blip into a missed week.
  5xx server side -- same reasoning: already retried, and a genuinely
      sustained outage is caught by the degraded-run floor.
  400 without billing text -- e.g. a single over-long prompt. That IS about
      one item, and dropping it is correct.

WHY A STRING MATCH IS ACCEPTABLE HERE. The API reports a zero balance as
`invalid_request_error`, the same error type as "your prompt is too long", so
the structured fields cannot separate them -- only the message can. That
makes this match fragile against Anthropic rewording the message, so it is
built as an *optimization on top of a safe default*, not as the only guard:
if the phrase ever changes, the failures degrade to per-item drops, the
failure rate goes over the ceiling, and deliver.py still refuses to send or
commit. We lose the clear diagnosis and the early abort, not correctness.
"""
from __future__ import annotations

# Lowercased substrings that identify a billing/quota refusal in a 400 body.
# Several spellings on purpose -- one wording change shouldn't silently turn
# this back into 28 mystery drops.
_BILLING_PHRASES = (
    "credit balance",
    "purchase credits",
    "plans & billing",
    "billing",
    "insufficient funds",
    "insufficient credit",
    "quota",
)

# Status codes where the account/config is wrong, independent of the message.
_FATAL_STATUS_CODES = frozenset({401, 403, 404})


def is_fatal_api_error(status_code: int | None, message: str) -> bool:
    """True if this failure means every remaining request in the run will
    fail too, so the run should abort rather than drop the item and continue.

    status_code may be None (a connection-level failure with no HTTP status);
    those are treated as per-item -- a dropped TCP connection is exactly the
    kind of thing that succeeds on the next call.
    """
    if status_code in _FATAL_STATUS_CODES:
        return True
    if status_code == 400:
        text = (message or "").lower()
        return any(phrase in text for phrase in _BILLING_PHRASES)
    return False


def fatal_reason(status_code: int | None, message: str) -> str:
    """A short human label for the abort log, so the operator reads
    "top up credits" rather than "400 Bad Request"."""
    if status_code == 401:
        return "authentication failed (bad or missing ANTHROPIC_API_KEY)"
    if status_code == 403:
        return "permission denied for this API key"
    if status_code == 404:
        return "model not found -- check AI_DIGEST_MODEL_FILTER / AI_DIGEST_MODEL_SCORE"
    if status_code == 400:
        return "billing/quota refused the request (credit balance)"
    return f"unrecoverable API error (status {status_code})"


class FatalAPIError(RuntimeError):
    """Raised by llm_client the moment a run-ending failure is seen.

    Carries the custom_id of the request that tripped it purely so the log
    can say where in the queue the run stopped -- callers branch on the type,
    never on the id.
    """

    def __init__(self, reason: str, detail: str, custom_id: str = ""):
        self.reason = reason
        self.detail = detail
        self.custom_id = custom_id
        where = f" (at request {custom_id})" if custom_id else ""
        super().__init__(f"{reason}{where}: {detail}")
