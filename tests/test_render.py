from __future__ import annotations

from datetime import datetime, timezone

from pipeline.dedupe import Candidate
from pipeline.render import (
    render_email_html,
    render_email_text,
    render_vault_note,
    vault_note_filename,
)
from pipeline.score_stage import ScoredItem
from pipeline.select import Selection


def _org(title="Org story", vendor=False, so_what="Matters for pricing."):
    return ScoredItem(
        candidate=Candidate(title=title, url="https://example.com/o", source="Stratechery"),
        org_score=82, org_reason="Strong strategy signal.",
        fluency_score=10, fluency_reason="", summary="Sentence one. Sentence two.",
        so_what=so_what, vendor_marketing=vendor, clean_title=title, trust_tier="independent_analysis",
    )


def _flu(title="Fluency story"):
    return ScoredItem(
        candidate=Candidate(title=title, url="https://example.com/f", source="AI Newsletter"),
        org_score=5, org_reason="", fluency_score=78, fluency_reason="Real practitioner debate.",
        summary="Sentence one. Sentence two.", so_what="", vendor_marketing=False,
        clean_title=title, trust_tier="independent_news",
    )


def _sel():
    return Selection(
        for_org=[_org()], for_you=[_flu()],
        considered_and_skipped=[_org("Skipped one")], filtered_out_count=37,
    )


NOW = datetime(2026, 8, 16, tzinfo=timezone.utc)


# --- email HTML ---

def test_email_includes_sections_and_items():
    html = render_email_html(_sel(), NOW)
    assert "For the org" in html and "For you" in html and "Considered" in html
    assert "Org story" in html and "Fluency story" in html
    assert "https://example.com/o" in html  # linked


def test_email_shows_score_and_reason_not_just_verdict():
    html = render_email_html(_sel(), NOW)
    assert "82/100" in html and "Strong strategy signal." in html
    assert "78/100" in html and "Real practitioner debate." in html


def test_email_shows_so_what_for_org_only():
    html = render_email_html(_sel(), NOW)
    assert "So what:" in html
    assert "Matters for pricing." in html


def test_email_vendor_badge_only_when_flagged():
    assert "vendor content" not in render_email_html(_sel(), NOW)
    vendor_sel = Selection(for_org=[_org(vendor=True)], for_you=[], considered_and_skipped=[])
    assert "vendor content" in render_email_html(vendor_sel, NOW)


def test_email_escapes_html_in_titles():
    sel = Selection(for_org=[_org(title="A <script> & B")], for_you=[], considered_and_skipped=[])
    html = render_email_html(sel, NOW)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html or "&amp;" in html


def test_email_handles_empty_sections_gracefully():
    empty = Selection(for_org=[], for_you=[], considered_and_skipped=[])
    html = render_email_html(empty, NOW)
    assert "For the org" in html  # renders without crashing


# --- vault note ---

def test_vault_note_frontmatter_and_type():
    note = render_vault_note(_sel(), NOW)
    assert note.startswith("---\n")
    assert "type: ai-digest" in note
    assert "date: 2026-08-16" in note


def test_vault_note_filename_convention():
    assert vault_note_filename(NOW) == "🗞️ AI Digest 2026-08-16.md"


def test_vault_note_includes_items_as_markdown_links():
    note = render_vault_note(_sel(), NOW)
    assert "[Org story](https://example.com/o)" in note
    assert "## For the org" in note and "## For you" in note


# --- plaintext email part ---

def test_email_text_contains_every_item_title_and_url():
    text = render_email_text(_sel(), NOW)
    assert "Org story" in text and "https://example.com/o" in text
    assert "Fluency story" in text and "https://example.com/f" in text
    assert "FOR THE ORG" in text and "FOR YOU" in text


def test_email_text_has_no_yaml_frontmatter():
    """Right in an Obsidian note, noise in an email body."""
    text = render_email_text(_sel(), NOW)
    assert not text.startswith("---")
    assert "type: ai-digest" not in text


def test_email_text_handles_empty_sections():
    empty = Selection(for_org=[], for_you=[], considered_and_skipped=[])
    text = render_email_text(empty, NOW)
    assert "Nothing cleared the bar this week." in text


# --- operator diagnostics in the footer (§0.6) ---
# A healthy week carries no diagnostics at all; a lossy one says so, with the
# denominator, in BOTH parts -- the email is the only artifact reliably read,
# and the degraded-run floor at 30% doesn't fire on the 15-23% steady state.

def _lossy_sel(failed=9, attempted=60, passed=312):
    return Selection(
        for_org=[_org()], for_you=[_flu()], considered_and_skipped=[],
        filtered_out_count=129,
        scoring_failed_count=failed, score_attempted_count=attempted,
        filter_passed_count=passed,
    )


def test_healthy_week_has_no_operator_line_in_either_part():
    healthy = Selection(
        for_org=[_org()], for_you=[_flu()], considered_and_skipped=[],
        filtered_out_count=129,
        scoring_failed_count=0, score_attempted_count=60, filter_passed_count=60,
    )
    assert "could not be scored" not in render_email_html(healthy, NOW)
    assert "could not be scored" not in render_email_text(healthy, NOW)
    assert "max_survivors" not in render_email_html(healthy, NOW)
    assert "max_survivors" not in render_email_text(healthy, NOW)


def test_scoring_failures_reported_with_denominator_in_both_parts():
    html = render_email_html(_lossy_sel(), NOW)
    text = render_email_text(_lossy_sel(), NOW)
    for out in (html, text):
        assert "9 of 60 items could not be scored" in out
        assert "15%" in out


def test_cap_loss_reported_separately_from_scoring_loss():
    """Different failure, different number -- not folded into one line."""
    html = render_email_html(_lossy_sel(), NOW)
    text = render_email_text(_lossy_sel(), NOW)
    for out in (html, text):
        assert "60 of 312 items that passed the filter were scored" in out


def test_cap_line_absent_when_the_cap_did_not_bind():
    sel = Selection(
        for_org=[_org()], for_you=[], considered_and_skipped=[],
        score_attempted_count=40, filter_passed_count=40,
    )
    assert "max_survivors" not in render_email_html(sel, NOW)
    assert "max_survivors" not in render_email_text(sel, NOW)


def test_filtered_out_count_now_means_filter_rejects_only():
    """It is shown to the reader as successful curation, so a scoring
    breakage must never be counted into it."""
    sel = _lossy_sel()
    assert "129 more filtered below the cut" in render_email_html(sel, NOW)
