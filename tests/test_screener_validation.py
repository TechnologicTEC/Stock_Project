from datetime import date, timedelta
from unittest.mock import patch

from engine import screener_validation as sv


# --------------------------------------------------------------------------
# summarize — pure, no network
# --------------------------------------------------------------------------

def test_summarize_reports_positive_ic_when_scores_track_returns():
    # Monotonic: higher score -> higher forward return, so rank correlation ~ +1.
    points = [
        {"date": date(2022, 1, 1), "score": 30, "recommendation": "Sell", "forward_return_pct": -5.0},
        {"date": date(2022, 2, 1), "score": 50, "recommendation": "Hold", "forward_return_pct": 1.0},
        {"date": date(2022, 3, 1), "score": 65, "recommendation": "Buy", "forward_return_pct": 6.0},
        {"date": date(2022, 4, 1), "score": 80, "recommendation": "Strong Buy", "forward_return_pct": 12.0},
        {"date": date(2022, 5, 1), "score": 85, "recommendation": "Strong Buy", "forward_return_pct": 15.0},
    ]
    summary = sv.summarize(points)

    assert summary["n"] == 5
    assert summary["insufficient_data"] is False
    assert summary["information_coefficient"] == 1.0  # perfectly monotonic
    band_avgs = {b["band"]: b["avg_forward_return_pct"] for b in summary["bands"]}
    assert band_avgs["0–40 (Sell)"] == -5.0
    assert band_avgs["75–100 (Strong Buy)"] == 13.5  # mean(12, 15)


def test_summarize_flags_insufficient_data():
    points = [{"date": date(2022, 1, 1), "score": 60, "recommendation": "Buy", "forward_return_pct": 3.0}]
    summary = sv.summarize(points)
    assert summary["insufficient_data"] is True
    assert summary["information_coefficient"] is None


# --------------------------------------------------------------------------
# walk_forward — score/return boundaries mocked
# --------------------------------------------------------------------------

def test_walk_forward_collects_score_and_forward_return_points():
    def fake_score(ticker, as_of):
        return {"overall_score": 72.0, "recommendation": "Buy"}

    def fake_forward(ticker, as_of, horizon_days):
        return 4.0

    with patch("engine.screener_validation.price_history.ensure_cached"), \
         patch("engine.screener_validation.screener_history.historical_screener_score", side_effect=fake_score), \
         patch("engine.screener_validation.forward_return_pct", side_effect=fake_forward):
        points = sv.walk_forward("TEST", date(2022, 1, 1), date(2022, 4, 1), step_days=30, horizon_days=91)

    # 2022-01-01, 01-31, 03-02, 04-01 -> 4 monthly points
    assert len(points) == 4
    assert all(p["score"] == 72.0 and p["forward_return_pct"] == 4.0 for p in points)


def test_walk_forward_skips_dates_without_a_score_or_a_forward_return():
    calls = {"n": 0}

    def fake_score(ticker, as_of):
        calls["n"] += 1
        return {"overall_score": None, "recommendation": "Insufficient data"}  # never scorable

    with patch("engine.screener_validation.price_history.ensure_cached"), \
         patch("engine.screener_validation.screener_history.historical_screener_score", side_effect=fake_score), \
         patch("engine.screener_validation.forward_return_pct", return_value=5.0):
        points = sv.walk_forward("TEST", date(2022, 1, 1), date(2022, 3, 1), step_days=30)

    assert points == []
    assert calls["n"] >= 2  # it did iterate the dates, just found nothing scorable


def test_walk_forward_does_not_score_dates_whose_forward_window_has_not_elapsed():
    # Dates within `horizon_days` of today have no completed forward return yet,
    # so they must be skipped (rather than requesting future prices).
    today = date.today()
    with patch("engine.screener_validation.price_history.ensure_cached"), \
         patch("engine.screener_validation.screener_history.historical_screener_score",
               return_value={"overall_score": 70.0, "recommendation": "Buy"}), \
         patch("engine.screener_validation.forward_return_pct", return_value=5.0):
        points = sv.walk_forward("TEST", today - timedelta(days=250), today, step_days=20, horizon_days=91)

    assert points  # the older dates still score
    assert all(p["date"] <= today - timedelta(days=91) for p in points)
