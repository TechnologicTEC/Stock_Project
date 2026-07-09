"""
YouTube access for the Creator Signals feature (docs/creator-signals-plan.md).
Two raw, un-cached calls (like every data_sources/* module):

    latest_videos(channel_id) -> [{"video_id", "title", "url", "published_at"}]
        Recent uploads via the channel's public Atom feed
        (youtube.com/feeds/videos.xml). No API key, no quota. The feed
        intermittently 404/500s, so we retry.

    get_transcript(video_id) -> (status, text)
        The video's captions via youtube-transcript-api. status is one of
        ok | no_captions | blocked | error; text is the joined caption text on
        "ok", else None. youtube-transcript-api is imported lazily so this module
        (and the app/tests) load fine without it — only the cron needs it.

Caveat: transcript fetching can be blocked from datacenter IPs (e.g. GitHub
Actions runners); that surfaces here as status "blocked" and the caller can
retry on the next run.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

_FEED_URL = "https://www.youtube.com/feeds/videos.xml"
_WATCH_URL = "https://www.youtube.com/watch?v="
# A browser-ish UA avoids the occasional bot-block on the feed endpoint.
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _parse_published(text: str | None) -> datetime | None:
    """Atom `<published>` is ISO-8601 (…Z). Return a tz-aware UTC datetime."""
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _parse_feed(content: bytes, limit: int) -> list[dict]:
    soup = BeautifulSoup(content, "xml")
    out: list[dict] = []
    for entry in soup.find_all("entry"):
        id_tag = entry.find("id")          # "yt:video:VIDEOID" — no namespace, avoids the yt: prefix pitfall
        title = entry.find("title")
        if not (id_tag and id_tag.text and title and title.text):
            continue
        video_id = id_tag.text.rsplit(":", 1)[-1].strip()
        link = entry.find("link")
        published = entry.find("published")
        out.append({
            "video_id": video_id,
            "title": title.text.strip(),
            "url": (link.get("href") if link and link.get("href") else _WATCH_URL + video_id),
            "published_at": _parse_published(published.text if published else None),
        })
        if len(out) >= limit:
            break
    return out


def latest_videos(channel_id: str, limit: int = 15, retries: int = 5) -> list[dict]:
    """Recent uploads for a channel, newest first. The feed is flaky (intermittent
    404/500), so retry with a growing backoff; raises if it never returns 200. A
    whole-run failure is self-healing — the videos are still 'new' next run."""
    last_status = None
    for attempt in range(retries):
        resp = requests.get(_FEED_URL, params={"channel_id": channel_id}, headers=_HEADERS, timeout=15)
        if resp.status_code == 200:
            return _parse_feed(resp.content, limit)
        last_status = resp.status_code
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"YouTube feed for {channel_id} failed after {retries} tries (last HTTP {last_status})")


def _fetch_transcript_text(video_id: str) -> str:
    """Join a video's caption segments into one string. Isolated so tests can
    patch it (and so the youtube-transcript-api import stays lazy)."""
    from youtube_transcript_api import YouTubeTranscriptApi

    api = YouTubeTranscriptApi()
    if hasattr(api, "fetch"):                       # v1.x instance API
        return " ".join(seg.text for seg in api.fetch(video_id))
    return " ".join(s["text"] for s in YouTubeTranscriptApi.get_transcript(video_id))  # legacy static API


# Exception class names (matched by name so we don't import them — they move
# between youtube-transcript-api versions) that mean captions genuinely aren't
# available vs. we were rate-limited/blocked.
_NO_CAPTION_ERRORS = {"TranscriptsDisabled", "NoTranscriptFound", "NoTranscriptAvailable",
                      "VideoUnavailable", "VideoUnplayable"}
_BLOCKED_HINTS = ("block", "ipblocked", "toomanyrequests", "requestblocked", "429")


def get_transcript(video_id: str) -> tuple[str, str | None]:
    """(status, text): status is ok | no_captions | blocked | error."""
    try:
        text = _fetch_transcript_text(video_id)
        return ("ok", text) if (text and text.strip()) else ("no_captions", None)
    except Exception as exc:
        name = type(exc).__name__
        if name in _NO_CAPTION_ERRORS:
            return "no_captions", None
        blob = f"{name} {exc}".lower()
        if any(h in blob for h in _BLOCKED_HINTS):
            return "blocked", None
        return "error", None
