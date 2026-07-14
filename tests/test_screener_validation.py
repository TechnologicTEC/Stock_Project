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


def _fpt(ticker, fwd, factors, score=50.0):
    return {"date": date(2022, 1, 1), "ticker": ticker, "score": score,
            "recommendation": "Hold", "forward_return_pct": fwd, "factors": factors}


def test_factor_ic_measures_each_factor_against_forward_return():
    # momentum score tracks the forward return perfectly; sentiment is the exact
    # opposite; valuation is constant (uninformative).
    pts = [_fpt(f"T{i}", float(i), {"momentum": float(i * 10), "sentiment": float(90 - i * 10),
                                    "valuation": 50.0}) for i in range(6)]
    fic = sv.factor_information_coefficients(pts)
    assert fic["momentum"]["ic"] == 1.0 and fic["momentum"]["n"] == 6
    assert fic["sentiment"]["ic"] == -1.0
    assert fic["valuation"]["ic"] is None            # zero variance -> undefined
    assert fic["momentum"]["label"] == "Momentum / Technical"


def test_factor_ic_needs_the_minimum_sample_and_skips_none():
    pts = [_fpt(f"T{i}", float(i), {"momentum": (None if i < 3 else float(i))}) for i in range(6)]
    fic = sv.factor_information_coefficients(pts)
    assert fic["momentum"]["n"] == 3 and fic["momentum"]["ic"] is None   # only 3 usable (<5)


def test_pooled_walk_forward_tags_points_and_reports_progress():
    def fake_wf(ticker, *a, **k):
        return [{"score": 60.0, "forward_return_pct": 2.0, "factors": {"momentum": 70.0}, "date": date(2022, 1, 1)}]
    seen = []
    with patch("engine.screener_validation.walk_forward", side_effect=fake_wf):
        pts = sv.pooled_walk_forward(["aapl", "msft"], date(2022, 1, 1), date(2022, 6, 1),
                                     on_progress=lambda d, t, tk: seen.append((d, t, tk)))
    assert [p["ticker"] for p in pts] == ["AAPL", "MSFT"]     # tagged
    assert seen == [(1, 2, "aapl"), (2, 2, "msft")]           # progress reported


def test_summarize_pooled_adds_ticker_count_and_per_factor_ic():
    pts = [_fpt("A" if i % 2 else "B", float(i), {"momentum": float(i * 5)}, score=float(i * 10))
           for i in range(6)]
    s = sv.summarize_pooled(pts)
    assert s["n_tickers"] == 2
    assert "factor_ic" in s and "momentum" in s["factor_ic"]
    assert s["information_coefficient"] is not None            # pooled overall IC still computed


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


def test_pooled_result_survives_a_lost_session_via_the_store():
    # A pooled run can outlive the Streamlit websocket; session_state is then
    # empty on reconnect. The stored copy is what makes the result reappear.
    key = sv.pooled_cache_key(["aapl", "msft"], lookback_days=730, horizon_days=91,
                              step_days=30, include_news=True)
    assert sv.load_pooled_result(key) is None          # nothing stored yet

    summary = {"n": 40, "n_tickers": 2, "information_coefficient": 0.06,
               "insufficient_data": False, "bands": [], "trend": None,
               "factor_ic": {"momentum": {"label": "Momentum", "ic": 0.2, "n": 40}}}
    sv.save_pooled_result(key, summary)

    restored = sv.load_pooled_result(key)
    assert restored["n_tickers"] == 2 and restored["factor_ic"]["momentum"]["ic"] == 0.2


def test_pooled_cache_key_changes_with_the_settings():
    base = dict(lookback_days=730, horizon_days=91, step_days=30, include_news=False)
    k1 = sv.pooled_cache_key(["AAPL"], **base)
    assert k1 == sv.pooled_cache_key(["aapl"], **base)                      # case/order-insensitive
    assert k1 != sv.pooled_cache_key(["AAPL", "MSFT"], **base)              # ticker set matters
    assert k1 != sv.pooled_cache_key(["AAPL"], **{**base, "include_news": True})
