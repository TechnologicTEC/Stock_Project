"""
engine/sponsorship.py and the rule it serves: a stock presented in a paid
("sponsored") segment is kept and labelled, but never counted as the creator's
view (Tane, 16 Oct 2026). ZipTrader closes most videos with one — VNRX was
sponsored three times in six weeks, and counted as three bullish mentions.

The model is always stubbed; the database is conftest's in-memory SQLite.
"""
from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select

from db.models import Creator, CreatorVideo, VideoMention
from db.session import get_session
from engine import creator_signals, sponsorship, ticker_extraction
from engine.ticker_extraction import Mention
from engine.time_utils import utcnow

# Real announcements from stored transcripts, garbled tickers and all.
GORO_SEGMENT = ("And then at the end of today's video, we have our sponsored segment on gold "
                "group mining, ticker symbol G O on the NYC American. This is a gold and silver "
                "producer with two operating mines in Mexico.")
VNRX_SEGMENT = ("And then at the very end of the video, it'll be time for our sponsored segment "
                "on Volition RX, ticker symbol VNRX on the NYC American. They're a diagnostics "
                "company that's developed simple blood tests.")
NO_SPONSOR = "Today we look at Nvidia and Micron, two names I think you want to own."


def _m(ticker, company=None, stance="bullish"):
    return Mention(ticker=ticker, company_name=company, stance=stance, confidence=0.9)


@pytest.fixture
def model():
    """Stand in for the model: `model.answer` is what it returns."""
    class _Model:
        answer, calls, error = [], 0, None
    m = _Model()

    def ask(transcript, mentions):
        m.calls += 1
        if m.error:
            raise m.error
        return {t.upper() for t in m.answer}

    with patch.object(ticker_extraction, "_llm_available", return_value=True), \
         patch.object(sponsorship, "_ask_model", side_effect=ask):
        yield m


# --------------------------------------------------------------------------
# Deciding what was sponsored
# --------------------------------------------------------------------------

def test_a_video_with_no_sponsorship_wording_never_asks_the_model(model):
    assert sponsorship.sponsored_tickers(NO_SPONSOR, [_m("NVDA"), _m("MU")]) == set()
    assert model.calls == 0


def test_the_model_answer_is_kept_to_the_stocks_actually_found(model):
    model.answer = ["VNRX", "AAPL"]                        # AAPL was never extracted
    got = sponsorship.sponsored_tickers(VNRX_SEGMENT, [_m("VNRX", "VolitionRx"), _m("NVDA")])
    assert got == {"VNRX"}


def test_a_busy_model_defers_rather_than_guessing(model):
    model.error = RuntimeError("503 UNAVAILABLE")
    with pytest.raises(ticker_extraction.TransientExtractionError):
        sponsorship.sponsored_tickers(VNRX_SEGMENT, [_m("VNRX")])


def test_a_broken_model_falls_back_to_the_announcement_text(model):
    model.error = ValueError("bad json")
    got = sponsorship.sponsored_tickers(GORO_SEGMENT, [_m("GORO", "Goldgroup Mining Inc."),
                                                       _m("NVDA", "NVIDIA")])
    assert got == {"GORO"}


@pytest.mark.parametrize("segment, mention", [
    (GORO_SEGMENT, ("GORO", "Goldgroup Mining Inc.")),     # ticker garbled to "G O"
    (VNRX_SEGMENT, ("VNRX", "VolitionRx Limited")),
])
def test_the_text_fallback_reads_garbled_announcements(segment, mention):
    with patch.object(ticker_extraction, "_llm_available", return_value=False):
        got = sponsorship.sponsored_tickers(segment, [_m(*mention), _m("MU", "Micron Technology")])
    assert got == {mention[0]}


# --------------------------------------------------------------------------
# Through the scan, and into what the creator bot counts
# --------------------------------------------------------------------------

def _store_video(vid, transcript, days_ago=3):
    with get_session() as s:
        if not s.execute(select(Creator.id)).first():
            s.add(Creator(channel_id="UC" + "x" * 22, handle="@ZipTrader", display_name="ZipTrader"))
        s.add(CreatorVideo(creator_id=1, video_id=vid, title=vid, url="u",
                           published_at=utcnow() - timedelta(days=days_ago),
                           transcript_status="ok", transcript=transcript, processed_at=utcnow()))


