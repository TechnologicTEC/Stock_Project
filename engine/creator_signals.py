"""
Creator Signals orchestrator (docs/creator-signals-plan.md).

Phase 1: for each active creator, poll the channel feed, find videos we haven't
seen, fetch each transcript, and store a `creator_videos` row. Extraction +
screening (populating `video_mentions`) arrives in Phase 2.

Runs as a scheduled job (scripts/scan_creators.py) against a BYPASSRLS Postgres
role — these tables are global/shared, not per-user. Idempotent: a video is
stored once (deduped by `video_id`), and transient failures (`blocked`/`error`)
are deliberately NOT persisted so the next run retries them.
"""
from __future__ import annotations

from sqlalchemy import select

from db.models import Creator, CreatorVideo, VideoMention
from db.session import get_session
from engine import ticker_extraction
from engine.data_sources import youtube_client
from engine.time_utils import utcnow

# Seeded on first run; adding creators later is data, not code.
DEFAULT_CREATORS = [
    {"channel_id": "UC0BGhWsIbV7Dm-lsvhdlMbA", "handle": "@ZipTrader", "display_name": "ZipTrader"},
]

# transcript_status values we persist immediately (terminal). blocked/error are
# left unstored so the video is re-detected and retried on the next run.
_PERSIST_STATUSES = {"ok", "no_captions"}


def seed_default_creators() -> int:
    """Insert any DEFAULT_CREATORS not already present. Returns how many added."""
    added = 0
    with get_session() as s:
        for c in DEFAULT_CREATORS:
            exists = s.execute(select(Creator).where(Creator.channel_id == c["channel_id"])).scalar_one_or_none()
            if exists is None:
                s.add(Creator(**c))
                added += 1
    return added


def add_creator(url_or_handle: str) -> dict:
    """Resolve a channel URL/@handle and add it (or re-activate it if present).
    Returns {channel_id, display_name, reactivated}."""
    info = youtube_client.resolve_channel(url_or_handle)
    with get_session() as s:
        existing = s.execute(select(Creator).where(Creator.channel_id == info["channel_id"])).scalar_one_or_none()
        if existing is not None:
            existing.active = True
            if not existing.display_name and info.get("display_name"):
                existing.display_name = info["display_name"]
            return {"channel_id": existing.channel_id, "display_name": existing.display_name, "reactivated": True}
        s.add(Creator(channel_id=info["channel_id"], handle=info.get("handle"),
                      display_name=info.get("display_name")))
    return {"channel_id": info["channel_id"], "display_name": info.get("display_name"), "reactivated": False}


def set_creator_active(channel_id: str, active: bool) -> bool:
    """Enable/disable a creator (disabled ones are skipped by scans; their stored
    videos stay). Returns False if the channel isn't known."""
    with get_session() as s:
        c = s.execute(select(Creator).where(Creator.channel_id == channel_id)).scalar_one_or_none()
        if c is None:
            return False
        c.active = active
        return True


def list_creators() -> list[dict]:
    """All creators (for the management UI), oldest first."""
    with get_session() as s:
        return [{"channel_id": c.channel_id, "display_name": c.display_name or c.handle or c.channel_id,
                 "handle": c.handle, "active": c.active}
                for c in s.execute(select(Creator).order_by(Creator.added_at)).scalars()]


def _already_stored(video_id: str) -> bool:
    with get_session() as s:
        return s.execute(
            select(CreatorVideo.id).where(CreatorVideo.video_id == video_id)
        ).scalar_one_or_none() is not None


def _process_video(creator: Creator, video: dict, summary: dict) -> None:
    """Store a newly-seen video (extraction happens later in _extract_pending)."""
    if _already_stored(video["video_id"]):
        return
    status, transcript = youtube_client.get_transcript(video["video_id"])
    summary["new_videos"] += 1
    summary[status] = summary.get(status, 0) + 1

    if status not in _PERSIST_STATUSES:
        print(f"    {video['video_id']} {status} (not stored — will retry): {video['title'][:60]}", flush=True)
        return

    with get_session() as s:
        s.add(CreatorVideo(
            creator_id=creator.id, video_id=video["video_id"], title=video["title"], url=video["url"],
            published_at=video["published_at"], transcript_status=status, transcript=transcript,
            processed_at=utcnow(),
        ))
    chars = len(transcript) if transcript else 0
    print(f"    {video['video_id']} {status} ({chars} chars): {video['title'][:60]}", flush=True)


