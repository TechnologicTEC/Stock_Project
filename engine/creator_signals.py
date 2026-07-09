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


def _already_stored(video_id: str) -> bool:
    with get_session() as s:
        return s.execute(
            select(CreatorVideo.id).where(CreatorVideo.video_id == video_id)
        ).scalar_one_or_none() is not None


def _extract_and_screen(video_id: str, transcript: str, summary: dict) -> None:
    """Pull the tickers discussed, snapshot each one's screener score, and store
    `video_mentions` rows. Best-effort: a failure here never loses the video."""
    from engine import screener  # local: heavy import kept off module load

    mentions = ticker_extraction.extract_mentions(transcript)
    if not mentions:
        print("      mentions: none", flush=True)
        return

    scored = {}
    try:
        for r in screener.screen_tickers([m.ticker for m in mentions]):
            scored[r.ticker] = r
    except Exception as exc:
        print(f"      screen FAILED (mentions kept, scores blank): {type(exc).__name__}: {exc}", flush=True)

    with get_session() as s:
        for m in mentions:
            r = scored.get(m.ticker)
            s.add(VideoMention(
                video_id=video_id, ticker=m.ticker, company_name=m.company_name, stance=m.stance,
                confidence=m.confidence, screener_score=(r.overall_score if r else None),
                recommendation=(r.recommendation if r else None), screened_at=utcnow(),
            ))
    summary["mentions"] = summary.get("mentions", 0) + len(mentions)
    print(f"      mentions: {', '.join(f'{m.ticker}[{m.stance}]' for m in mentions)}", flush=True)


def _process_video(creator: Creator, video: dict, summary: dict) -> None:
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

    if status == "ok" and transcript:
        try:
            _extract_and_screen(video["video_id"], transcript, summary)
        except Exception as exc:
            print(f"      extract FAILED (video kept): {type(exc).__name__}: {exc}", flush=True)


def scan_creators(video_limit: int = 15) -> dict:
    """Poll every active creator for new videos and store them. Returns a summary."""
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

    print(f"\ndone: {summary}", flush=True)
    return summary