def _extract(found):
    with patch.object(ticker_extraction, "extract_mentions", return_value=found), \
         patch("engine.screener.screen_tickers", return_value=[]):
        return creator_signals._extract_pending({})


def test_a_sponsored_mention_is_stored_labelled(model):
    model.answer = ["VNRX"]
    _store_video("V1", VNRX_SEGMENT + " " + NO_SPONSOR)
    _extract([_m("VNRX", "VolitionRx"), _m("NVDA", "NVIDIA")])
    with get_session() as s:
        got = {m.ticker: m.sponsored for m in s.execute(select(VideoMention)).scalars()}
    assert got == {"VNRX": True, "NVDA": False}


def test_a_busy_model_stores_nothing_and_leaves_the_video_for_the_next_run(model):
    model.error = RuntimeError("503 UNAVAILABLE")
    _store_video("V1", VNRX_SEGMENT)
    _extract([_m("VNRX", "VolitionRx")])
    with get_session() as s:
        assert s.execute(select(VideoMention)).first() is None
        assert s.execute(select(CreatorVideo.mentions_extracted_at)).scalar() is None


def test_three_sponsored_bullish_calls_do_not_put_a_stock_in_the_queue(model):
    """VNRX, Aug-Sep 2026: sponsored three times, bullish each time. Counted, that
    clears the entry bar (3 bullish); as adverts it is not there at all."""
    model.answer = ["VNRX"]
    for i, days in enumerate((20, 12, 4)):
        _store_video(f"V{i}", VNRX_SEGMENT, days_ago=days)
        _extract([_m("VNRX", "VolitionRx")])
    board = {e["ticker"]: e for e in creator_signals.mention_leaderboard(days=30, min_mentions=1)}
    assert "VNRX" not in board
    assert creator_signals.ticker_stance("VNRX") is None


def test_the_same_stock_discussed_on_its_merits_still_counts(model):
    """Only the paid segment is excluded, not the stock forever."""
    model.answer = ["VNRX"]
    _store_video("PAID", VNRX_SEGMENT, days_ago=10)
    _extract([_m("VNRX", "VolitionRx")])
    model.answer = []
    _store_video("OWN", "Here is why I like VolitionRx on its own merits.", days_ago=5)
    _extract([_m("VNRX", "VolitionRx")])
    board = {e["ticker"]: e for e in creator_signals.mention_leaderboard(days=30, min_mentions=1)}
    assert board["VNRX"]["mentions"] == 1 and board["VNRX"]["stances"]["bullish"] == 1


def test_a_sponsored_mention_adds_no_days_to_a_holding_clock(model):
    model.answer = ["GORO"]
    _store_video("PAID", GORO_SEGMENT, days_ago=2)
    _extract([_m("GORO", "Goldgroup Mining Inc.")])
    history = creator_signals.mention_history(["GORO"], since=utcnow() - timedelta(days=30))
    assert history == {}


def test_the_page_and_digest_still_show_it_as_sponsored(model):
    model.answer = ["VNRX"]
    _store_video("V1", VNRX_SEGMENT)
    _extract([_m("VNRX", "VolitionRx")])
    shaped = creator_signals.signals_for_videos(["V1"])
    assert shaped[0]["mentions"][0]["sponsored"] is True
    from engine import creator_digest
    assert "Sponsored" in creator_digest.build_html(shaped)


def test_a_model_that_never_runs_is_reported_not_silent(model, caplog):
    """A non-transient model failure falls back to the announcement text. It
    must say so: on the first backfill preview the model call was failing on
    every video ("the client has been closed") and the results looked normal."""
    model.error = RuntimeError("Cannot send a request, as the client has been closed.")
    with caplog.at_level("WARNING", logger="engine.sponsorship"):
        got = sponsorship.sponsored_tickers(VNRX_SEGMENT, [_m("VNRX", "VolitionRx")])
    assert got == {"VNRX"}                                  # the text match still found it
    assert "model failed" in caplog.text
