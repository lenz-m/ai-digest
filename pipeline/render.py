"""Stage 4a: render a Selection into the reader-facing digest.

Two outputs, both pure functions (no I/O, no network -- fully testable):
  - render_email_html(): the actual email the reader receives. This is NOT
    the run.py debug dump (scores/tiers/raw-titles for our vetting) -- it's
    the clean reader experience, though it still shows the score + one-line
    reason per item, because the spec requires ranking to be *explainable*
    ("show the score and the reason, not a verdict").
  - render_vault_note(): the Obsidian archive note (markdown + frontmatter).

run.py writes both to outbox/ as previews on every (dry-run) run, so the
digest can be opened and vetted before send + vault-archive (the --apply
half of stage 4) exist.
"""
from __future__ import annotations

import html
from datetime import datetime, timezone

from pipeline.select import Selection

_ESC = html.escape


def _fmt_date(dt: datetime) -> str:
    return dt.strftime("%A, %B %-d, %Y")


# --------------------------------------------------------------------------
# Email (HTML)
# --------------------------------------------------------------------------

_STYLE = """
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
         color: #1a1a1a; line-height: 1.5; max-width: 640px; margin: 0 auto; padding: 24px; }
  h1 { font-size: 22px; margin: 0 0 4px; }
  .sub { color: #666; font-size: 13px; margin: 0 0 28px; }
  h2 { font-size: 16px; text-transform: uppercase; letter-spacing: 0.04em; color: #444;
       border-bottom: 2px solid #eee; padding-bottom: 6px; margin: 32px 0 16px; }
  .item { margin: 0 0 22px; }
  .item .title { font-size: 16px; font-weight: 600; margin: 0 0 2px; }
  .item .title a { color: #0b3d91; text-decoration: none; }
  .item .meta { font-size: 12px; color: #888; margin: 0 0 6px; }
  .item .summary { font-size: 14px; margin: 0 0 6px; }
  .item .sowhat { font-size: 14px; margin: 0 0 6px; }
  .item .sowhat b { color: #111; }
  .item .why { font-size: 12px; color: #777; font-style: italic; margin: 0; }
  .badge { display: inline-block; font-size: 11px; font-weight: 600; color: #9a6700;
           background: #fff8e1; border: 1px solid #ffe8a3; border-radius: 3px; padding: 0 5px; margin-left: 6px; }
  .skipped { font-size: 13px; color: #555; }
  .skipped li { margin: 0 0 3px; }
  .footer { color: #999; font-size: 12px; margin-top: 36px; border-top: 1px solid #eee; padding-top: 12px; }
  .opnote { color: #a08000; font-size: 11px; margin: 4px 0 0; }
"""


# --------------------------------------------------------------------------
# Operator diagnostics
# --------------------------------------------------------------------------
#
# WHY these are in the email at all, when a curated digest should not carry
# pipeline diagnostics: there is no reader who isn't the operator. Recipients
# are fixed at one address -- the user's own -- and the alternative home for
# these numbers is a log file written at 6am on an unattended Pi, which is not
# an artifact anyone reads. The email is the only thing reliably read, so it
# is the only place a signal actually lands.
#
# And the degraded-run floor does not cover this. It fires above 30%, which
# makes it a catastrophe detector; the measured steady state is 14/60 (23%)
# and 9/60 (15%). Both send silently under a floor-only rule while quietly
# discarding a seventh to a quarter of everything that got as far as scoring.
#
# Both lines are emitted ONLY when non-zero, so a healthy week reads clean.


def _score_failure_note(selection: Selection) -> str | None:
    """"9 of 60 could not be scored" -- a bare "9 items" is unjudgeable, since
    9-of-12 and 9-of-300 want opposite responses, so the denominator is not
    optional."""
    if not selection.scoring_failed_count:
        return None
    n = selection.scoring_failed_count
    attempted = selection.score_attempted_count
    if attempted:
        return f"⚠ {n} of {attempted} items could not be scored this week and were dropped ({n / attempted:.0%})."
    return f"⚠ {n} items could not be scored this week and were dropped."


