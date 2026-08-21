from __future__ import annotations

from datetime import datetime, timezone

from pipeline.dedupe import Candidate
from pipeline.render import (
    FLUENCY_BLURB,
    FLUENCY_HEADER,
    ORG_BLURB,
    ORG_HEADER,
    TAIL_BLURB,
    TAIL_HEADER,
    render_email_html,
    render_email_text,
    render_vault_note,
    vault_note_filename,
)
from pipeline.score_stage import ScoredItem
from pipeline.select import Selection


def _org(title="Org story", vendor=False, so_what="Matters for pricing.",
         url="https://example.com/o"):
    return ScoredItem(
        candidate=Candidate(title=title, url=url, source="Stratechery"),
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


SKIPPED_URL = "https://example.com/skipped-one"


def _sel():
    return Selection(
        for_org=[_org()], for_you=[_flu()],
        considered_and_skipped=[_org("Skipped one", url=SKIPPED_URL)],
        filtered_out_count=37,
    )


NOW = datetime(2026, 8, 16, tzinfo=timezone.utc)


# --- email HTML ---

def test_email_includes_sections_and_items():
    html = render_email_html(_sel(), NOW)
    assert ORG_HEADER in html and FLUENCY_HEADER in html and TAIL_HEADER in html
    assert "Org story" in html and "Fluency story" in html
    assert "https://example.com/o" in html  # linked


def test_email_shows_the_score_so_the_ranking_stays_explainable():
    html = render_email_html(_sel(), NOW)
    assert "82/100" in html
    assert "78/100" in html


def test_email_does_not_render_the_reason_strings():
    """Dropped from the reader-facing output on purpose. The reasons are still
    on ScoredItem and still printed by run.py's console digest -- that is where
    a bad ranking gets diagnosed -- but a per-item "Why it ranked" line
    restated the summary and pushed the content down the page."""
    html = render_email_html(_sel(), NOW)
    assert "Why it ranked" not in html
    assert "Strong strategy signal." not in html
    assert "Real practitioner debate." not in html


def test_no_section_carries_a_reason_line_in_any_format():
    """One assertion covering all three renderers, so re-adding the line to
    just one of them (the drift this whole module guards against) fails."""
    sel = _sel()
    for out in (
        render_email_html(sel, NOW),
        render_email_text(sel, NOW),
        render_vault_note(sel, NOW),
    ):
        assert "Why it ranked" not in out
        assert "Strong strategy signal." not in out
        assert "Real practitioner debate." not in out


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
    assert ORG_HEADER in html  # renders without crashing


# --- section headers + descriptions ---
# The one-liners describe what each section SELECTS FOR (derived from the
# org_score / fluency_score rubrics), which is the part a header alone can't
# carry. All three formats show them, from the same constants.

def test_every_section_description_appears_in_all_three_formats():
    sel = _sel()
    for out in (
        render_email_html(sel, NOW),
        render_email_text(sel, NOW),
        render_vault_note(sel, NOW),
    ):
        for blurb in (ORG_BLURB, FLUENCY_BLURB, TAIL_BLURB):
            assert blurb in out


def test_descriptions_are_one_sentence():
    """The brief was 'one sentence maximum' -- a paragraph under every header
    turns the digest into a manual."""
    for blurb in (ORG_BLURB, FLUENCY_BLURB, TAIL_BLURB):
        assert blurb.count(".") == 1 and blurb.endswith(".")


def test_html_description_is_muted_and_smaller_than_body_text():
    html = render_email_html(_sel(), NOW)
    assert 'class="blurb"' in html
    assert ".blurb { font-size: 12px; color: #777;" in html  # body copy is 14px


def test_old_headers_are_gone_everywhere():
    """They read as opaque ('For you' says nothing about the technical-depth
    bar) or as a dismissal of items we are actually recommending."""
    sel = _sel()
    for out in (
        render_email_html(sel, NOW),
        render_email_text(sel, NOW),
        render_vault_note(sel, NOW),
    ):
        lowered = out.lower()
        assert "for the org" not in lowered
        assert "considered & skipped" not in lowered
        assert "considered and skipped" not in lowered


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
    assert f"## {ORG_HEADER}" in note and f"## {FLUENCY_HEADER}" in note


# --- the tail is a reading list, so it links like the rest ---

def test_tail_items_are_linked_in_all_three_formats():
    sel = _sel()
    assert f'<a href="{SKIPPED_URL}">Skipped one</a>' in render_email_html(sel, NOW)
    assert SKIPPED_URL in render_email_text(sel, NOW)
    assert f"[Skipped one]({SKIPPED_URL})" in render_vault_note(sel, NOW)


# --- plaintext email part ---

def test_email_text_contains_every_item_title_and_url():
    text = render_email_text(_sel(), NOW)
    assert "Org story" in text and "https://example.com/o" in text
    assert "Fluency story" in text and "https://example.com/f" in text
    assert ORG_HEADER.upper() in text and FLUENCY_HEADER.upper() in text


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
