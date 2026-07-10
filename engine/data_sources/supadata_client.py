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
_DEFAULT_MAX_WAIT_SECONDS = 180.0


class SupadataError(RuntimeError):
    """A Supadata failure the caller may retry."""


class TranscriptUnavailable(SupadataError):
    """HTTP 206 — the video genuinely has no transcript in the requested mode."""


class QuotaExceeded(SupadataError):
    """HTTP 429 — rate or monthly-credit limit; retry on a later run."""


def _api_key() -> str | None:
    return credentials.get("SUPADATA_API_KEY")


def is_configured() -> bool:
    return bool(_api_key())


def _mode() -> str:
    # native = captions only (1 credit). See the credit note above before changing.
    return os.environ.get("SUPADATA_MODE", "native")


def _max_wait() -> float:
    try:
        return float(os.environ.get("SUPADATA_MAX_WAIT", _DEFAULT_MAX_WAIT_SECONDS))
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
            return body.get("content") or ""
        if status == "failed":
            message = (body.get("error") or {}).get("message") or "job failed"
            raise SupadataError(f"transcript job failed: {message}")
        # queued / active -> keep waiting
    raise SupadataError(f"transcript job {job_id} did not finish within {deadline - time.time():.0f}s")


def get_transcript_text(video_id: str) -> str:
    """Plain-text transcript for a YouTube video id.

    Raises TranscriptUnavailable (206), QuotaExceeded (429), or SupadataError.
    """
    resp = requests.get(
        _BASE,
        params={"url": _WATCH_URL + video_id, "text": "true", "mode": _mode()},
        headers=_headers(),
        timeout=30,
    )
    _check(resp)
    body = resp.json()
    if resp.status_code == 202 or "jobId" in body:      # long video -> async job
        return _poll_job(body["jobId"], time.time() + _max_wait())
    return body.get("content") or ""