def _cap_note(selection: Selection) -> str | None:
    """"60 of 312 that passed the filter were scored (cap)" -- a different
    failure and a different number from the scoring one, so a separate line.
    Per §0.7 this is currently the larger of the two silent losses."""
    passed = selection.filter_passed_count
    scored = selection.score_attempted_count
    if not passed or passed <= scored:
        return None
    return f"⚠ {scored} of {passed} items that passed the filter were scored (max_survivors cap)."


def _org_item_html(item) -> str:
    badge = ' <span class="badge">vendor content</span>' if item.vendor_marketing else ""
    sowhat = f'<p class="sowhat"><b>So what:</b> {_ESC(item.so_what)}</p>' if item.so_what.strip() else ""
    return f"""  <div class="item">
    <p class="title"><a href="{_ESC(item.url)}">{_ESC(item.title)}</a>{badge}</p>
    <p class="meta">{_ESC(item.source)} · org relevance {item.org_score}/100</p>
    <p class="summary">{_ESC(item.summary)}</p>
    {sowhat}
    <p class="why">Why it ranked: {_ESC(item.org_reason)}</p>
  </div>"""


def _fluency_item_html(item) -> str:
    return f"""  <div class="item">
    <p class="title"><a href="{_ESC(item.url)}">{_ESC(item.title)}</a></p>
    <p class="meta">{_ESC(item.source)} · fluency {item.fluency_score}/100</p>
    <p class="summary">{_ESC(item.summary)}</p>
    <p class="why">Why it ranked: {_ESC(item.fluency_reason)}</p>
  </div>"""


