"""Stage 4b: build the digest email. Pure MIME -- no sockets, no config
lookups at call time, no I/O of any kind.

Split from send.py for the same reason fetch_strategy.py is split from
fetch.py: everything that can hide a bug (part ordering, encoding, header
construction) is testable without a server, and what's left in send.py is
"open a socket and call smtplib correctly".

The message is multipart/alternative with text/plain FIRST and text/html
SECOND. That order is not cosmetic -- a mail client renders the LAST part it
understands, so reversing it would show every recipient the plaintext
fallback.
"""
from __future__ import annotations

from datetime import datetime
from email.message import EmailMessage

from pipeline.render import render_email_html, render_email_text
from pipeline.select import Selection


def parse_addrs(raw: str) -> list[str]:
    """Comma-separated -> list. There is one recipient today; keeping the
    config value a list means adding someone later is env-only, with no Bcc
    branch to maintain."""
    return [a.strip() for a in raw.split(",") if a.strip()]


def subject_line(generated_at: datetime) -> str:
    """`AI Digest — Aug 24, 2026`.

    Deliberately stable week to week, and worth defending against a future
    "improvement": a stable subject THREADS in Mail, which matters more as the
    archive grows. Prepending the top headline would start a new thread every
    week and scatter the archive.
    """
    subject = f"AI Digest — {generated_at.strftime('%b %-d, %Y')}"
    # Belt and braces. The date can't contain a newline, but a header is the
    # one place where a stray CR/LF stops being a formatting problem and
    # becomes header injection -- so it is stripped at the boundary, not
    # assumed away.
    return subject.replace("\r", " ").replace("\n", " ")


def build_digest_message(
    selection: Selection,
    generated_at: datetime,
    *,
    to_addrs: list[str],
    from_addr: str,
) -> EmailMessage:
    """Build the finished multipart/alternative message.

    from_addr MUST be an address the sending iCloud account actually owns.
    An unrecognised From gets a hard 550 from iCloud -- not a retryable
    error, and the likeliest first-run failure.
    """
    if not to_addrs:
        raise ValueError("no recipients: set AI_DIGEST_TO or pass --to")
    if not from_addr:
        raise ValueError("no sender: set AI_DIGEST_FROM (must be an address the iCloud account owns)")

    msg = EmailMessage()
    msg["Subject"] = subject_line(generated_at)
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)

    # set_content then add_alternative gives plain-then-html in that order.
    msg.set_content(render_email_text(selection, generated_at))
    msg.add_alternative(render_email_html(selection, generated_at), subtype="html")
    return msg
