"""Shared pure types for the LLM stages -- stdlib only, no anthropic SDK
import here, so filter_stage.py and score_stage.py stay fully testable
without a network connection or the SDK installed. pipeline/llm_client.py
is the only module that touches the real API and consumes these types.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LLMRequest:
    """One request destined for a Batch API job. custom_id is how the
    response gets matched back to the request after the batch completes --
    the Anthropic Batch API returns results keyed by custom_id, not in
    submission order.
    """

    custom_id: str
    prompt: str
    max_tokens: int = 1024

    # Turn extended thinking OFF for this request. Set by score_stage; see
    # the comment there. Lives on the request rather than on CONFIG because
    # it is a property of the TASK (structured JSON out, no reasoning needed)
    # and the two stages differ -- llm_client just forwards it to the SDK.
    disable_thinking: bool = False


@dataclass(frozen=True)
class LLMResult:
    """One completed result from a batch. usage is None if the request
    errored (e.g. the model refused, or hit a content filter) -- callers
    should treat that as "no usable output" and degrade, not crash.
    """

    custom_id: str
    text: str | None
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None

    # Diagnostic only, never branched on -- and they did their job. The Aug 16
    # log recorded out_tokens=1000 (exactly score_max_tokens) alongside
    # len(text)=211 chars for the same response, which cannot both describe
    # one plain text response. These two fields settled it on Aug 20:
    # `stop_reason=max_tokens blocks=('thinking', 'text')`. The model was
    # emitting a reasoning block that is billed as output and counted against
    # max_tokens, while the extractor only concatenates `.text`. Hence
    # disable_thinking above. Keep collecting both -- they are how the next
    # unexplained parse failure gets diagnosed from the log instead of guessed.
    stop_reason: str | None = None
    content_block_types: tuple[str, ...] = ()
