import json
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


# --------------------------------------------------------------------------
# Error bars. The raw observation count overstates the evidence badly, which
# invited reading "momentum works, fundamentals don't" off a table where every
# interval straddles zero. These lock the honest accounting in.
# --------------------------------------------------------------------------

def _series(ticker, n, *, step_days, start=date(2022, 1, 1), fwd=lambda i: float(i % 5)):
    return [{"date": start + timedelta(days=step_days * i), "ticker": ticker, "score": float(i),
             "recommendation": "Hold", "forward_return_pct": fwd(i), "factors": {"momentum": float(i)}}
            for i in range(n)]


def test_effective_sample_size_deflates_overlapping_return_windows():
    # 22 monthly samples of a 91-day forward return re-measure the same price move
    # ~3x over: only span/horizon genuinely fresh windows exist.
    pts = _series("AAA", 22, step_days=30)          # spans 630 days
    assert len(pts) == 22
    assert sv.effective_sample_size(pts, horizon_days=91) == 6   # 630 // 91


def test_effective_sample_size_deflates_perfectly_correlated_tickers():
    # Two names whose forward returns move identically are ONE bet, not two.
    pts = _series("AAA", 6, step_days=91, fwd=float) + _series("BBB", 6, step_days=91, fwd=float)
    # 5 independent windows each -> 10, halved by a +1.0 cross-correlation
    assert sv.effective_sample_size(pts, horizon_days=91) == 5


def test_effective_sample_size_does_not_inflate_on_diversified_tickers():
    # Negative/zero correlation means the names are diversifying — that must not
    # be "rewarded" with a bigger effective sample than we actually observed.
    pts = _series("AAA", 6, step_days=91, fwd=float) + _series("BBB", 6, step_days=91, fwd=lambda i: -float(i))
    assert sv.effective_sample_size(pts, horizon_days=91) == 10


def test_ic_standard_error_shrinks_with_the_sample():
    assert sv.ic_standard_error(2) is None                    # too small to mean anything
    assert sv.ic_standard_error(0) is None
    assert round(sv.ic_standard_error(101), 2) == 0.10        # 1/sqrt(100)
    assert sv.ic_standard_error(50) > sv.ic_standard_error(200)


def test_factor_ic_flags_a_small_overlapping_sample_as_not_significant():
    fic = sv.factor_information_coefficients(_series("AAA", 22, step_days=30), horizon_days=91)
    m = fic["momentum"]
    assert m["n"] == 22 and m["n_eff"] == 6          # raw count is ~3.5x the real evidence
    assert m["ci95"] is not None
    assert abs(m["ic"]) < m["ci95"] and m["significant"] is False


def test_a_real_signal_on_enough_independent_names_is_flagged_significant():
    # 60 distinct tickers, one date each: no window overlap, no cross-correlation.
    pts = [_fpt(f"T{i}", float(i), {"momentum": float(i)}) for i in range(60)]
    m = sv.factor_information_coefficients(pts, horizon_days=91)["momentum"]
    assert m["ic"] == 1.0 and m["n_eff"] == 60
    assert abs(m["ic"]) > m["ci95"] and m["significant"] is True


def test_summarize_pooled_carries_error_bars_on_the_headline_ic():
    s = sv.summarize_pooled(_series("AAA", 22, step_days=30), horizon_days=91)
    assert s["n"] == 22 and s["n_eff"] == 6 and s["horizon_days"] == 91
    assert s["ci95"] is not None and s["significant"] is False
    assert s["avg_ticker_correlation"] == 0.0       # single ticker -> no pairs


# --------------------------------------------------------------------------
# Cross-sectional IC — ranks names against each other ON THE SAME DATE, which is
# the only thing the Screener actually claims to do.
# --------------------------------------------------------------------------

def _xs_point(ticker, day, score, fwd, factors=None):
    return {"date": day, "ticker": ticker, "score": score, "recommendation": "Hold",
            "forward_return_pct": fwd, "factors": factors or {"momentum": score}}


def _perfect_date(day, n=25, flip=False):
    """One date where score ranks the names exactly right (or exactly wrong)."""
    return [_xs_point(f"T{i}", day, float(i), float(-i if flip else i)) for i in range(n)]