def render_email_html(selection: Selection, generated_at: datetime | None = None) -> str:
    generated_at = generated_at or datetime.now(timezone.utc)

    org = "\n".join(_org_item_html(i) for i in selection.for_org) or "  <p>Nothing cleared the bar this week.</p>"
    you = "\n".join(_fluency_item_html(i) for i in selection.for_you) or "  <p>Nothing this week.</p>"

    skipped_items = "\n".join(
        f'    <li>{_ESC(i.title)} <span style="color:#999">— {_ESC(i.source)}</span></li>'
        for i in selection.considered_and_skipped
    )
    skipped = (
        f'  <ul class="skipped">\n{skipped_items}\n  </ul>' if skipped_items else "  <p>None.</p>"
    )

    filtered_note = ""
    if selection.filtered_out_count:
        filtered_note = f" · {selection.filtered_out_count} more filtered below the cut"

    # Operator notes sit after "Considered and skipped", visually de-emphasised
    # -- an operator note, not content. Absent entirely on a healthy week.
    op_notes = "".join(
        f'\n  <p class="opnote">{_ESC(note)}</p>'
        for note in (_score_failure_note(selection), _cap_note(selection))
        if note
    )

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>{_STYLE}</style>
</head>
<body>
  <h1>AI Digest</h1>
  <p class="sub">{_fmt_date(generated_at)}</p>

  <h2>For the org</h2>
{org}

  <h2>For you</h2>
{you}

  <h2>Considered &amp; skipped</h2>
{skipped}
{op_notes}
  <p class="footer">{len(selection.for_org)} for the org · {len(selection.for_you)} for you · \
{len(selection.considered_and_skipped)} listed in the tail{filtered_note}.</p>
</body>
</html>"""


# --------------------------------------------------------------------------
# Email (plain text)
# --------------------------------------------------------------------------

def _org_item_text(item) -> str:
    flag = "  (vendor content)" if item.vendor_marketing else ""
    sowhat = f"\n  So what: {item.so_what}" if item.so_what.strip() else ""
    return (
        f"* {item.title}{flag}\n"
        f"  {item.source} · org relevance {item.org_score}/100\n"
        f"  {item.url}\n"
        f"  {item.summary}{sowhat}\n"
        f"  Why it ranked: {item.org_reason}\n"
    )


def _fluency_item_text(item) -> str:
    return (
        f"* {item.title}\n"
        f"  {item.source} · fluency {item.fluency_score}/100\n"
        f"  {item.url}\n"
        f"  {item.summary}\n"
        f"  Why it ranked: {item.fluency_reason}\n"
    )


def render_email_text(selection: Selection, generated_at: datetime | None = None) -> str:
    """The text/plain alternative part of the email.

    Lives here rather than in email_build.py so all rendering stays in one
    tested module and email_build.py stays pure MIME. Deliberately NOT the
    vault note: that carries YAML frontmatter, which is right in an Obsidian
    file and noise in an email body.
    """
    generated_at = generated_at or datetime.now(timezone.utc)

    org = "\n".join(_org_item_text(i) for i in selection.for_org) or "Nothing cleared the bar this week.\n"
    you = "\n".join(_fluency_item_text(i) for i in selection.for_you) or "Nothing this week.\n"
    skipped = "\n".join(
        f"- {i.title} — {i.source}" for i in selection.considered_and_skipped
    ) or "None."

    filtered_note = ""
    if selection.filtered_out_count:
        filtered_note = f" · {selection.filtered_out_count} more filtered below the cut"

    op_notes = "".join(
        f"\n{note}"
        for note in (_score_failure_note(selection), _cap_note(selection))
        if note
    )

    return (
        f"AI DIGEST\n{_fmt_date(generated_at)}\n\n"
        f"FOR THE ORG\n{'-' * 40}\n\n{org}\n"
        f"FOR YOU\n{'-' * 40}\n\n{you}\n"
        f"CONSIDERED & SKIPPED\n{'-' * 40}\n\n{skipped}\n"
        f"{op_notes}\n"
        f"\n{len(selection.for_org)} for the org · {len(selection.for_you)} for you · "
        f"{len(selection.considered_and_skipped)} listed in the tail{filtered_note}.\n"
    )


# --------------------------------------------------------------------------
# Obsidian vault note (Markdown + frontmatter)
# --------------------------------------------------------------------------

def vault_note_filename(generated_at: datetime) -> str:
    """`🗞️ AI Digest YYYY-MM-DD.md` -- type-emoji prefix per the vault's
    existing filename convention; lands in the Digests/ folder."""
    return f"🗞️ AI Digest {generated_at.strftime('%Y-%m-%d')}.md"


def _md_org_item(item) -> str:
    flag = " *(vendor content)*" if item.vendor_marketing else ""
    sowhat = f"\n**So what:** {item.so_what}" if item.so_what.strip() else ""
    return (
        f"### [{item.title}]({item.url}){flag}\n"
        f"*{item.source} · org relevance {item.org_score}/100*\n\n"
        f"{item.summary}{sowhat}\n\n"
        f"> Why it ranked: {item.org_reason}\n"
    )


def _md_fluency_item(item) -> str:
    return (
        f"### [{item.title}]({item.url})\n"
        f"*{item.source} · fluency {item.fluency_score}/100*\n\n"
        f"{item.summary}\n\n"
        f"> Why it ranked: {item.fluency_reason}\n"
    )


def render_vault_note(selection: Selection, generated_at: datetime | None = None) -> str:
    generated_at = generated_at or datetime.now(timezone.utc)
    date_str = generated_at.strftime("%Y-%m-%d")

    org = "\n".join(_md_org_item(i) for i in selection.for_org) or "_Nothing cleared the bar this week._"
    you = "\n".join(_md_fluency_item(i) for i in selection.for_you) or "_Nothing this week._"
    skipped = "\n".join(
        f"- {i.title} — {i.source}" for i in selection.considered_and_skipped
    ) or "_None._"

    frontmatter = (
        "---\n"
        "type: ai-digest\n"
        f"date: {date_str}\n"
        "---\n"
    )
    return (
        f"{frontmatter}\n"
        f"# 🗞️ AI Digest — {date_str}\n\n"
        f"## For the org\n\n{org}\n\n"
        f"## For you\n\n{you}\n\n"
        f"## Considered & skipped\n\n{skipped}\n"
    )
