#!/usr/bin/env python3
"""Email an alert when the weekly digest run fails.

Wired up as `OnFailure=ai-digest-notify@%n.service` on ai-digest.service.

WHY THIS EXISTS: the only symptom this system has ever had for a failure is
the ABSENCE of an email. The 2026-09-07 run spent two hours polling a score
batch that had in fact succeeded, exited 1, and nobody found out for four
days. The 2026-08-31 run died in the same place and went unnoticed entirely.
A missing thing is not a signal; this turns it into one.

THREE RULES THIS SCRIPT IS BUILT AROUND:

1. It must not depend on anything the pipeline depends on. Stdlib only,
   /usr/bin/python3, no uv, no venv, no `import pipeline`. A notifier that
   breaks when `uv sync` breaks is silent on exactly the day it is needed.
2. It must leave a LOCAL breadcrumb before it touches the network. If SMTP
   is itself what failed, the alert cannot get out, and logs/LAST_FAILURE.txt
   is the only record there will be. It costs one write.
3. It must never leak a secret. The log tail goes into an email body, so
   every value read from .env is redacted out of it first -- belt and braces
   over the existing guarantee that logs/ carries no secret material.
"""
from __future__ import annotations

import os
import re
import smtplib
import subprocess
import sys
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
LOG_TAIL_LINES = 60

# run.py's exit codes, spelled out. The whole point of the alert is that the
# reader should not have to go and look these up at 7am.
EXIT_HINTS = {
    0: "exit 0 -- ran cleanly. If this fired anyway, systemd killed the unit "
       "(TimeoutStartSec, OOM, or a reboot mid-run).",
    1: "exit 1 -- the send failed after send.py exhausted its 5s/30s/120s "
       "ladder, OR the per-run budget ceiling tripped, OR an uncaught "
       "exception. A BatchTimeoutError lands here: the batch did not finish "
       "within AI_DIGEST_BATCH_MAX_WAIT. Check the log tail below for which.",
    2: "exit 2 -- incompatible flags. Should be impossible from the unit file; "
       "if you see this, someone edited ExecStart.",
    3: "exit 3 -- fatal API error: Anthropic auth, credit balance, or the "
       "monthly spend limit. Nothing was sent, nothing was written to outbox/, "
       "and the seen-set was NOT committed, so a re-run re-ingests normally.",
}


def read_env(path: Path) -> dict[str, str]:
    """Parse .env well enough to find the SMTP values.

    Deliberately NOT a dotenv dependency -- see rule 1. Handles the shapes
    that actually occur in this file: comments, blank lines, `export `
    prefixes, and single or double quoted values.
    """
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, val = line.partition("=")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        out[key.strip()] = val
    return out


def setting(env: dict[str, str], key: str, default: str = "") -> str:
    """Real environment wins over .env, matching python-dotenv's precedence
    (it never overwrites an already-set variable) so this script and the
    pipeline can never disagree about where a value came from."""
    return os.environ.get(key) or env.get(key, default)


