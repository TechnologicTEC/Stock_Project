"""
engine/creator_signals.py — Phase 1 orchestrator: seed creators, detect new
videos, store them, dedupe, and leave transient failures unstored for retry.
youtube_client is mocked; the DB is the in-memory SQLite from conftest.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import select

from db.models import Creator, CreatorVideo, VideoMention
from db.session import get_session
from engine import creator_signals
from engine.ticker_extraction import Mention


@pytest.fixture(autouse=True)
def _no_extraction_by_default():
    """Most tests focus on video detection/storage — stub extraction out so they
    don't hit the SEC list / LLM. The extraction tests below opt back in."""
    with patch("engine.ticker_extraction.extract_mentions", return_value=[]):
        yield


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


def test_scan_extracts_and_screens_mentions():
    mentions = [Mention("NVDA", "NVIDIA", "bullish", 0.9), Mention("AAPL", "Apple", "bearish", 0.9)]
    results = [SimpleNamespace(ticker="NVDA", overall_score=80.0, recommendation="Buy"),
               SimpleNamespace(ticker="AAPL", overall_score=55.0, recommendation="Hold")]
    with patch("engine.data_sources.youtube_client.latest_videos", return_value=[_video("AAA")]), \
         patch("engine.data_sources.youtube_client.get_transcript", return_value=("ok", "transcript text")), \
         patch("engine.ticker_extraction.extract_mentions", return_value=mentions), \
         patch("engine.screener.screen_tickers", return_value=results):
        summary = creator_signals.scan_creators()

    with get_session() as s:
        rows = {r.ticker: r for r in s.execute(select(VideoMention)).scalars()}
    assert set(rows) == {"NVDA", "AAPL"} and summary["mentions"] == 2
    assert rows["NVDA"].screener_score == 80.0 and rows["NVDA"].recommendation == "Buy"
    assert rows["NVDA"].stance == "bullish" and rows["NVDA"].video_id == "AAA"


def test_screen_failure_keeps_mentions_with_blank_scores():
    with patch("engine.data_sources.youtube_client.latest_videos", return_value=[_video("AAA")]), \
         patch("engine.data_sources.youtube_client.get_transcript", return_value=("ok", "t")), \
         patch("engine.ticker_extraction.extract_mentions",
               return_value=[Mention("NVDA", "NVIDIA", "neutral", 0.9)]), \
         patch("engine.screener.screen_tickers", side_effect=RuntimeError("api down")):
        creator_signals.scan_creators()

    with get_session() as s:
        row = s.execute(select(VideoMention).where(VideoMention.ticker == "NVDA")).scalar_one()
    assert row.screener_score is None and row.recommendation is None and row.stance == "neutral"


def test_recent_signals_orders_and_shapes_the_read_model():
    creator_signals.seed_default_creators()
    with get_session() as s:
        cid = s.execute(select(Creator.id)).scalar_one()
        s.add(CreatorVideo(creator_id=cid, video_id="AAA", title="Newer", url="http://y/AAA",
                           transcript_status="ok", transcript="t", published_at=datetime(2026, 7, 8)))
        s.add(CreatorVideo(creator_id=cid, video_id="BBB", title="Older", url="http://y/BBB",
                           transcript_status="ok", transcript="t", published_at=datetime(2026, 7, 1)))
        s.add(CreatorVideo(creator_id=cid, video_id="CCC", title="No caps", url="u",
                           transcript_status="no_captions", published_at=datetime(2026, 7, 9)))
        s.add(VideoMention(video_id="AAA", ticker="NVDA", stance="bullish", screener_score=80.0, recommendation="Buy"))
        s.add(VideoMention(video_id="AAA", ticker="AAPL", stance="bearish", screener_score=90.0, recommendation="Hold"))

    sigs = creator_signals.recent_signals()
    assert [x["video_id"] for x in sigs] == ["AAA", "BBB"]           # newest first; no_captions excluded
    assert sigs[0]["creator"] == "ZipTrader"
    assert [m["ticker"] for m in sigs[0]["mentions"]] == ["AAPL", "NVDA"]   # higher screener score first
    assert sigs[1]["mentions"] == []


# --------------------------------------------------------------------------
# Phase 5: extraction retry flag + multi-creator management
# --------------------------------------------------------------------------