def test_per_date_ics_rank_names_against_each_other_on_each_date():
    pts = _perfect_date(date(2022, 1, 1)) + _perfect_date(date(2022, 4, 1))
    ics = sv.per_date_ics(pts)
    assert ics == [1.0, 1.0]        # score ranks names perfectly on both dates


def test_per_date_ics_skip_dates_with_too_few_names_to_rank():
    # Ranking 3 stocks is noise, not a cross-section — that date must not count.
    thin = [_xs_point(f"T{i}", date(2022, 1, 1), float(i), float(i)) for i in range(3)]
    assert sv.per_date_ics(thin) == []
    assert len(sv.per_date_ics(thin, min_names=3)) == 1     # unless we say it's enough


def test_cross_sectional_ic_averages_per_date_and_reports_consistency():
    # Right on 3 dates, wrong on 1 -> mean IC 0.5, hit rate 0.75.
    pts = (_perfect_date(date(2022, 1, 1)) + _perfect_date(date(2022, 4, 1))
           + _perfect_date(date(2022, 7, 1)) + _perfect_date(date(2022, 10, 1), flip=True))
    xs = sv.cross_sectional_ic(pts, horizon_days=91, step_days=91)
    assert xs["n_dates"] == 4
    assert xs["mean_ic"] == 0.5
    assert xs["hit_rate"] == 0.75
    assert xs["ic_ir"] is not None


def test_cross_sectional_t_stat_deflates_for_overlapping_windows():
    # Same per-date ICs, but sampled 3x more often than the horizon: the extra
    # dates re-measure the same price move, so the t-stat must NOT triple.
    days = [date(2022, 1, 1) + timedelta(days=30 * i) for i in range(12)]
    pts = [p for i, d in enumerate(days) for p in _perfect_date(d, flip=(i % 4 == 0))]

    overlapped = sv.cross_sectional_ic(pts, horizon_days=91, step_days=30)
    independent = sv.cross_sectional_ic(pts, horizon_days=30, step_days=30)

    assert overlapped["n_dates"] == independent["n_dates"] == 12
    assert overlapped["n_dates_eff"] == 4          # 12 dates * (30/91) -> ~4 real trials
    assert independent["n_dates_eff"] == 12
    assert abs(overlapped["t_stat"]) < abs(independent["t_stat"])


def test_cross_sectional_ic_needs_two_dates():
    assert sv.cross_sectional_ic(_perfect_date(date(2022, 1, 1)))["mean_ic"] is None


def test_a_perfectly_consistent_ic_is_significant_not_undefined():
    # Zero spread across dates makes the t-stat undefined (nothing to divide by).
    # That must NOT be reported as "no signal" — and must never put inf in the
    # payload, which is JSON-cached.
    pts = _perfect_date(date(2022, 1, 1)) + _perfect_date(date(2022, 4, 1))
    xs = sv.cross_sectional_ic(pts, horizon_days=91, step_days=91)
    assert xs["mean_ic"] == 1.0
    assert xs["t_stat"] is None and xs["ic_ir"] is None    # undefined, not infinite
    assert xs["significant"] is True
    assert json.dumps(xs)                                   # stays serialisable


def test_a_consistently_zero_ic_is_not_significant():
    # Zero spread around a zero mean is the genuinely-no-signal case.
    flat = [_xs_point(f"T{i}", d, 50.0, float(i))
            for d in (date(2022, 1, 1), date(2022, 4, 1)) for i in range(25)]
    xs = sv.cross_sectional_ic(flat, horizon_days=91, step_days=91)
    assert xs["significant"] is False


# --------------------------------------------------------------------------
# Batch-job survivability. A 503-ticker run timed out at 3h having done 175 and
# lost all of it; the analyst fetch (Yahoo, blocked from datacenter IPs) hung
# rather than failing fast and was most of the cost.
# --------------------------------------------------------------------------

