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

import logging
import os
import re
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

from engine.data_sources import supadata_client, youtube_data_api

logger = logging.getLogger(__name__)

_FEED_URL = "https://www.youtube.com/feeds/videos.xml"
_CHANNEL_ID_RE = re.compile(r"UC[0-9A-Za-z_-]{22}")
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


def _fetch_feed(channel_id: str, retries: int = 5) -> bytes:
    """The channel's Atom feed. It's flaky (intermittent 404/500), so retry with a
    growing backoff. Unlike the channel's HTML page this endpoint is not
    bot-protected, so it works from datacenter IPs."""
    last_status = None
    for attempt in range(retries):
        resp = requests.get(_FEED_URL, params={"channel_id": channel_id}, headers=_HEADERS, timeout=15)
        if resp.status_code == 200:
            return resp.content
        last_status = resp.status_code
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"YouTube feed for {channel_id} failed after {retries} tries (last HTTP {last_status})")


def latest_videos(channel_id: str, limit: int = 15, retries: int = 5) -> list[dict]:
    """Recent uploads for a channel, newest first.

    Prefers the official Data API (free, 1 quota unit, reliable). Falls back to
    the unofficial RSS feed, which throttles hard (measured ~1-in-6 success,
    404s and 500s) — that's why the API key is worth setting. A whole-run failure
    is self-healing: the videos are still 'new' on the next run.
    """
    if youtube_data_api.is_configured():
        try:
            return youtube_data_api.list_uploads(channel_id, limit)
        except Exception as exc:
            logger.warning("Data API list_uploads(%s) failed (%s: %s) — falling back to the flaky RSS feed",
                           channel_id, type(exc).__name__, exc)
    return _parse_feed(_fetch_feed(channel_id, retries), limit)


def channel_info(channel_id: str) -> dict:
    """Verify a channel and read its display name from the *feed* — deliberately
    not the channel's HTML page, which YouTube blocks from datacenter IPs."""
    soup = BeautifulSoup(_fetch_feed(channel_id), "xml")
    name = None
    author = soup.find("author")
    if author is not None and author.find("name") is not None:
        name = author.find("name").text.strip()
    if not name:
        title = soup.find("title")
        name = title.text.strip() if title is not None else None
    return {"channel_id": channel_id, "display_name": name}


def _channel_id_from_input(text: str) -> str | None:
    """A UC id straight out of the input — a bare id, or a /channel/UC… URL."""
    if _CHANNEL_ID_RE.fullmatch(text):
        return text
    m = re.search(r"/channel/(UC[0-9A-Za-z_-]{22})", text)
    return m.group(1) if m else None


def _resolve_via_html(text: str) -> tuple[str, str | None]:
    """Map an @handle / custom URL to a channel id by reading the channel page.
    This is the ONE bot-protected call here — YouTube commonly blocks it from
    datacenter IPs, so the error tells the user how to avoid needing it."""
    url = text if text.startswith("http") else \
        "https://www.youtube.com/" + (text if text.startswith("@") else "@" + text)
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=15)
        resp.raise_for_status()
        html = resp.text
    except Exception as exc:
        raise ValueError(
            f"YouTube blocked the handle lookup from this server ({type(exc).__name__}). "
            "Paste the channel's '/channel/UC…' URL instead — on the channel page use "
            "Share channel → Copy channel ID."
        ) from exc

    m = re.search(r"/channel/(UC[0-9A-Za-z_-]{22})", html) \
        or re.search(r'"(?:channelId|externalId)":"(UC[0-9A-Za-z_-]{22})"', html)
    if not m:
        raise ValueError(f"Couldn't find a channel id for {text!r} — check the URL/handle.")
    handle = re.search(r'"canonicalBaseUrl":"/(@[\w.-]+)"', html)
    return m.group(1), (handle.group(1) if handle else None)


