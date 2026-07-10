"""
Outbound email, used by the Creator Signals digest (docs/creator-signals-plan.md).

Provider-agnostic — configure whichever you already have:
  * **Resend**: set `RESEND_API_KEY` (one HTTP call, no deps)
  * **SMTP**:   set `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD` (+ `SMTP_PORT`, default 587)

Optional: `DIGEST_FROM` (sender), `DIGEST_EMAIL_TO` (recipient — otherwise the
first address in `OWNER_EMAILS`).

Set neither backend and `send()` is a **no-op returning False**, so the scan job
simply skips the email rather than failing.

These are host secrets (like DATABASE_URL), so they're read from `os.environ`
rather than the per-user credentials vault.
"""
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage

import requests

_RESEND_ENDPOINT = "https://api.resend.com/emails"
# Resend's sandbox sender — works with no verified domain, but only delivers to
# the address that owns the Resend account. Set DIGEST_FROM once you verify one.
_DEFAULT_FROM = "Investment Co-Pilot <onboarding@resend.dev>"


def recipient() -> str | None:
    """Where the digest goes: DIGEST_EMAIL_TO, else the first OWNER_EMAILS entry."""
    to = (os.environ.get("DIGEST_EMAIL_TO") or "").strip()
    if to:
        return to
    owners = [e.strip() for e in (os.environ.get("OWNER_EMAILS") or "").split(",") if e.strip()]
    return owners[0] if owners else None


def _sender() -> str:
    return os.environ.get("DIGEST_FROM") or os.environ.get("SMTP_USER") or _DEFAULT_FROM


def _backend() -> str | None:
    if os.environ.get("RESEND_API_KEY"):
        return "resend"
    if all(os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD")):
        return "smtp"
    return None


def is_configured() -> bool:
    return bool(_backend()) and bool(recipient())


def _send_resend(to: str, subject: str, html: str) -> None:
    resp = requests.post(
        _RESEND_ENDPOINT,
        headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}",
                 "Content-Type": "application/json"},
        json={"from": _sender(), "to": [to], "subject": subject, "html": html},
        timeout=15,
    )
    resp.raise_for_status()


def _send_smtp(to: str, subject: str, html: str) -> None:
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, _sender(), to
    msg.set_content("This digest is best viewed as HTML.")
    msg.add_alternative(html, subtype="html")
    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587")), timeout=20) as smtp:
        smtp.starttls()
        smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        smtp.send_message(msg)


def send(subject: str, html: str, to: str | None = None) -> bool:
    """Send one HTML email. Returns False (without raising) when email isn't
    configured; raises only if a configured backend actually fails."""
    backend = _backend()
    to = to or recipient()
    if not backend or not to:
        return False
    (_send_resend if backend == "resend" else _send_smtp)(to, subject, html)
    return True
