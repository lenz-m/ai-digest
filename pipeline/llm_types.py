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
