"""
engine/creator_signals.py — Phase 1 orchestrator: seed creators, detect new
videos, store them, dedupe, and leave transient failures unstored for retry.
youtube_client is mocked; the DB is the in-memory SQLite from conftest.
"""
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import select

from db.models import Creator, CreatorVideo
from db.session import get_session
from engine import creator_signals


def _video(vid, title="A video"):
    return {"video_id": vid, "title": title, "url": f"https://youtu.be/{vid}",
            "published_at": datetime(2026, 7, 8, tzinfo=timezone.utc)}


def _stored_video_ids():
    with get_session() as s:
        return sorted(s.execute(select(CreatorVideo.video_id)).scalars().all())


def test_scan_seeds_creator_and_stores_new_videos():
    with patch("engine.data_sources.youtube_client.latest_videos", return_value=[_video("AAA"), _video("BBB")]), \
         patch("engine.data_sources.youtube_client.get_transcript", return_value=("ok", "transcript text")):
        summary = creator_signals.scan_creators()

    with get_session() as s:
        creators = s.execute(select(Creator)).scalars().all()
    assert [c.display_name for c in creators] == ["ZipTrader"]        # seeded once
    assert _stored_video_ids() == ["AAA", "BBB"]
    assert summary["new_videos"] == 2 and summary["ok"] == 2

    with get_session() as s:
        row = s.execute(select(CreatorVideo).where(CreatorVideo.video_id == "AAA")).scalar_one()
    assert row.transcript_status == "ok" and row.transcript == "transcript text"


def test_scan_skips_already_stored_videos():
    creator_signals.seed_default_creators()
    with get_session() as s:
        cid = s.execute(select(Creator.id)).scalar_one()
        s.add(CreatorVideo(creator_id=cid, video_id="AAA", title="old", url="u",
                           transcript_status="ok", transcript="x"))

    with patch("engine.data_sources.youtube_client.latest_videos", return_value=[_video("AAA"), _video("CCC")]), \
         patch("engine.data_sources.youtube_client.get_transcript", return_value=("ok", "t")) as gt:
        summary = creator_signals.scan_creators()

    assert _stored_video_ids() == ["AAA", "CCC"]                      # AAA not duplicated
    gt.assert_called_once()                                          # transcript only fetched for the new one
    assert summary["new_videos"] == 1


def test_blocked_transcript_is_not_persisted_so_it_retries():
    with patch("engine.data_sources.youtube_client.latest_videos", return_value=[_video("AAA")]), \
         patch("engine.data_sources.youtube_client.get_transcript", return_value=("blocked", None)):
        summary = creator_signals.scan_creators()
    assert _stored_video_ids() == []                                 # nothing stored -> next run retries
    assert summary["new_videos"] == 1 and summary["blocked"] == 1


def test_no_captions_is_persisted_with_null_transcript():
    with patch("engine.data_sources.youtube_client.latest_videos", return_value=[_video("AAA")]), \
         patch("engine.data_sources.youtube_client.get_transcript", return_value=("no_captions", None)):
        creator_signals.scan_creators()
    with get_session() as s:
        row = s.execute(select(CreatorVideo).where(CreatorVideo.video_id == "AAA")).scalar_one()
    assert row.transcript_status == "no_captions" and row.transcript is None


def test_feed_failure_for_one_creator_does_not_crash():
    with patch("engine.data_sources.youtube_client.latest_videos", side_effect=RuntimeError("feed down")), \
         patch("engine.data_sources.youtube_client.get_transcript") as gt:
        summary = creator_signals.scan_creators()
    assert summary["new_videos"] == 0
    gt.assert_not_called()
