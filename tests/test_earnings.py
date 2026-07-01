from unittest.mock import patch

from engine import earnings


# --------------------------------------------------------------------------
# Earnings surprises (Finnhub calendar)
# --------------------------------------------------------------------------

def test_get_surprises_parses_sorts_and_drops_unreported():
    raw = {"earningsCalendar": [
        {"date": "2026-05-01", "epsActual": 1.2, "epsEstimate": 1.1, "revenueActual": 1000, "revenueEstimate": 950},
        {"date": "2026-02-01", "epsActual": 0.9, "epsEstimate": 1.0, "revenueActual": 800, "revenueEstimate": 820},
        {"date": "2026-08-01", "epsActual": None, "epsEstimate": 1.3},  # future — should be dropped
    ]}
    with patch("engine.earnings.finnhub_client.get_earnings_calendar", return_value=raw):
        rows = earnings.get_surprises("AAPL")

    assert [r["period"] for r in rows] == ["2026-05-01", "2026-02-01"]  # newest first, future dropped
    assert rows[0]["beat"] is True
    assert rows[0]["eps_surprise"] == 0.1
    assert rows[0]["eps_surprise_pct"] == 9.1
    assert rows[1]["beat"] is False


def test_get_surprises_returns_empty_on_failure():
    with patch("engine.earnings.finnhub_client.get_earnings_calendar", side_effect=RuntimeError("403")):
        assert earnings.get_surprises("AAPL") == []


# --------------------------------------------------------------------------
# Earnings press release (EDGAR 8-K EX-99.1) + sentiment
# --------------------------------------------------------------------------

def test_get_press_release_attaches_sentiment():
    release = {"filing_date": "2026-05-01", "url": "http://sec/ex99.htm", "text": "Record revenue and strong growth"}
    with patch("engine.earnings.edgar_client.get_cik_for_ticker", return_value="0000320193"), \
         patch("engine.earnings.edgar_client.get_8k_press_release", return_value=release), \
         patch("engine.earnings.sentiment.is_available", return_value=True), \
         patch("engine.earnings.sentiment.score_text", return_value=0.62):
        result = earnings.get_press_release("AAPL")

    assert result["sentiment_score"] == 0.62
    assert result["url"] == "http://sec/ex99.htm"


def test_get_press_release_none_when_not_a_us_filer():
    with patch("engine.earnings.edgar_client.get_cik_for_ticker", return_value=None):
        assert earnings.get_press_release("XXXX") is None


def test_get_press_release_none_when_no_ex99_exhibit():
    with patch("engine.earnings.edgar_client.get_cik_for_ticker", return_value="0000320193"), \
         patch("engine.earnings.edgar_client.get_8k_press_release", return_value=None):
        assert earnings.get_press_release("AAPL") is None


# --------------------------------------------------------------------------
# analyze_ticker report
# --------------------------------------------------------------------------

def test_analyze_ticker_builds_beat_and_sentiment_summary():
    raw = {"earningsCalendar": [
        {"date": "2026-05-01", "epsActual": 1.2, "epsEstimate": 1.1, "revenueActual": 1, "revenueEstimate": 1},
    ]}
    release = {"filing_date": "2026-05-01", "url": "http://sec/ex99.htm", "text": "Great quarter"}
    with patch("engine.earnings.finnhub_client.get_earnings_calendar", return_value=raw), \
         patch("engine.earnings.edgar_client.get_cik_for_ticker", return_value="0000320193"), \
         patch("engine.earnings.edgar_client.get_8k_press_release", return_value=release), \
         patch("engine.earnings.sentiment.is_available", return_value=True), \
         patch("engine.earnings.sentiment.score_text", return_value=0.5):
        analysis = earnings.analyze_ticker("AAPL")

    assert analysis.latest["beat"] is True
    assert analysis.has_release is True
    assert "beat" in analysis.summary
    assert "Positive" in analysis.summary
    assert analysis.release["sentiment_label"] == "Positive"


def test_analyze_ticker_with_nothing_found():
    with patch("engine.earnings.finnhub_client.get_earnings_calendar", return_value={"earningsCalendar": []}), \
         patch("engine.earnings.edgar_client.get_cik_for_ticker", return_value=None):
        analysis = earnings.analyze_ticker("ZZZZ")

    assert analysis.surprises == []
    assert analysis.release is None
    assert "No earnings data" in analysis.summary
