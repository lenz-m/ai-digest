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
"""


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

  <p class="footer">{len(selection.for_org)} for the org · {len(selection.for_you)} for you · \
{len(selection.considered_and_skipped)} listed in the tail{filtered_note}.</p>
</body>
</html>"""


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