def resolve_channel(url_or_handle: str) -> dict:
    """Resolve a channel URL / @handle / bare UC id to
    {"channel_id", "display_name", "handle"}.

    A bare UC id or a /channel/UC… URL needs **no HTML scrape** — the channel is
    verified and named from the feed. Only an @handle / custom URL needs the
    channel page, which can be blocked from datacenter IPs.
    """
    text = (url_or_handle or "").strip()
    if not text:
        raise ValueError("Enter a channel URL or @handle.")

    # Official API first: resolves @handles without scraping the channel page
    # (which YouTube blocks from datacenter IPs).
    if youtube_data_api.is_configured():
        try:
            return youtube_data_api.resolve_channel(text)
        except ValueError:
            raise                 # genuinely no such channel — don't mask it
        except Exception as exc:
            logger.warning("Data API channel lookup failed (%s: %s) — falling back",
                           type(exc).__name__, exc)

    channel_id = _channel_id_from_input(text)
    handle = text if text.startswith("@") else None
    if channel_id is None:
        channel_id, resolved_handle = _resolve_via_html(text)
        handle = resolved_handle or handle

    info = channel_info(channel_id)   # verifies it exists + gets the name, via the feed
    info["handle"] = handle
    return info


def _proxy_config():
    """Route transcript fetches through a proxy when configured. YouTube blocks
    the caption endpoint from datacenter IPs (GitHub Actions, HF Spaces, …), so a
    residential proxy is the fix for a cloud-run scan. Returns None when unset,
    and only imports the proxies module in that case.

    Set EITHER `WEBSHARE_PROXY_USERNAME` + `WEBSHARE_PROXY_PASSWORD` (the
    rotating-residential provider youtube-transcript-api supports natively), or a
    generic `YT_PROXY_URL` like http://user:pass@host:port.
    """
    user, password = os.environ.get("WEBSHARE_PROXY_USERNAME"), os.environ.get("WEBSHARE_PROXY_PASSWORD")
    url = os.environ.get("YT_PROXY_URL")
    if not (user and password) and not url:
        return None
    from youtube_transcript_api.proxies import GenericProxyConfig, WebshareProxyConfig

    if user and password:
        return WebshareProxyConfig(proxy_username=user, proxy_password=password)
    return GenericProxyConfig(http_url=url, https_url=url)


def _fetch_transcript_text(video_id: str) -> str:
    """Join a video's caption segments into one string. Isolated so tests can
    patch it (and so the youtube-transcript-api import stays lazy)."""
    from youtube_transcript_api import YouTubeTranscriptApi

    api = YouTubeTranscriptApi(proxy_config=_proxy_config())
    if hasattr(api, "fetch"):                       # v1.x instance API
        return " ".join(seg.text for seg in api.fetch(video_id))
    return " ".join(s["text"] for s in YouTubeTranscriptApi.get_transcript(video_id))  # legacy static API


# Exception class names (matched by name so we don't import them — they move
# between youtube-transcript-api versions) that mean captions genuinely aren't
# available vs. we were rate-limited/blocked.
_NO_CAPTION_ERRORS = {"TranscriptsDisabled", "NoTranscriptFound", "NoTranscriptAvailable",
                      "VideoUnavailable", "VideoUnplayable"}
_BLOCKED_HINTS = ("block", "ipblocked", "toomanyrequests", "requestblocked", "429")


def _direct_transcript(video_id: str) -> tuple[str, str | None]:
    """Fetch captions straight from YouTube. Works from a residential IP; blocked
    from datacenter IPs unless a proxy is configured (see _proxy_config)."""
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


def get_transcript(video_id: str) -> tuple[str, str | None]:
    """(status, text): status is ok | no_captions | blocked | error.

    Prefers the Supadata API when a key is set — it fetches server-side, so it
    works from the datacenter IPs that YouTube blocks. Falls back to the direct
    caption fetch when Supadata isn't configured or errors (a quota failure on
    the runner then surfaces as `blocked`, which the scan retries next run).
    """
    if supadata_client.is_configured():
        try:
            text = supadata_client.get_transcript_text(video_id)
            return ("ok", text) if (text and text.strip()) else ("no_captions", None)
        except supadata_client.TranscriptUnavailable:
            return "no_captions", None   # authoritative: don't retry this one forever
        except Exception as exc:
            # Never swallow this silently: a misconfigured key or bad param looks
            # identical to an IP block once we fall through to the direct fetch.
            logger.warning("Supadata transcript for %s failed (%s: %s) — falling back to the direct fetch",
                           video_id, type(exc).__name__, exc)
    return _direct_transcript(video_id)