def test_walk_forward_can_skip_the_analyst_fetch_entirely():
    # include_analyst=False must not even CALL the (hanging) Yahoo path.
    scored = {"overall_score": 60.0, "recommendation": "Buy", "factor_scores": {"momentum": 70.0}}
    with patch("engine.screener_history.analyst_history.recommendation_as_of") as rec, \
         patch("engine.screener_history.historical_screener_score", return_value=scored), \
         patch("engine.screener_validation.forward_return_pct", return_value=3.0), \
         patch("engine.screener_validation.price_history.ensure_cached"):
        sv.walk_forward("AAPL", date(2022, 1, 1), date(2022, 3, 1),
                        step_days=30, horizon_days=30, include_news=False, include_analyst=False)
    assert not rec.called


def test_pooled_walk_forward_caches_each_ticker_so_a_killed_run_resumes():
    calls = {"n": 0}

    def fake_wf(ticker, *a, **k):
        calls["n"] += 1
        return [{"score": 60.0, "forward_return_pct": 2.0, "factors": {"momentum": 70.0},
                 "date": date(2022, 1, 1), "recommendation": "Buy"}]

    with patch("engine.screener_validation.walk_forward", side_effect=fake_wf):
        first = sv.pooled_walk_forward(["AAPL", "MSFT"], date(2022, 1, 1), date(2022, 6, 1),
                                       step_days=30, horizon_days=30, use_cache=True)
        # Simulates the next run after a timeout: same window, already-done tickers.
        second = sv.pooled_walk_forward(["AAPL", "MSFT"], date(2022, 1, 1), date(2022, 6, 1),
                                        step_days=30, horizon_days=30, use_cache=True)

    assert calls["n"] == 2                      # 2 tickers reconstructed ONCE, not 4 times
    assert len(first) == len(second) == 2
    assert {p["ticker"] for p in second} == {"AAPL", "MSFT"}
    # Dates must survive the JSON round-trip as real dates — the effective-sample
    # maths does arithmetic on them.
    assert all(isinstance(p["date"], date) for p in second)


def test_pinned_window_is_identical_every_day_of_the_same_week():
    # THE property that makes a killed run resumable: re-running on a later day
    # must produce the same window, or the per-ticker cache keys all miss and the
    # job redoes everything it already did.
    monday, friday = date(2026, 7, 13), date(2026, 7, 17)
    assert monday.weekday() == 0 and friday.weekday() == 4
    w = sv.pinned_window(monday, lookback_days=1825, horizon_days=91)
    for day_offset in range(7):                       # Mon..Sun
        assert sv.pinned_window(monday + timedelta(days=day_offset),
                                lookback_days=1825, horizon_days=91) == w
    assert sv.pinned_window(friday, lookback_days=1825, horizon_days=91) == w


def test_pinned_window_moves_on_to_the_next_week():
    a = sv.pinned_window(date(2026, 7, 17), lookback_days=1825, horizon_days=91)
    b = sv.pinned_window(date(2026, 7, 20), lookback_days=1825, horizon_days=91)  # next Monday
    assert b[1] == a[1] + timedelta(days=7)           # a fresh window, a week on


def test_pinned_window_end_is_already_the_last_scorable_date():
    # end must sit a full horizon in the past, so walk_forward's internal
    # `min(end, today - horizon)` clamp is a no-op and can't drift day to day.
    start, end = sv.pinned_window(date(2026, 7, 17), lookback_days=730, horizon_days=91)
    monday = date(2026, 7, 13)
    assert end == monday - timedelta(days=91)
    assert (end - start).days == 730


def test_pooled_walk_forward_cache_key_separates_different_windows_and_flags():
    base = dict(step_days=30, horizon_days=91, include_news=False, include_analyst=True)
    k = sv.ticker_points_cache_key("AAPL", date(2022, 1, 1), date(2024, 1, 1), **base)
    assert k != sv.ticker_points_cache_key("AAPL", date(2021, 1, 1), date(2024, 1, 1), **base)
    assert k != sv.ticker_points_cache_key("MSFT", date(2022, 1, 1), date(2024, 1, 1), **base)
    assert k != sv.ticker_points_cache_key("AAPL", date(2022, 1, 1), date(2024, 1, 1),
                                           **{**base, "include_analyst": False})


