"""Stage 3b/3e: the actual Anthropic API I/O -- Batch API submission,
polling, retrieval, and prompt caching on the shared system prompt.

Deliberately thin, mirroring pipeline/fetch.py's split: all the logic that
can hide bugs (request batching, response parsing, selection) lives in
filter_stage.py / score_stage.py / select.py and is unit-tested there
without needing the anthropic SDK or a network connection. This module is
"call the SDK correctly" and can only be verified by actually running it
against the real API -- not by unit tests against canned responses, since
the anthropic package isn't installed in the dev sandbox this was built in
(same no-network wall documented in CLAUDE.md for httpx/feedparser).

Both LLM passes (filter and score) go through run_batch(): every prompt for
a given stage is submitted as ONE Batch API job (50% off, no latency
pressure on a weekly cadence), never as separate synchronous calls.
"""
from __future__ import annotations

import logging
import time

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

from pipeline.config import CONFIG
from pipeline.cost import CostTracker, estimate_tokens
from pipeline.llm_types import LLMRequest, LLMResult

logger = logging.getLogger(__name__)


class BatchTimeoutError(Exception):
    """The batch didn't reach 'ended' status within CONFIG.batch_max_wait_seconds."""


def _message_diagnostics(message) -> tuple[str, tuple[str, ...]]:
    """Pull the two fields that make an unparseable response diagnosable:
    why the model stopped, and what kinds of content block it produced.

    Concatenating only blocks with a `.text` attribute (below) silently drops
    any other block type. If output tokens went into a block this code can't
    see, the symptom downstream is exactly the Aug 16 contradiction -- a large
    output_tokens count next to a tiny extracted string -- with nothing in the
    log to distinguish that from ordinary truncation.
    """
    return (
        getattr(message, "stop_reason", None),
        tuple(getattr(b, "type", type(b).__name__) for b in message.content),
    )


def _estimate_batch_cost_usd(requests: list[LLMRequest], system_prompt: str, model: str) -> float:
    from pipeline.cost import PRICING_PER_MILLION, BATCH_DISCOUNT

    in_price, out_price = PRICING_PER_MILLION[model]
    total_input = sum(estimate_tokens(system_prompt) + estimate_tokens(r.prompt) for r in requests)
    total_output = sum(r.max_tokens for r in requests)  # worst-case ceiling, not expected usage
    return (total_input / 1_000_000 * in_price + total_output / 1_000_000 * out_price) * BATCH_DISCOUNT


