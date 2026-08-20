from __future__ import annotations

from datetime import datetime, timezone
from email import message_from_bytes
from email.policy import default as default_policy

import pytest

from pipeline.dedupe import Candidate
from pipeline.email_build import build_digest_message, parse_addrs, subject_line
from pipeline.render import render_email_html, render_email_text
from pipeline.score_stage import ScoredItem
from pipeline.select import Selection

NOW = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)


def _item(title="Org story", source="Stratechery", url="https://example.com/o"):
    return ScoredItem(
        candidate=Candidate(title=title, url=url, source=source),
        org_score=82, org_reason="Strong strategy signal.",
        fluency_score=40, fluency_reason="Some depth.",
        summary="Sentence one. Sentence two.", so_what="Matters for pricing.",
        vendor_marketing=False, clean_title=title, trust_tier="independent_analysis",
    )


def _sel(**kw):
    base = dict(for_org=[_item()], for_you=[_item("Fluency story", url="https://example.com/f")],
                considered_and_skipped=[], filtered_out_count=12)
    base.update(kw)
    return Selection(**base)


def _built(**kw):
    return build_digest_message(_sel(**kw), NOW, to_addrs=["me@icloud.com"], from_addr="me@icloud.com")


def _roundtrip(msg):
    return message_from_bytes(msg.as_bytes(), policy=default_policy)


# --- subject ---

def test_subject_contains_the_date():
    assert subject_line(NOW) == "AI Digest — Aug 24, 2026"


def test_subject_is_stable_across_content_so_mail_threads_it():
    """Deliberate: a stable subject threads in Mail. Don't 'improve' this by
    prepending the top headline -- that starts a new thread every week."""
    a = build_digest_message(_sel(), NOW, to_addrs=["me@icloud.com"], from_addr="me@icloud.com")
    b = build_digest_message(
        Selection(for_org=[_item("A totally different lead story")], for_you=[], considered_and_skipped=[]),
        NOW, to_addrs=["me@icloud.com"], from_addr="me@icloud.com",
    )
    assert a["Subject"] == b["Subject"]


def test_subject_is_a_single_line():
    subject = subject_line(NOW)
    assert "\r" not in subject and "\n" not in subject


# --- structure ---

def test_message_is_multipart_alternative_with_two_parts():
    msg = _built()
    assert msg.get_content_type() == "multipart/alternative"
    parts = list(msg.iter_parts())
    assert len(parts) == 2


def test_plain_part_comes_first_and_html_second():
    """Clients render the LAST part they understand -- reversing this order
    would silently show everyone the plaintext fallback."""
    parts = list(_built().iter_parts())
    assert parts[0].get_content_type() == "text/plain"
    assert parts[1].get_content_type() == "text/html"


def test_html_part_is_exactly_what_render_email_html_produced():
    parts = list(_built().iter_parts())
    assert parts[1].get_content().rstrip("\n") == render_email_html(_sel(), NOW).rstrip("\n")


def test_plain_part_is_exactly_what_render_email_text_produced():
    parts = list(_built().iter_parts())
    assert parts[0].get_content().rstrip("\n") == render_email_text(_sel(), NOW).rstrip("\n")


def test_headers_set():
    msg = build_digest_message(_sel(), NOW, to_addrs=["a@icloud.com", "b@icloud.com"], from_addr="me@icloud.com")
    assert msg["From"] == "me@icloud.com"
    assert msg["To"] == "a@icloud.com, b@icloud.com"


# --- encoding round-trip: the bugs that actually bite ---

def test_roundtrip_preserves_emoji_and_em_dash_in_both_parts():
    sel = _sel(for_org=[_item("🗞️ Pricing — the new model", source="Économist")])
    msg = build_digest_message(sel, NOW, to_addrs=["me@icloud.com"], from_addr="me@icloud.com")
    parts = list(_roundtrip(msg).iter_parts())
    for part in parts:
        body = part.get_content()
        assert "🗞️" in body
        assert "—" in body
        assert "Économist" in body


def test_roundtrip_survives_long_unbroken_html_lines():
    """Long HTML lines get quoted-printable soft-wrapped in transit; the
    decoded content must come back identical anyway."""
    sel = _sel(for_org=[_item("x" * 400)])
    msg = build_digest_message(sel, NOW, to_addrs=["me@icloud.com"], from_addr="me@icloud.com")
    html_part = list(_roundtrip(msg).iter_parts())[1]
    assert "x" * 400 in html_part.get_content()


def test_a_title_containing_crlf_cannot_inject_a_header():
    sel = _sel(for_org=[_item("Innocent\r\nBcc: attacker@example.com")])
    msg = build_digest_message(sel, NOW, to_addrs=["me@icloud.com"], from_addr="me@icloud.com")
    parsed = _roundtrip(msg)
    assert parsed["Bcc"] is None
    assert "attacker@example.com" not in str(parsed["To"])


# --- degenerate inputs ---

def test_empty_for_you_still_builds_a_valid_message():
    msg = _built(for_you=[])
    assert msg.get_content_type() == "multipart/alternative"
    assert _roundtrip(msg)["Subject"] == "AI Digest — Aug 24, 2026"


def test_missing_recipient_or_sender_raises_before_anything_else():
    with pytest.raises(ValueError, match="no recipients"):
        build_digest_message(_sel(), NOW, to_addrs=[], from_addr="me@icloud.com")
    with pytest.raises(ValueError, match="no sender"):
        build_digest_message(_sel(), NOW, to_addrs=["me@icloud.com"], from_addr="")


# --- address parsing ---

def test_parse_addrs_splits_and_strips():
    assert parse_addrs(" a@x.com , b@x.com ") == ["a@x.com", "b@x.com"]
    assert parse_addrs("") == []
    assert parse_addrs("  ,  ") == []
