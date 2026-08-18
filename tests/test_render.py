from __future__ import annotations

from datetime import datetime, timezone

from pipeline.dedupe import Candidate
from pipeline.render import render_email_html, render_vault_note, vault_note_filename
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
