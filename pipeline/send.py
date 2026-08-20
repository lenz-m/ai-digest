"""Stage 4c: the SMTP transport. Thin on purpose -- everything that can hide
a bug lives in email_build.py (message construction) and deliver.py (the
commit transaction). What's left here is opening a socket, one retry policy,
and reading two credentials.

Credentials come from os.environ at point of use, NOT from CONFIG. Config is
a frozen dataclass whose repr lands in tracebacks and is trivially printed
while debugging; a password has no business there. On the Mac they arrive via
scripts/run_with_secrets.sh out of Keychain; on the Pi via a chmod-600 .env.

Host/port/TLS mode are all config-driven, which also means anyone who later
wants a local debug SMTP server can point at localhost:8025 with no code
change. There is deliberately no aiosmtpd in the test suite -- it would add a
dev dependency, an asyncio loop and a listening socket to prove that smtplib
works, which is stdlib, not the code under test.
"""
from __future__ import annotations

import logging
import os
import random
import smtplib
import socket
import time
from email.message import EmailMessage

from pipeline.config import CONFIG

logger = logging.getLogger(__name__)


class SendError(Exception):
    """The email did not go out. Always fatal to the run -- deliver.py treats
    this as "roll back and let next Monday retry"."""


# Keychain service name per env var, so the error a first --apply hits tells
# the user exactly how to fix it instead of just naming the variable.
_KEYCHAIN_SERVICES = {
    "SMTP_USERNAME": "ai-digest-smtp-username",
    "SMTP_APP_PASSWORD": "ai-digest-smtp-app-password",
}


def _require_env(name: str) -> str:
    value = os.environ.get(name, "")
    if value:
        return value
    service = _KEYCHAIN_SERVICES.get(name, f"ai-digest-{name.lower().replace('_', '-')}")
    raise SendError(
        f"{name} is not set, so no email can be sent.\n"
        f"  Mac: add it to Keychain as service '{service}' and run through "
        f"scripts/run_with_secrets.sh --\n"
        f"    security add-generic-password -U -a \"$USER\" -s {service} -w\n"
        f"  Pi:  set {name} in .env (chmod 600).\n"
        f"  The app-specific password is generated at appleid.apple.com -> "
        f"Sign-In and Security."
    )


# Transport-level failures with no response code attached. NOTE the ordering
# in _is_retryable below: nearly everything in smtplib subclasses OSError, so
# a bare OSError check must come LAST or it swallows the permanent failures.
_RETRYABLE = (
    smtplib.SMTPServerDisconnected,
    smtplib.SMTPConnectError,
    smtplib.SMTPHeloError,
    socket.timeout,
    OSError,
)

# Never retried, whatever their code says. Retrying a 535 against Apple is
# actively worse than failing: repeated bad-credential attempts risk
# throttling or locking the account, turning a broken week into a broken
# month. SMTPRecipientsRefused is listed explicitly because -- unlike the
# others -- it does NOT subclass SMTPResponseException, so the 5xx code check
# below would miss it and the OSError catch-all would retry it.
_NEVER_RETRY = (
    smtplib.SMTPAuthenticationError,
    smtplib.SMTPRecipientsRefused,
    smtplib.SMTPSenderRefused,
    smtplib.SMTPNotSupportedError,
)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, _NEVER_RETRY):
        return False
    if isinstance(exc, smtplib.SMTPResponseException):
        # 4xx is transient (greylisting, rate limiting -- exactly the
        # "finicky about an unfamiliar Pi outbound IP" risk). 5xx never is.
        return 400 <= exc.smtp_code < 500
    return isinstance(exc, _RETRYABLE)


def _default_factory() -> smtplib.SMTP:
    """Connect and bring up TLS. Returned already-connected; smtplib's
    __enter__ just returns self, so the caller's `with` block is safe."""
    if CONFIG.smtp_use_ssl:
        return smtplib.SMTP_SSL(CONFIG.smtp_host, CONFIG.smtp_port, timeout=CONFIG.smtp_timeout_seconds)
    smtp = smtplib.SMTP(CONFIG.smtp_host, CONFIG.smtp_port, timeout=CONFIG.smtp_timeout_seconds)
    smtp.ehlo()
    smtp.starttls()
    smtp.ehlo()
    return smtp


def _backoffs() -> list[float]:
    return [float(s) for s in CONFIG.smtp_backoff_seconds.split(",") if s.strip()] or [5.0]


def send_message(msg: EmailMessage, *, smtp_factory=None, sleep=time.sleep) -> None:
    """Send one message, retrying transient failures.

    Gaps are wide (5s -> 30s -> 120s, +/-20% jitter) because this is a weekly
    job with zero latency pressure, so long waits are free -- and greylisting
    and IP-reputation throttling clear over minutes. A 1-second retry has
    approximately no chance of clearing either; a 2-minute one has a real one.
    Total added wall time is about 3 minutes.

    HARD CONSTRAINT: the retry wraps the SMTP transaction ONLY. It must never
    re-enter render, and categorically must never re-run an LLM stage -- all
    API spend has already happened by the time this is called. Enforced by
    test in tests/test_deliver.py.

    smtp_factory and sleep are injected so the whole retry policy is testable
    with no sockets and no real waiting.
    """
    smtp_factory = smtp_factory or _default_factory

    # Read credentials BEFORE opening anything. A missing password should be
    # an instant, self-explaining error, not a connection followed by an
    # auth failure that then looks like a credentials problem at Apple's end.
    username = _require_env("SMTP_USERNAME")
    password = _require_env("SMTP_APP_PASSWORD")

    backoffs = _backoffs()
    attempts = max(1, CONFIG.smtp_max_attempts)
    last: BaseException | None = None

    for attempt in range(attempts):
        try:
            with smtp_factory() as smtp:
                smtp.login(username, password)
                smtp.send_message(msg)
            logger.info("email sent to %s (attempt %d)", msg["To"], attempt + 1)
            return
        except SendError:
            raise
        except Exception as exc:
            last = exc
            if not _is_retryable(exc):
                raise SendError(f"permanent SMTP failure, not retrying: {exc!r}") from exc
            if attempt == attempts - 1:
                break
            delay = backoffs[min(attempt, len(backoffs) - 1)] * random.uniform(0.8, 1.2)
            logger.warning(
                "SMTP attempt %d/%d failed (%r), retrying in %.0fs",
                attempt + 1, attempts, exc, delay,
            )
            sleep(delay)

    raise SendError(f"SMTP failed after {attempts} attempt(s): {last!r}") from last
