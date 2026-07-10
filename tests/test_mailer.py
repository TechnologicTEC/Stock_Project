"""
engine/mailer.py — provider-agnostic email. No network or SMTP is touched; the
env is cleared per test so a real .env can't leak in.
"""
from unittest.mock import Mock, patch

import pytest

from engine import mailer

_ENV = ("RESEND_API_KEY", "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD",
        "DIGEST_EMAIL_TO", "DIGEST_FROM", "OWNER_EMAILS")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in _ENV:
        monkeypatch.delenv(key, raising=False)


def test_unconfigured_send_is_a_noop():
    assert mailer.is_configured() is False
    assert mailer.send("subject", "<p>body</p>") is False


def test_recipient_prefers_digest_to_then_first_owner_email(monkeypatch):
    monkeypatch.setenv("OWNER_EMAILS", " a@x.com , b@x.com ")
    assert mailer.recipient() == "a@x.com"
    monkeypatch.setenv("DIGEST_EMAIL_TO", "me@x.com")
    assert mailer.recipient() == "me@x.com"


def test_resend_backend_posts_to_the_api(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_123")
    monkeypatch.setenv("DIGEST_EMAIL_TO", "me@x.com")
    assert mailer.is_configured() is True

    with patch("engine.mailer.requests.post", return_value=Mock(status_code=200)) as post:
        assert mailer.send("Subj", "<b>hi</b>") is True

    body = post.call_args.kwargs["json"]
    assert body["to"] == ["me@x.com"] and body["subject"] == "Subj" and "<b>hi</b>" in body["html"]
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer re_123"


def test_smtp_backend_used_when_no_resend_key(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.x.com")
    monkeypatch.setenv("SMTP_USER", "u@x.com")
    monkeypatch.setenv("SMTP_PASSWORD", "pw")
    monkeypatch.setenv("DIGEST_EMAIL_TO", "me@x.com")

    with patch("engine.mailer.smtplib.SMTP") as smtp_cls:
        smtp = smtp_cls.return_value.__enter__.return_value
        assert mailer.send("Subj", "<b>hi</b>") is True

    smtp.starttls.assert_called_once()
    smtp.login.assert_called_once_with("u@x.com", "pw")
    smtp.send_message.assert_called_once()


def test_backend_without_a_recipient_is_still_a_noop(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_123")   # configured, but nowhere to send
    assert mailer.is_configured() is False
    assert mailer.send("s", "h") is False
