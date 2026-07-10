"""
Official YouTube Data API v3 — channel lookup + recent uploads.

Why: the public RSS feed (youtube.com/feeds/videos.xml) is an unofficial endpoint
that throttles hard — measured 1-in-6 success, returning a mix of 404s and 500s,
which made the scheduled scan fail with "feed FAILED after 5 tries". The official
API is free and reliable:

    channels.list       1 quota unit
    playlistItems.list  1 quota unit
    free daily quota    10,000 units

So one scan costs ~2 units per creator; four scans a day is ~8/10,000. Get a key
from the Google Cloud console (enable "YouTube Data API v3") and set
YOUTUBE_API_KEY. Without it, youtube_client falls back to the flaky RSS feed.

`channels.list?forHandle=@name` also resolves a handle to a channel id officially,
which avoids scraping the channel's HTML page (YouTube blocks that from
datacenter IPs — it's what made "Add creator" fail on the deployed app).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from functools import lru_cache

import requests

from engine import credentials

_BASE = "https://www.googleapis.com/youtube/v3"
_WATCH_URL = "https://www.youtube.com/watch?v="
_CHANNEL_ID_RE = re.compile(r"UC[0-9A-Za-z_-]{22}")


class YouTubeApiError(RuntimeError):
    """A Data API failure (quota, bad key, outage). Callers may fall back."""


def _api_key() -> str | None:
    return credentials.get("YOUTUBE_API_KEY")


def is_configured() -> bool:
    return bool(_api_key())


def _get(resource: str, **params) -> dict:
    key = _api_key()
    if not key:
        raise YouTubeApiError("YOUTUBE_API_KEY is not set")
    resp = requests.get(f"{_BASE}/{resource}", params={**params, "key": key}, timeout=15)
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("error", {}).get("message", "")
        except Exception:
            detail = (resp.text or "")[:120]
        raise YouTubeApiError(f"{resource} HTTP {resp.status_code}: {detail}")
    return resp.json()


def _parse_published(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def resolve_channel(url_or_handle: str) -> dict:
    """{"channel_id", "display_name", "handle"} from a UC id, /channel/ URL, or
    @handle — officially, with no HTML scraping. Raises ValueError if not found."""
    text = (url_or_handle or "").strip()
    channel_id = None
    if _CHANNEL_ID_RE.fullmatch(text):
        channel_id = text
    else:
        m = re.search(r"/channel/(UC[0-9A-Za-z_-]{22})", text)
        channel_id = m.group(1) if m else None

    if channel_id:
        body = _get("channels", part="snippet", id=channel_id)
    else:
        m = re.search(r"@([\w.-]+)", text)
        if not m:
            raise ValueError(f"Couldn't read a channel id or @handle from {url_or_handle!r}.")
        body = _get("channels", part="snippet", forHandle="@" + m.group(1))

    items = body.get("items") or []
    if not items:
        raise ValueError(f"No YouTube channel found for {url_or_handle!r}.")
    item = items[0]
    snippet = item.get("snippet") or {}
    return {
        "channel_id": item.get("id"),
        "display_name": snippet.get("title"),
        "handle": snippet.get("customUrl") or (("@" + m.group(1)) if not channel_id else None),
    }


@lru_cache(maxsize=32)
def _uploads_playlist(channel_id: str) -> str:
    """The channel's 'uploads' playlist id. Memoized — it never changes."""
    items = _get("channels", part="contentDetails", id=channel_id).get("items") or []
    if not items:
        raise YouTubeApiError(f"channel {channel_id} not found")
    playlist = (items[0].get("contentDetails") or {}).get("relatedPlaylists", {}).get("uploads")
    if not playlist:
        raise YouTubeApiError(f"channel {channel_id} exposes no uploads playlist")
    return playlist


def list_uploads(channel_id: str, limit: int = 15) -> list[dict]:
    """Recent uploads, newest first, in the same shape youtube_client returns."""
    body = _get("playlistItems", part="snippet,contentDetails",
                playlistId=_uploads_playlist(channel_id), maxResults=min(max(limit, 1), 50))
    out = []
    for item in body.get("items") or []:
        snippet, details = item.get("snippet") or {}, item.get("contentDetails") or {}
        video_id = details.get("videoId") or (snippet.get("resourceId") or {}).get("videoId")
        if not video_id:
            continue
        out.append({
            "video_id": video_id,
            "title": snippet.get("title") or "",
            "url": _WATCH_URL + video_id,
            "published_at": _parse_published(details.get("videoPublishedAt") or snippet.get("publishedAt")),
        })
        if len(out) >= limit:
            break
    return out


def refresh() -> None:
    """Drop the memoized uploads-playlist ids (tests)."""
    _uploads_playlist.cache_clear()