def test_pooled_walk_forward_does_not_cache_by_default():
    # The page's interactive run should reflect fresh data, not a week-old snapshot.
    calls = {"n": 0}

    def fake_wf(ticker, *a, **k):
        calls["n"] += 1
        return []

    with patch("engine.screener_validation.walk_forward", side_effect=fake_wf):
        sv.pooled_walk_forward(["AAPL"], date(2022, 1, 1), date(2022, 6, 1))
        sv.pooled_walk_forward(["AAPL"], date(2022, 1, 1), date(2022, 6, 1))
    assert calls["n"] == 2


def test_bonferroni_threshold_rises_with_the_number_of_factors_tested():
    # One pre-specified test keeps the familiar 1.96...
    assert round(sv.bonferroni_t_threshold(1), 2) == 1.96
    # ...but testing the whole 6-factor table at once costs ~2.64.
    assert round(sv.bonferroni_t_threshold(6), 2) == 2.64
    assert sv.bonferroni_t_threshold(6) > sv.bonferroni_t_threshold(2)


def test_a_marginal_factor_is_not_significant_once_corrected_for_the_whole_table():
    # The real regression: a 5-year run flagged Profitability at t=-1.98, which
    # clears the naive 1.96 but is exactly the marginal hit you EXPECT when six
    # factors are tested at once (~26% chance of at least one false positive).
    assert abs(-1.98) > sv.bonferroni_t_threshold(1)     # would pass alone...
    assert abs(-1.98) < sv.bonferroni_t_threshold(6)     # ...but not against the table


def test_summarize_universe_corrects_factor_significance_for_multiple_comparisons():
    days = [date(2022, 1, 1) + timedelta(days=91 * i) for i in range(12)]
    pts = []
    for i, day in enumerate(days):
        flip = i in (0, 1, 2)
        for k in range(25):
            pts.append({"date": day, "ticker": f"T{k}", "score": float(k), "recommendation": "Hold",
                        "forward_return_pct": float(-k if flip else k),
                        "factors": {"momentum": float(k), "valuation": float(k),
                                    "growth": float(25 - k)}})
    s = sv.summarize_universe(pts, horizon_days=91, step_days=91)

    assert s["n_tests"] == 3                                     # only factors with a t-stat count
    assert s["t_threshold"] == round(sv.bonferroni_t_threshold(3), 2)
    assert s["t_threshold"] > 1.96                               # testing 3 at once costs more
    for f in s["factor_ic"].values():
        assert "significant_uncorrected" in f                    # kept, not silently overwritten
        if f["significant"]:
            assert f["significant_uncorrected"]                  # corrected is strictly stricter


def test_a_single_tested_factor_needs_no_correction():
    # Only momentum has scores -> one test -> the alpha budget isn't being split.
    days = [date(2022, 1, 1) + timedelta(days=91 * i) for i in range(12)]
    pts = [p for i, d in enumerate(days) for p in _perfect_date(d, flip=(i in (0, 1, 2)))]
    s = sv.summarize_universe(pts, horizon_days=91, step_days=91)
    assert s["n_tests"] == 1 and s["t_threshold"] == 1.96


def test_summarize_universe_reports_overall_and_per_factor_cross_sectional_ics():
    pts = (_perfect_date(date(2022, 1, 1)) + _perfect_date(date(2022, 4, 1))
           + _perfect_date(date(2022, 7, 1)))
    s = sv.summarize_universe(pts, horizon_days=91, step_days=91)
    assert s["overall"]["mean_ic"] == 1.0
    assert s["overall"]["significant"] is True          # a real, consistent signal
    assert s["factor_ic"]["momentum"]["mean_ic"] == 1.0
    assert s["factor_ic"]["momentum"]["label"] == "Momentum / Technical"
    assert s["factor_ic"]["valuation"]["mean_ic"] is None   # no valuation scores supplied
    assert s["n_tickers"] == 25 and s["horizon_days"] == 91


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
    def fake_score(ticker, as_of, include_news=True, include_analyst=True):
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

    def fake_score(ticker, as_of, include_news=True, include_analyst=True):
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
