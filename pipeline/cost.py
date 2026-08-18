"""Token/cost accounting and the hard per-run budget ceiling.

Every run -- dry-run or --apply -- tracks actual token usage and prints the
resulting cost, so a pricing change or a batching regression is visible the
next morning, not next quarter's bill. A call that would push cumulative
run cost past CONFIG.cost_ceiling_usd is refused before it's made (via
check_budget(), a pre-flight check using a rough token estimate) rather than
discovered after the fact -- this is the direct fix for a previous version
of this pipeline calling the API per-item and running ~3x over budget
before anyone noticed.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from pipeline.config import CONFIG

# $ per million tokens, (input, output). Batch API is a flat 50% off both,
# applied via the `batch` flag on CallRecord rather than a separate table.
# Last checked against platform.claude.com/docs/en/about-claude/pricing,
# July 2026 -- re-verify if this ever looks wrong, pricing does change.
PRICING_PER_MILLION: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
}

BATCH_DISCOUNT = 0.5

# Rough chars-per-token used ONLY for the pre-flight budget estimate before
# a call is made (we don't know real token counts until the API responds).
# Real accounting always uses actual usage from the response, never this.
CHARS_PER_TOKEN_ESTIMATE = 4


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN_ESTIMATE)


class BudgetExceededError(Exception):
    """Raised by CostTracker.check_budget() when a call would push
    cumulative run cost past the configured ceiling."""


@dataclass(frozen=True)
class CallRecord:
    model: str
    input_tokens: int
    output_tokens: int
    batch: bool = False

    @property
    def cost_usd(self) -> float:
        in_price, out_price = PRICING_PER_MILLION[self.model]
        discount = BATCH_DISCOUNT if self.batch else 1.0
        return (
            self.input_tokens / 1_000_000 * in_price + self.output_tokens / 1_000_000 * out_price
        ) * discount


@dataclass
class CostTracker:
    """Accumulates CallRecords across a run and enforces the hard ceiling."""

    ceiling_usd: float | None = None
    records: list[CallRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.ceiling_usd is None:
            self.ceiling_usd = CONFIG.cost_ceiling_usd

    @property
    def total_cost_usd(self) -> float:
        return sum(r.cost_usd for r in self.records)

    def check_budget(self, estimated_usd: float) -> None:
        """Pre-flight check -- call BEFORE making a call expected to cost
        roughly estimated_usd. Raises rather than letting the run silently
        exceed the ceiling."""
        projected = self.total_cost_usd + estimated_usd
        if projected > self.ceiling_usd:
            raise BudgetExceededError(
                f"call would bring run cost to ${projected:.2f}, over the "
                f"${self.ceiling_usd:.2f} ceiling -- stopping rather than overspending"
            )

    def record(
        self, model: str, input_tokens: int, output_tokens: int, batch: bool = False
    ) -> CallRecord:
        rec = CallRecord(model=model, input_tokens=input_tokens, output_tokens=output_tokens, batch=batch)
        self.records.append(rec)
        return rec

    def report(self) -> str:
        if not self.records:
            return "0 API calls, $0.00 total"
        lines = [f"{len(self.records)} API call(s), ${self.total_cost_usd:.4f} total"]
        by_model: dict[str, list[CallRecord]] = {}
        for r in self.records:
            by_model.setdefault(r.model, []).append(r)
        for model, recs in sorted(by_model.items()):
            in_tok = sum(r.input_tokens for r in recs)
            out_tok = sum(r.output_tokens for r in recs)
            cost = sum(r.cost_usd for r in recs)
            batched = sum(1 for r in recs if r.batch)
            lines.append(
                f"  {model}: {len(recs)} call(s) ({batched} batched), "
                f"{in_tok:,} in / {out_tok:,} out tokens, ${cost:.4f}"
            )
        return "\n".join(lines)