def test_extraction_retries_previously_unextracted_video():
    creator_signals.seed_default_creators()
    with get_session() as s:
        cid = s.execute(select(Creator.id)).scalar_one()
        s.add(CreatorVideo(creator_id=cid, video_id="AAA", title="t", url="u",
                           transcript_status="ok", transcript="body"))  # mentions_extracted_at is NULL

    # No NEW videos this run, but the pending one should still get extracted.
    with patch("engine.data_sources.youtube_client.latest_videos", return_value=[]), \
         patch("engine.ticker_extraction.extract_mentions",
               return_value=[Mention("NVDA", "NVIDIA", "bullish", 0.9)]), \
         patch("engine.screener.screen_tickers",
               return_value=[SimpleNamespace(ticker="NVDA", overall_score=70.0, recommendation="Buy")]):
        creator_signals.scan_creators()

    with get_session() as s:
        assert s.execute(select(VideoMention).where(VideoMention.ticker == "NVDA")).scalar_one()
        v = s.execute(select(CreatorVideo).where(CreatorVideo.video_id == "AAA")).scalar_one()
        assert v.mentions_extracted_at is not None


def test_transient_extraction_failure_leaves_flag_unset_for_retry():
    from engine.ticker_extraction import TransientExtractionError
    with patch("engine.data_sources.youtube_client.latest_videos", return_value=[_video("AAA")]), \
         patch("engine.data_sources.youtube_client.get_transcript", return_value=("ok", "body")), \
         patch("engine.ticker_extraction.extract_mentions", side_effect=TransientExtractionError("quota")):
        summary = creator_signals.scan_creators()

    with get_session() as s:
        v = s.execute(select(CreatorVideo).where(CreatorVideo.video_id == "AAA")).scalar_one()
        assert v.mentions_extracted_at is None                       # not marked -> retried next run
        assert s.execute(select(VideoMention)).first() is None
    assert summary.get("extract_deferred") == 1


def test_add_creator_resolves_and_inserts_then_reactivates():
    resolved = {"channel_id": "UCnew1234567890abcdef22", "display_name": "New Guy", "handle": "@newguy"}
    with patch("engine.data_sources.youtube_client.resolve_channel", return_value=resolved):
        info = creator_signals.add_creator("@newguy")
    assert info["channel_id"] == "UCnew1234567890abcdef22" and info["reactivated"] is False
    row = {c["channel_id"]: c for c in creator_signals.list_creators()}[resolved["channel_id"]]
    assert row["display_name"] == "New Guy" and row["active"]

    creator_signals.set_creator_active(resolved["channel_id"], False)
    with patch("engine.data_sources.youtube_client.resolve_channel", return_value=resolved):
        again = creator_signals.add_creator("@newguy")
    assert again["reactivated"] is True
    assert {c["channel_id"]: c["active"] for c in creator_signals.list_creators()}[resolved["channel_id"]]


def test_set_creator_active_unknown_returns_false():
    assert creator_signals.set_creator_active("UCdoesnotexist000000000", True) is False


def test_seed_default_creators_is_idempotent():
    assert creator_signals.seed_default_creators() == 1     # ZipTrader inserted
    assert creator_signals.seed_default_creators() == 0     # second call adds nothing
    assert [c["display_name"] for c in creator_signals.list_creators()] == ["ZipTrader"]


def test_scan_sends_digest_only_for_videos_with_new_mentions():
    with patch("engine.data_sources.youtube_client.latest_videos", return_value=[_video("AAA")]), \
         patch("engine.data_sources.youtube_client.get_transcript", return_value=("ok", "body")), \
         patch("engine.ticker_extraction.extract_mentions",
               return_value=[Mention("NVDA", "NVIDIA", "bullish", 0.9)]), \
         patch("engine.screener.screen_tickers",
               return_value=[SimpleNamespace(ticker="NVDA", overall_score=70.0, recommendation="Buy")]), \
         patch("engine.creator_digest.send_digest", return_value=True) as digest:
        summary = creator_signals.scan_creators()

    assert summary.get("digest_sent") is True
    sent = digest.call_args.args[0]
    assert sent[0]["video_id"] == "AAA" and sent[0]["mentions"][0]["ticker"] == "NVDA"


def test_scan_sends_no_digest_when_no_mentions_found():
    # extract_mentions is stubbed to [] by the autouse fixture -> nothing to report.
    with patch("engine.data_sources.youtube_client.latest_videos", return_value=[_video("AAA")]), \
         patch("engine.data_sources.youtube_client.get_transcript", return_value=("ok", "body")), \
         patch("engine.creator_digest.send_digest") as digest:
        summary = creator_signals.scan_creators()

    digest.assert_not_called()
    assert "digest_sent" not in summary