def _screen_and_store_mentions(video_id: str, transcript: str) -> int:
    """Extract tickers, snapshot each one's screener score, store `video_mentions`.
    Returns the count. Raises if extraction should be retried later (e.g. the LLM
    hit a transient quota error) — the caller then leaves the retry flag unset."""
    from engine import screener  # local: heavy import kept off module load

    mentions = ticker_extraction.extract_mentions(transcript)  # may raise TransientExtractionError
    if not mentions:
        print(f"    {video_id} mentions: none", flush=True)
        return 0

    scored = {}
    try:
        for r in screener.screen_tickers([m.ticker for m in mentions]):
            scored[r.ticker] = r
    except Exception as exc:
        print(f"    {video_id} screen FAILED (mentions kept, scores blank): {type(exc).__name__}: {exc}", flush=True)

    with get_session() as s:
        for m in mentions:
            r = scored.get(m.ticker)
            s.add(VideoMention(
                video_id=video_id, ticker=m.ticker, company_name=m.company_name, stance=m.stance,
                confidence=m.confidence, screener_score=(r.overall_score if r else None),
                recommendation=(r.recommendation if r else None), screened_at=utcnow(),
            ))
    print(f"    {video_id} mentions: {', '.join(f'{m.ticker}[{m.stance}]' for m in mentions)}", flush=True)
    return len(mentions)


def _mark_extracted(video_id: str) -> None:
    with get_session() as s:
        v = s.execute(select(CreatorVideo).where(CreatorVideo.video_id == video_id)).scalar_one_or_none()
        if v is not None:
            v.mentions_extracted_at = utcnow()


def _extract_pending(summary: dict, limit: int = 25) -> None:
    """Extract+screen every captioned video not yet extracted — this run's new
    videos plus any whose extraction failed before (retry). On success the retry
    flag is set; a transient/failed extraction leaves it unset to try again."""
    with get_session() as s:
        pending = [(v.video_id, v.transcript) for v in s.execute(
            select(CreatorVideo).where(
                CreatorVideo.transcript_status == "ok",
                CreatorVideo.transcript.is_not(None),
                CreatorVideo.mentions_extracted_at.is_(None),
            ).order_by(CreatorVideo.processed_at.desc()).limit(limit)
        ).scalars().all()]

    for video_id, transcript in pending:
        try:
            summary["mentions"] = summary.get("mentions", 0) + _screen_and_store_mentions(video_id, transcript)
            _mark_extracted(video_id)
        except Exception as exc:
            summary["extract_deferred"] = summary.get("extract_deferred", 0) + 1
            print(f"    {video_id} extract deferred (will retry): {type(exc).__name__}: {exc}", flush=True)


def scan_creators(video_limit: int = 15) -> dict:
    """Poll active creators for new videos, store them, then extract+screen any
    videos still pending. Returns a summary."""
    seed_default_creators()
    summary: dict = {"creators": 0, "new_videos": 0}
    with get_session() as s:
        creators = s.execute(select(Creator).where(Creator.active.is_(True))).scalars().all()

    for creator in creators:
        summary["creators"] += 1
        print(f"  {creator.display_name or creator.channel_id}:", flush=True)
        try:
            videos = youtube_client.latest_videos(creator.channel_id, limit=video_limit)
        except Exception as exc:
            print(f"    feed FAILED: {type(exc).__name__}: {exc}", flush=True)
            continue
        for video in videos:
            _process_video(creator, video, summary)

    _extract_pending(summary)
    print(f"\ndone: {summary}", flush=True)
    return summary


def recent_signals(limit_videos: int = 12) -> list[dict]:
    """Recent captioned videos and their screened mentions, newest first — the
    read model for the Creator Signals page. Global/shared data (no user scope)."""
    with get_session() as s:
        creators = {c.id: (c.display_name or c.handle or c.channel_id)
                    for c in s.execute(select(Creator)).scalars()}
        rows = s.execute(
            select(CreatorVideo).where(CreatorVideo.transcript_status == "ok")
            .order_by(CreatorVideo.processed_at.desc()).limit(limit_videos * 3)
        ).scalars().all()
        rows.sort(key=lambda v: (v.published_at or v.processed_at), reverse=True)

        out = []
        for v in rows[:limit_videos]:
            mentions = s.execute(
                select(VideoMention).where(VideoMention.video_id == v.video_id)
            ).scalars().all()
            mentions.sort(key=lambda m: (m.screener_score is None, -(m.screener_score or 0.0), m.ticker))
            out.append({
                "video_id": v.video_id, "title": v.title, "url": v.url,
                "published_at": v.published_at, "creator": creators.get(v.creator_id, "Unknown"),
                "mentions": [{
                    "ticker": m.ticker, "company_name": m.company_name, "stance": m.stance,
                    "screener_score": m.screener_score, "recommendation": m.recommendation,
                    "confidence": m.confidence,
                } for m in mentions],
            })
    return out