def run_batch(
    requests: list[LLMRequest],
    system_prompt: str,
    model: str,
    cost_tracker: CostTracker,
    client: "anthropic.Anthropic | None" = None,
    on_poll=None,
) -> dict[str, LLMResult]:
    """Submits every request in `requests` as ONE Message Batch job, polls
    until complete, and returns {custom_id: LLMResult}.

    Pre-flight budget check uses a worst-case token estimate (every request
    hitting its max_tokens ceiling) before submitting anything -- real
    per-request usage is recorded into cost_tracker after the batch
    completes, using actual token counts from the API, not the estimate.

    on_poll, if given, is called on each poll with (elapsed_seconds, status)
    -- purely for a console progress line, has no effect on results.
    """
    if not requests:
        return {}

    client = client or anthropic.Anthropic()

    estimated = _estimate_batch_cost_usd(requests, system_prompt, model)
    cost_tracker.check_budget(estimated)  # raises BudgetExceededError if over ceiling

    batch_requests = [
        Request(
            custom_id=req.custom_id,
            params=MessageCreateParamsNonStreaming(
                model=model,
                max_tokens=req.max_tokens,
                system=[
                    {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}
                ],
                messages=[{"role": "user", "content": req.prompt}],
            ),
        )
        for req in requests
    ]

    batch = client.messages.batches.create(requests=batch_requests)
    logger.info("submitted batch %s with %d request(s)", batch.id, len(requests))

    start = time.monotonic()
    deadline = start + CONFIG.batch_max_wait_seconds
    while batch.processing_status != "ended":
        if time.monotonic() > deadline:
            raise BatchTimeoutError(
                f"batch {batch.id} did not finish within {CONFIG.batch_max_wait_seconds}s "
                f"(last status: {batch.processing_status})"
            )
        time.sleep(CONFIG.batch_poll_interval_seconds)
        batch = client.messages.batches.retrieve(batch.id)
        logger.info("batch %s status: %s", batch.id, batch.processing_status)
        if on_poll is not None:
            on_poll(int(time.monotonic() - start), batch.processing_status)

    results: dict[str, LLMResult] = {}
    for entry in client.messages.batches.results(batch.id):
        if entry.result.type == "succeeded":
            message = entry.result.message
            text = "".join(block.text for block in message.content if hasattr(block, "text"))
            in_tok = message.usage.input_tokens
            out_tok = message.usage.output_tokens
            stop_reason, block_types = _message_diagnostics(message)
            cost_tracker.record(model=model, input_tokens=in_tok, output_tokens=out_tok, batch=True)
            results[entry.custom_id] = LLMResult(
                custom_id=entry.custom_id, text=text, input_tokens=in_tok, output_tokens=out_tok,
                stop_reason=stop_reason, content_block_types=block_types,
            )
        else:
            error_detail = getattr(entry.result, "error", None)
            logger.warning("batch request %s did not succeed: %s", entry.custom_id, error_detail)
            results[entry.custom_id] = LLMResult(custom_id=entry.custom_id, text=None, error=str(error_detail))

    return results


def run_sync(
    requests: list[LLMRequest],
    system_prompt: str,
    model: str,
    cost_tracker: CostTracker,
    client: "anthropic.Anthropic | None" = None,
    on_progress=None,
) -> dict[str, LLMResult]:
    """Synchronous alternative to run_batch: fires each request as a normal
    Messages API call and returns immediately, no async queue wait. Full
    price (no 50% batch discount) -- so this is for FAST ITERATION, not the
    weekly production run, which should use run_batch.

    Crucially, this does NOT reintroduce the per-item-filter cost bug: the
    filter stage still hands us only ceil(N/40) already-packed requests, so
    a 45-candidate filter pass is 2 sync calls here, not 45. Only the
    transport differs from run_batch; the request list is identical.

    Same signature contract as run_batch (returns {custom_id: LLMResult}),
    so run.py can swap one for the other. Pre-flight budget check and prompt
    caching on the system prompt both still apply. One failed request logs
    and degrades to an errored LLMResult rather than killing the run.
    """
    if not requests:
        return {}

    client = client or anthropic.Anthropic()

    estimated = _estimate_batch_cost_usd(requests, system_prompt, model) * 2  # undo batch discount for the estimate
    cost_tracker.check_budget(estimated)

    results: dict[str, LLMResult] = {}
    for i, req in enumerate(requests, start=1):
        if on_progress is not None:
            on_progress(i, len(requests))
        try:
            message = client.messages.create(
                model=model,
                max_tokens=req.max_tokens,
                system=[
                    {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}
                ],
                messages=[{"role": "user", "content": req.prompt}],
            )
            text = "".join(block.text for block in message.content if hasattr(block, "text"))
            in_tok = message.usage.input_tokens
            out_tok = message.usage.output_tokens
            stop_reason, block_types = _message_diagnostics(message)
            cost_tracker.record(model=model, input_tokens=in_tok, output_tokens=out_tok, batch=False)
            results[req.custom_id] = LLMResult(
                custom_id=req.custom_id, text=text, input_tokens=in_tok, output_tokens=out_tok,
                stop_reason=stop_reason, content_block_types=block_types,
            )
        except anthropic.AnthropicError as e:  # one bad call must not kill the run
            logger.warning("sync request %s failed: %s", req.custom_id, e)
            results[req.custom_id] = LLMResult(custom_id=req.custom_id, text=None, error=str(e))

    return results
