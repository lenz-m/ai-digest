"""Transport tests with an injected fake. Zero sockets, zero real sleeps.

Deliberately no aiosmtpd: a listening socket and an asyncio loop would prove
that smtplib works, which is stdlib and not the code under test. The one
thing no fake can prove -- that iCloud accepts this account, from this IP,
with this From header -- is the manual smoke test in docs/stage4-send-plan.md.
"""
from __future__ import annotations

import smtplib
from email.message import EmailMessage

import pytest

from pipeline import config as config_module
from pipeline import send as send_module
from pipeline.send import SendError, _is_retryable, send_message


@pytest.fixture(autouse=True)
def _creds(monkeypatch):
    monkeypatch.setenv("SMTP_USERNAME", "me@icloud.com")
    monkeypatch.setenv("SMTP_APP_PASSWORD", "abcd-efgh-ijkl-mnop")


def _msg():
    msg = EmailMessage()
    msg["Subject"] = "AI Digest — Aug 24, 2026"
    msg["From"] = "me@icloud.com"
    msg["To"] = "me@icloud.com"
    msg.set_content("body")
    return msg


class FakeSMTP:
    """Records the transaction. `raises` is a list of exceptions (or None) to
    apply per attempt, so a test can script "fail twice, then succeed"."""

    def __init__(self, log, raises=None):
        self.log = log
        self._raises = list(raises or [])
        self.log["factory_calls"] += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.log["quit"] += 1
        return False

    def _maybe_raise(self):
        if self._raises:
            exc = self._raises.pop(0)
            if exc is not None:
                raise exc

    def login(self, user, password):
        self.log["logins"].append((user, password))
        self._maybe_raise()

    def send_message(self, msg):
        self.log["sent"].append(msg)
        self._maybe_raise()


def _harness(per_attempt_exceptions=None):
    """Returns (factory, log, sleeps). per_attempt_exceptions[i] is raised on
    attempt i (None = that attempt succeeds)."""
    log = {"factory_calls": 0, "logins": [], "sent": [], "quit": 0}
    scripted = list(per_attempt_exceptions or [])
    sleeps: list[float] = []

    def factory():
        exc = scripted.pop(0) if scripted else None
        return FakeSMTP(log, raises=[exc] if exc else None)

    return factory, log, sleeps


def _sleep_recorder(sleeps):
    return lambda d: sleeps.append(d)


# --- happy path ---

def test_sends_once_with_env_credentials():
    factory, log, sleeps = _harness()
    send_message(_msg(), smtp_factory=factory, sleep=_sleep_recorder(sleeps))

    assert log["factory_calls"] == 1
    assert log["logins"] == [("me@icloud.com", "abcd-efgh-ijkl-mnop")]
    assert len(log["sent"]) == 1
    assert log["quit"] == 1, "connection must be closed"
    assert sleeps == []


# --- retry policy ---

def test_retries_transient_disconnect_and_succeeds_on_the_third_attempt():
    factory, log, sleeps = _harness([
        smtplib.SMTPServerDisconnected("connection reset"),
        smtplib.SMTPServerDisconnected("connection reset"),
        None,
    ])
    send_message(_msg(), smtp_factory=factory, sleep=_sleep_recorder(sleeps))

    assert log["factory_calls"] == 3
    # The first two attempts died at login, so exactly ONE message went out --
    # a retry must not turn into a duplicate digest.
    assert len(log["sent"]) == 1
    assert len(sleeps) == 2


def test_exhausting_attempts_raises_send_error():
    factory, log, sleeps = _harness([smtplib.SMTPServerDisconnected("nope")] * 5)
    with pytest.raises(SendError, match="after 3 attempt"):
        send_message(_msg(), smtp_factory=factory, sleep=_sleep_recorder(sleeps))
    assert log["factory_calls"] == 3
    assert len(sleeps) == 2, "no sleep after the final attempt"


def test_does_not_retry_authentication_failure():
    """The one protecting the account: repeated bad-credential attempts
    against Apple risk throttling or lockout, turning a broken week into a
    broken month."""
    factory, log, sleeps = _harness([smtplib.SMTPAuthenticationError(535, b"bad creds")] * 3)
    with pytest.raises(SendError, match="permanent SMTP failure"):
        send_message(_msg(), smtp_factory=factory, sleep=_sleep_recorder(sleeps))
    assert log["factory_calls"] == 1
    assert sleeps == []