def systemctl_show(unit: str) -> dict[str, str]:
    props = ("Result", "ExecMainStatus", "ExecMainStartTimestamp",
             "ExecMainExitTimestamp", "InvocationID")
    try:
        proc = subprocess.run(
            ["systemctl", "show", unit, *(f"-p{p}" for p in props)],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    return dict(
        line.partition("=")[::2] for line in proc.stdout.splitlines() if "=" in line
    )


def newest_log() -> Path | None:
    logs = sorted(APP_DIR.joinpath("logs").glob("run-*.log"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    return logs[0] if logs else None


def tail(path: Path, n: int) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return f"(could not read {path}: {exc})"
    return "\n".join(lines[-n:])


def redact(text: str, secrets: list[str]) -> str:
    """Rule 3. Scrub every non-trivial .env value out of the log tail, plus a
    belt-and-braces regex pass for anything shaped like an Anthropic key, in
    case one ever reaches a log by a route .env does not cover.

    The >=8 length guard stops a short or empty value (AI_DIGEST_TO, say, or a
    key someone left blank) matching everywhere and turning the whole tail
    into [REDACTED].
    """
    for s in sorted((x for x in secrets if x and len(x) >= 8), key=len, reverse=True):
        text = text.replace(s, "[REDACTED]")
    return re.sub(r"sk-ant-[A-Za-z0-9_\-]{8,}", "[REDACTED-KEY]", text)


def build_body(unit: str, show: dict[str, str], log_path: Path | None,
               log_tail: str) -> str:
    status_raw = show.get("ExecMainStatus", "")
    try:
        status = int(status_raw)
    except ValueError:
        status = -1
    hint = EXIT_HINTS.get(status, f"exit {status_raw or '?'} -- unrecognised exit code.")

    return "\n".join([
        f"unit:        {unit}",
        f"result:      {show.get('Result', 'unknown')}",
        f"exit status: {status_raw or 'unknown'}",
        f"started:     {show.get('ExecMainStartTimestamp', 'unknown')}",
        f"exited:      {show.get('ExecMainExitTimestamp', 'unknown')}",
        f"host:        {os.uname().nodename}",
        "",
        "WHAT THIS EXIT CODE MEANS",
        f"  {hint}",
        "",
        "NOTHING WAS DELIVERED. On every failure path the seen-set is left",
        "uncommitted, so the items in this run are re-ingested next time --",
        "not committing IS the retry. There is no queue to drain and no",
        "--resume to run. Fix the cause and `sudo systemctl start",
        "ai-digest.service` when you want it now rather than next Monday.",
        "",
        f"LOG: {log_path if log_path else '(no run-*.log found)'}",
        f"last {LOG_TAIL_LINES} lines:",
        "-" * 60,
        log_tail,
        "-" * 60,
        "",
        "journalctl -u ai-digest is NOT a reliable second source on this Pi --",
        "it was found empty for a run that demonstrably happened. The file",
        "above is the record.",
    ])


def main() -> int:
    unit = sys.argv[1] if len(sys.argv) > 1 else "ai-digest.service"
    env = read_env(APP_DIR / ".env")
    secrets = [v for k, v in env.items()
               if any(t in k.upper() for t in ("KEY", "PASSWORD", "TOKEN", "SECRET"))]

    show = systemctl_show(unit)
    log_path = newest_log()
    log_tail = redact(tail(log_path, LOG_TAIL_LINES), secrets) if log_path else "(none)"
    body = build_body(unit, show, log_path, log_tail)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # RULE 2: breadcrumb first, network second.
    try:
        crumb = APP_DIR / "logs" / "LAST_FAILURE.txt"
        crumb.parent.mkdir(parents=True, exist_ok=True)
        crumb.write_text(f"{stamp}\n\n{body}\n", encoding="utf-8")
    except OSError as exc:
        print(f"could not write breadcrumb: {exc}", file=sys.stderr)

    host = setting(env, "AI_DIGEST_SMTP_HOST", "smtp.mail.me.com")
    port = int(setting(env, "AI_DIGEST_SMTP_PORT", "587"))
    user = setting(env, "SMTP_USERNAME")
    password = setting(env, "SMTP_APP_PASSWORD")
    # A dedicated alert address if you want alerts somewhere the digest does
    # not go (a phone, say); otherwise wherever the digest goes.
    to_addr = setting(env, "AI_DIGEST_ALERT_TO") or setting(env, "AI_DIGEST_TO")
    from_addr = setting(env, "AI_DIGEST_FROM") or user

    if not (user and password and to_addr):
        print("SMTP settings incomplete -- breadcrumb written, no email sent.",
              file=sys.stderr)
        return 0

    msg = EmailMessage()
    # Date in the subject so each week's alert is its own thread rather than
    # collapsing into a thread you have already marked read. Distinct from the
    # digest's own "AI Digest - <date>" subject, so alerts never thread into
    # the archive.
    msg["Subject"] = f"ai-digest FAILED - {datetime.now():%Y-%m-%d} ({show.get('Result', '?')})"
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(body)

    try:
        with smtplib.SMTP(host, port, timeout=45) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg)
        print(f"failure alert sent to {to_addr}")
    except Exception as exc:  # noqa: BLE001 -- a notifier must never itself explode
        # No retry ladder here on purpose: repeated bad-credential attempts
        # against Apple risk throttling the account, and send.py has already
        # made that mistake unavailable to us. The breadcrumb is the fallback.
        print(f"could not send failure alert: {exc!r}", file=sys.stderr)

    # Always 0. A failed notify unit is itself invisible, so there is nothing
    # to be gained by failing -- and OnFailure= does not chain from here.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
