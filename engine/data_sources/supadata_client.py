"""
YouTube transcripts via Supadata (https://supadata.ai) — a hosted transcript API.

Why this exists: YouTube blocks its caption endpoint from datacenter IPs, so the
direct youtube-transcript-api path always fails on GitHub Actions / HF Spaces
(proven: 15/15 videos came back `blocked`). Supadata fetches server-side, so the
scheduled scan works without a residential proxy.

Credits (free tier is 100/month):
  * `mode=native`   — existing captions, **1 credit** per transcript. The default.
  * `mode=generate|auto` — AI-transcribes when captions are missing, at **2 credits
    per minute** (a 20-minute video ≈ 40 credits). Opt in with SUPADATA_MODE=auto.

Long videos (>20 min) return HTTP 202 + a jobId, which we poll (job checks are free).

Like every data_sources/* module this makes raw network calls and does no caching.
"""
from __future__ import annotations

import os
import time

import requests

from engine import credentials

_BASE = "https://api.supadata.ai/v1/transcript"
_WATCH_URL = "https://www.youtube.com/watch?v="
_POLL_INTERVAL_SECONDS = 1.0        # the docs' recommended interval

# Ask for English by name. Without `lang`, Supadata returns "the first available
# language" — and YouTube now auto-translates ZipTrader's videos into ~19
# caption tracks with Bangla listed first. Six videos (10 Sep - 13 Oct 2026,
# five of them in the last fortnight) were stored in Bengali, and extraction
# from the translation flipped a stance and missed a bearish call (13 Oct: ONCY
# read bullish where the English says neutral; LCID bearish not found at all).
TRANSCRIPT_LANG = "en"
_DEFAULT_MAX_WAIT_SECONDS = 180.0


class SupadataError(RuntimeError):
    """A Supadata failure the caller may retry."""


class TranscriptUnavailable(SupadataError):
    """HTTP 206 — the video genuinely has no transcript in the requested mode."""


class QuotaExceeded(SupadataError):
    """HTTP 429 — rate or monthly-credit limit; retry on a later run."""


class NotEnglish(SupadataError):
    """English was asked for and another language came back — which Supadata
    does when the video has no English track. Not worth retrying."""

    def __init__(self, lang: str | None, available=None):
        self.lang, self.available = lang, list(available or [])
        super().__init__(f"transcript came back as {lang!r}, not English "
                         f"(available: {', '.join(self.available) or 'unknown'})")


def _require_english(body: dict) -> None:
    """Supadata reports the language it returned; refuse anything but English.
    A response with no `lang` at all is let through to the content check,
    youtube_client.looks_english."""
    lang = (body.get("lang") or "").strip().lower()
    if lang and not lang.startswith(TRANSCRIPT_LANG):
        raise NotEnglish(lang, body.get("availableLangs"))


def _api_key() -> str | None:
    return credentials.get("SUPADATA_API_KEY")


def is_configured() -> bool:
    return bool(_api_key())


_MODES = ("native", "generate", "auto")


def mode() -> str:
    """native = captions only (1 credit). See the credit note above before changing.

    NB: `or`, not `os.environ.get(name, default)` — GitHub Actions injects an
    **empty string** for an undefined var/secret, and get()'s default only applies
    when the key is absent. Sending `mode=` produced a 400 from Supadata.
    """
    chosen = (os.environ.get("SUPADATA_MODE") or "native").strip().lower()
    return chosen if chosen in _MODES else "native"


def _max_wait() -> float:
    raw = (os.environ.get("SUPADATA_MAX_WAIT") or "").strip()
    try:
        return float(raw) if raw else _DEFAULT_MAX_WAIT_SECONDS
    except ValueError:
        return _DEFAULT_MAX_WAIT_SECONDS


def _headers() -> dict:
    key = _api_key()
    if not key:
        raise SupadataError("SUPADATA_API_KEY is not set")
    return {"x-api-key": key}


def _check(resp) -> None:
    """Map Supadata's documented status codes onto typed errors. 200/202 pass."""
    if resp.status_code == 206:
        raise TranscriptUnavailable("no transcript available for this video")
    if resp.status_code == 429:
        raise QuotaExceeded("Supadata rate/credit limit exceeded")
    if resp.status_code >= 400:
        try:
            body = resp.json()
            detail = body.get("message") or body.get("error")
        except Exception:
            detail = (resp.text or "")[:120]
        raise SupadataError(f"HTTP {resp.status_code}: {detail}")


def _poll_job(job_id: str, deadline: float) -> str:
    """Wait for an async (long-video) transcript job. Status checks cost nothing."""
    while time.time() < deadline:
        time.sleep(_POLL_INTERVAL_SECONDS)
        resp = requests.get(f"{_BASE}/{job_id}", headers=_headers(), timeout=20)
        _check(resp)
        body = resp.json()
        status = body.get("status")
        if status == "completed":
            _require_english(body)
            return body.get("content") or ""
        if status == "failed":
            message = (body.get("error") or {}).get("message") or "job failed"
            raise SupadataError(f"transcript job failed: {message}")
        # queued / active -> keep waiting
    raise SupadataError(f"transcript job {job_id} did not finish within {deadline - time.time():.0f}s")


def get_transcript_text(video_id: str) -> str:
    """Plain-text transcript for a YouTube video id.

    Raises TranscriptUnavailable (206), QuotaExceeded (429), NotEnglish, or
    SupadataError.
    """
    resp = requests.get(
        _BASE,
        params={"url": _WATCH_URL + video_id, "text": "true", "mode": mode(),
                "lang": TRANSCRIPT_LANG},
        headers=_headers(),
        timeout=30,
    )
    _check(resp)
    body = resp.json()
    if resp.status_code == 202 or "jobId" in body:      # long video -> async job
        return _poll_job(body["jobId"], time.time() + _max_wait())
    _require_english(body)
    return body.get("content") or ""