def test_does_not_retry_550_sender_refused():
    """The likeliest first-run failure: a From address iCloud doesn't
    recognise. Hard 550, not something waiting fixes."""
    factory, log, sleeps = _harness([smtplib.SMTPSenderRefused(550, b"not owned", "me@icloud.com")] * 3)
    with pytest.raises(SendError, match="permanent SMTP failure"):
        send_message(_msg(), smtp_factory=factory, sleep=_sleep_recorder(sleeps))
    assert log["factory_calls"] == 1


def test_does_not_retry_recipients_refused():
    """SMTPRecipientsRefused does NOT subclass SMTPResponseException, so the
    5xx code check misses it and the OSError catch-all would happily retry.
    This is the case an isinstance-order refactor breaks silently."""
    factory, log, sleeps = _harness([smtplib.SMTPRecipientsRefused({"x@y.com": (550, b"no")})] * 3)
    with pytest.raises(SendError, match="permanent SMTP failure"):
        send_message(_msg(), smtp_factory=factory, sleep=_sleep_recorder(sleeps))
    assert log["factory_calls"] == 1


def test_does_retry_421_service_unavailable():
    factory, log, sleeps = _harness([smtplib.SMTPResponseException(421, b"try later"), None])
    send_message(_msg(), smtp_factory=factory, sleep=_sleep_recorder(sleeps))
    assert log["factory_calls"] == 2


def test_retry_classification_matrix():
    """Asserted directly rather than trusting the isinstance ordering, since
    that ordering is exactly what a later refactor breaks silently."""
    assert _is_retryable(smtplib.SMTPServerDisconnected("x")) is True
    assert _is_retryable(smtplib.SMTPResponseException(451, b"x")) is True
    assert _is_retryable(OSError("network unreachable")) is True
    assert _is_retryable(TimeoutError("timed out")) is True

    assert _is_retryable(smtplib.SMTPAuthenticationError(535, b"x")) is False
    assert _is_retryable(smtplib.SMTPSenderRefused(550, b"x", "a@b.com")) is False
    assert _is_retryable(smtplib.SMTPRecipientsRefused({"a@b.com": (550, b"x")})) is False
    assert _is_retryable(smtplib.SMTPResponseException(550, b"x")) is False
    assert _is_retryable(smtplib.SMTPResponseException(500, b"x")) is False


# --- backoff comes from config, and the test takes 0ms ---

def test_backoff_delays_read_from_config_in_order(monkeypatch):
    cfg = config_module.Config(smtp_backoff_seconds="5,30,120", smtp_max_attempts=4)
    monkeypatch.setattr(send_module, "CONFIG", cfg)

    factory, log, sleeps = _harness([smtplib.SMTPServerDisconnected("x")] * 4)
    with pytest.raises(SendError):
        send_message(_msg(), smtp_factory=factory, sleep=_sleep_recorder(sleeps))

    # +/-20% jitter, so assert the bands rather than exact values.
    assert len(sleeps) == 3
    for actual, nominal in zip(sleeps, [5, 30, 120]):
        assert nominal * 0.8 <= actual <= nominal * 1.2


def test_backoff_repeats_the_last_gap_when_attempts_exceed_the_list(monkeypatch):
    cfg = config_module.Config(smtp_backoff_seconds="5", smtp_max_attempts=3)
    monkeypatch.setattr(send_module, "CONFIG", cfg)

    factory, log, sleeps = _harness([smtplib.SMTPServerDisconnected("x")] * 3)
    with pytest.raises(SendError):
        send_message(_msg(), smtp_factory=factory, sleep=_sleep_recorder(sleeps))
    assert len(sleeps) == 2
    assert all(4 <= s <= 6 for s in sleeps)


# --- credentials ---

def test_missing_password_errors_before_any_connection(monkeypatch):
    monkeypatch.delenv("SMTP_APP_PASSWORD", raising=False)
    factory, log, sleeps = _harness()

    with pytest.raises(SendError) as exc:
        send_message(_msg(), smtp_factory=factory, sleep=_sleep_recorder(sleeps))

    assert log["factory_calls"] == 0, "must not open a socket before checking credentials"
    assert "ai-digest-smtp-app-password" in str(exc.value), "name the Keychain service, so the error is self-solving"
    assert "security add-generic-password" in str(exc.value)


def test_missing_username_names_its_own_keychain_service(monkeypatch):
    monkeypatch.delenv("SMTP_USERNAME", raising=False)
    factory, log, sleeps = _harness()
    with pytest.raises(SendError, match="ai-digest-smtp-username"):
        send_message(_msg(), smtp_factory=factory, sleep=_sleep_recorder(sleeps))
