from datetime import date, timedelta
from unittest.mock import MagicMock, patch

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
    assert summary["trend"] is None


# --------------------------------------------------------------------------
# Trend line — the least-squares fit drawn on the scatter
# --------------------------------------------------------------------------

def _points(pairs):
    return [{"date": date(2022, 1, 1) + timedelta(days=i), "score": s,
             "recommendation": "Hold", "forward_return_pct": r}
            for i, (s, r) in enumerate(pairs)]


def test_trend_fits_a_line_through_a_perfect_relationship():
    # y = 0.5x - 20 exactly -> slope 0.5, endpoints sit on the line.
    summary = sv.summarize(_points([(40, 0.0), (50, 5.0), (60, 10.0), (70, 15.0), (80, 20.0)]))
    trend = summary["trend"]
    assert trend["slope"] == 0.5 and trend["intercept"] == -20.0
    assert (trend["x0"], trend["y0"]) == (40.0, 0.0)
    assert (trend["x1"], trend["y1"]) == (80.0, 20.0)
    assert trend["pearson_r"] == 1.0


def test_trend_slope_is_negative_when_higher_scores_precede_lower_returns():
    summary = sv.summarize(_points([(30, 10.0), (45, 6.0), (60, 2.0), (75, -3.0), (90, -8.0)]))
    assert summary["trend"]["slope"] < 0
    assert summary["information_coefficient"] < 0        # rank IC and raw fit agree on direction


def test_no_trend_line_when_every_score_is_identical():
    # Zero variance in x -> slope undefined; don't draw a meaningless line.
    summary = sv.summarize(_points([(60, r) for r in (-4.0, -1.0, 0.0, 3.0, 7.0)]))
    assert summary["trend"] is None
    assert summary["insufficient_data"] is False         # the IC section still renders


def test_track_record_interprets_a_remembered_ic_by_tier():
    from engine import projections
    cases = {0.20: "positive", 0.05: "weak", 0.00: "none", -0.20: "negative"}
    for ic, expected in cases.items():
        projections.remember_validation_ic("NVDA", ic, n=18, horizon_days=21, include_news=True)
        tr = sv.track_record("nvda")
        assert tr["stance"] == expected and tr["ic"] == round(ic, 3) and tr["n"] == 18


def test_track_record_flags_when_the_ic_excludes_news_sentiment():
    from engine import projections
    # Validated with news OFF (the default) -> the IC covers the core, not the
    # live score, and the note must say so (#7).
    projections.remember_validation_ic("NVDA", 0.05, n=20, include_news=False)
    tr = sv.track_record("NVDA")
    assert tr["covers_news"] is False
    assert "news-sentiment factor can't be reconstructed" in tr["scope_note"]

    # Validated with news ON -> no exclusion caveat.
    projections.remember_validation_ic("NVDA", 0.05, n=20, include_news=True)
    tr = sv.track_record("NVDA")
    assert tr["covers_news"] is True and tr["scope_note"] == ""


def test_track_record_is_none_when_never_validated():
    assert sv.track_record("NEVERVALIDATED") is None


def test_negative_ic_track_record_warns_it_worked_against_you():
    from engine import projections
    projections.remember_validation_ic("BBAI", -0.15, n=12)
    tr = sv.track_record("BBAI")
    assert tr["stance"] == "negative" and "worked against you" in tr["text"]


def test_outlier_swings_the_raw_trend_but_not_the_rank_ic():
    # One wild point drags the least-squares slope negative (-1.41) while the rank
    # IC stays positive (+0.14) — exactly why the page says to compare the two on
    # direction, not magnitude, and why the trend line alone would mislead.
    summary = sv.summarize(_points([(40, 1.0), (50, 2.0), (60, 3.0), (70, 4.0), (80, 5.0), (90, -100.0)]))
    assert summary["information_coefficient"] > 0        # ranks: mostly still increasing
    assert summary["trend"]["slope"] < 0                 # raw fit: dragged down by the outlier
    assert summary["trend"]["pearson_r"] < 0             # ...and so is the raw correlation


# --------------------------------------------------------------------------
# walk_forward — score/return boundaries mocked
# --------------------------------------------------------------------------

def test_walk_forward_collects_score_and_forward_return_points():
    def fake_score(ticker, as_of, include_news=True):
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

    def fake_score(ticker, as_of, include_news=True):
        calls["n"] += 1
        return {"overall_score": None, "recommendation": "Insufficient data"}  # never scorable

    with patch("engine.screener_validation.price_history.ensure_cached"), \
         patch("engine.screener_validation.screener_history.historical_screener_score", side_effect=fake_score), \
         patch("engine.screener_validation.forward_return_pct", return_value=5.0):
        points = sv.walk_forward("TEST", date(2022, 1, 1), date(2022, 3, 1), step_days=30)

    assert points == []
    assert calls["n"] >= 2  # it did iterate the dates, just found nothing scorable


def test_walk_forward_threads_include_news_flag():
    scored = MagicMock(return_value={"overall_score": 70.0, "recommendation": "Buy", "factor_scores": {}})
    with patch("engine.screener_validation.price_history.ensure_cached"), \
         patch("engine.screener_validation.screener_history.historical_screener_score", scored), \
         patch("engine.screener_validation.forward_return_pct", return_value=5.0):
        sv.walk_forward("TEST", date(2022, 1, 1), date(2022, 3, 1), step_days=30, include_news=False)

    assert scored.call_count >= 1
    assert all(call.kwargs.get("include_news") is False for call in scored.call_args_list)


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
