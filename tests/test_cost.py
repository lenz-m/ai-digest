from __future__ import annotations

import pytest

from pipeline.cost import BudgetExceededError, CostTracker, estimate_tokens


def test_estimate_tokens_rough_chars_per_token():
    assert estimate_tokens("a" * 400) == 100
    assert estimate_tokens("") == 1  # never zero, avoids div-by-zero-ish edge cases downstream


def test_call_record_cost_standard_pricing():
    tracker = CostTracker(ceiling_usd=100)
    rec = tracker.record(model="claude-sonnet-5", input_tokens=1_000_000, output_tokens=1_000_000)
    assert rec.cost_usd == pytest.approx(3.00 + 15.00)


def test_call_record_cost_batch_discount_applied():
    tracker = CostTracker(ceiling_usd=100)
    rec = tracker.record(
        model="claude-sonnet-5", input_tokens=1_000_000, output_tokens=1_000_000, batch=True
    )
    assert rec.cost_usd == pytest.approx((3.00 + 15.00) * 0.5)


def test_cost_tracker_accumulates_across_records():
    tracker = CostTracker(ceiling_usd=100)
    tracker.record(model="claude-haiku-4-5-20251001", input_tokens=500_000, output_tokens=100_000)
    tracker.record(model="claude-sonnet-5", input_tokens=200_000, output_tokens=50_000)
    expected = (500_000 / 1e6 * 1.00 + 100_000 / 1e6 * 5.00) + (200_000 / 1e6 * 3.00 + 50_000 / 1e6 * 15.00)
    assert tracker.total_cost_usd == pytest.approx(expected)


def test_check_budget_raises_when_over_ceiling():
    tracker = CostTracker(ceiling_usd=1.00)
    tracker.record(model="claude-sonnet-5", input_tokens=200_000, output_tokens=20_000)  # ~$0.90
    with pytest.raises(BudgetExceededError):
        tracker.check_budget(estimated_usd=0.50)  # would push to ~$1.40, over ceiling


def test_check_budget_passes_when_under_ceiling():
    tracker = CostTracker(ceiling_usd=100.00)
    tracker.record(model="claude-sonnet-5", input_tokens=1000, output_tokens=100)
    tracker.check_budget(estimated_usd=0.10)  # should not raise


def test_check_budget_uses_config_default_when_not_specified(monkeypatch):
    from pipeline import config as config_module

    cfg = config_module.Config(cost_ceiling_usd=2.00)
    monkeypatch.setattr(config_module, "CONFIG", cfg)
    monkeypatch.setattr("pipeline.cost.CONFIG", cfg)

    tracker = CostTracker()
    assert tracker.ceiling_usd == 2.00


def test_report_includes_call_count_and_per_model_breakdown():
    tracker = CostTracker(ceiling_usd=100)
    tracker.record(model="claude-haiku-4-5-20251001", input_tokens=1000, output_tokens=100, batch=True)
    tracker.record(model="claude-sonnet-5", input_tokens=2000, output_tokens=200)
    report = tracker.report()
    assert "2 API call(s)" in report
    assert "claude-haiku-4-5-20251001" in report
    assert "claude-sonnet-5" in report


def test_report_handles_zero_calls():
    tracker = CostTracker(ceiling_usd=100)
    assert tracker.report() == "0 API calls, $0.00 total"
