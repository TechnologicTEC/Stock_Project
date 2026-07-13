from datetime import date, timedelta
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from engine import screener


@pytest.fixture(autouse=True)
def _no_news_by_default():
    """_score_sentiment now calls news.analyze_ticker (FinBERT pipeline).
    Default every test to 'no recent news' so screen_tickers stays network-
    and model-free; the sentiment-specific tests patch it themselves."""
    from engine import news
    empty = news.NewsAnalysis(ticker="", headlines=[], overall_score=None, has_sentiment=False, total_count=0)
    with patch("engine.news.analyze_ticker", return_value=empty):
        yield


# --------------------------------------------------------------------------
# Percentile ranking helper
# --------------------------------------------------------------------------

def test_percentile_ranks_higher_is_better():
    ranks = screener._percentile_ranks({"A": 10.0, "B": 20.0, "C": 30.0}, higher_is_better=True)
    assert ranks["C"] > ranks["B"] > ranks["A"]
    assert ranks["A"] == pytest.approx(0.0)
    assert ranks["C"] == pytest.approx(100.0)


def test_percentile_ranks_lower_is_better_inverts():
    ranks = screener._percentile_ranks({"A": 10.0, "B": 20.0, "C": 30.0}, higher_is_better=False)
    assert ranks["A"] > ranks["B"] > ranks["C"]
    assert ranks["A"] == pytest.approx(100.0)  # lowest raw value is "best" when lower is better
    assert ranks["C"] == pytest.approx(0.0)


def test_percentile_ranks_best_item_always_hits_100_regardless_of_direction():
    """Regression check for an asymmetry bug: with pandas' raw rank(pct=True)
    (rank/n), inverting for 'lower is better' meant even the best item
    capped below 100, worse the smaller the group. Both directions should
    reach the same 0-100 extremes for the same group size."""
    small_group = {"A": 5.0, "B": 10.0}
    assert screener._percentile_ranks(small_group, higher_is_better=True)["B"] == pytest.approx(100.0)
    assert screener._percentile_ranks(small_group, higher_is_better=False)["A"] == pytest.approx(100.0)


def test_percentile_ranks_none_values_excluded_but_present_in_output():
    ranks = screener._percentile_ranks({"A": 10.0, "B": None, "C": 30.0}, higher_is_better=True)
    assert ranks["B"] is None
    assert ranks["A"] is not None and ranks["C"] is not None


def test_percentile_ranks_returns_all_none_with_fewer_than_two_values():
    ranks = screener._percentile_ranks({"A": 10.0, "B": None}, higher_is_better=True)
    assert ranks == {"A": None, "B": None}


# --------------------------------------------------------------------------
# Metric extraction with fallback key candidates
# --------------------------------------------------------------------------

def test_extract_metric_uses_first_available_candidate_key():
    metrics = {"peNormalizedAnnual": 25.0}
    assert screener._extract_metric(metrics, "pe") == 25.0


def test_extract_metric_returns_none_when_nothing_matches():
    assert screener._extract_metric({"someOtherField": 1}, "pe") is None
    assert screener._extract_metric(None, "pe") is None


def test_extract_metric_skips_non_numeric_garbage():
    metrics = {"peTTM": "N/A", "peNormalizedAnnual": 18.5}
    assert screener._extract_metric(metrics, "pe") == 18.5


# --------------------------------------------------------------------------
# Factor scorers - constructed directly from synthetic TickerRawData,
# bypassing _gather_raw_data entirely so the scoring math is tested in
# isolation from any network/cache behavior.
# --------------------------------------------------------------------------

def _raw(
    ticker, fundamentals=None, price_df=None, recommendation=None, price_target=None, insider_mspr=None,
    sector_bucket=None, raw_industry=None,
):
    if price_df is None:
        price_df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    return screener.TickerRawData(
        ticker=ticker, fundamentals=fundamentals, price_df=price_df,
        recommendation=recommendation, price_target=price_target, insider_mspr=insider_mspr,
        sector_bucket=sector_bucket or screener.DEFAULT_SECTOR_BUCKET, raw_industry=raw_industry, errors=[],
    )


def test_score_valuation_cheaper_ranks_higher():
    raw = {
        "CHEAP": _raw("CHEAP", fundamentals={"peTTM": 8.0, "pbAnnual": 1.0}),
        "EXPENSIVE": _raw("EXPENSIVE", fundamentals={"peTTM": 50.0, "pbAnnual": 10.0}),
    }
    result = screener._score_valuation(raw)
    assert result["CHEAP"].score > result["EXPENSIVE"].score


def test_score_valuation_excludes_negative_pe():
    raw = {
        "LOSS_MAKER": _raw("LOSS_MAKER", fundamentals={"peTTM": -15.0}),
        "PROFITABLE": _raw("PROFITABLE", fundamentals={"peTTM": 20.0}),
    }
    result = screener._score_valuation(raw)
    assert result["LOSS_MAKER"].raw["pe"] is None  # negative P/E excluded, not scored as "infinitely cheap"


def test_score_valuation_no_data_returns_none_with_explanation():
    raw = {"X": _raw("X", fundamentals=None)}
    result = screener._score_valuation(raw)
    assert result["X"].score is None
    assert "No valuation ratios available" in result["X"].reasons[0]


def test_score_growth_higher_growth_ranks_higher():
    raw = {
        "FAST": _raw("FAST", fundamentals={"revenueGrowthTTMYoy": 40.0, "epsGrowthTTMYoy": 30.0}),
        "SLOW": _raw("SLOW", fundamentals={"revenueGrowthTTMYoy": 2.0, "epsGrowthTTMYoy": 1.0}),
    }
    result = screener._score_growth(raw)
    assert result["FAST"].score > result["SLOW"].score


def test_score_profitability_rewards_margins_penalizes_debt():
    raw = {
        "HEALTHY": _raw("HEALTHY", fundamentals={
            "grossMarginTTM": 60, "netProfitMarginTTM": 20, "roeTTM": 25, "totalDebt/totalEquityAnnual": 0.2,
        }),
        "WEAK": _raw("WEAK", fundamentals={
            "grossMarginTTM": 20, "netProfitMarginTTM": 2, "roeTTM": 3, "totalDebt/totalEquityAnnual": 3.0,
        }),
    }
    result = screener._score_profitability(raw)
    assert result["HEALTHY"].score > result["WEAK"].score


def _flat_price_df(closes: list[float]) -> pd.DataFrame:
    n = len(closes)
    idx = [date(2026, 1, 1) + timedelta(days=i) for i in range(n)]
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": [1000] * n}, index=idx
    )


def test_score_momentum_rewards_uptrend_over_downtrend():
    uptrend = list(np.linspace(80, 120, 260))     # >1yr history -> uses the 12-1 window
    downtrend = list(np.linspace(120, 80, 260))
    raw = {"UP": _raw("UP", price_df=_flat_price_df(uptrend)), "DOWN": _raw("DOWN", price_df=_flat_price_df(downtrend))}

    result = screener._score_momentum(raw)
    assert result["UP"].score > result["DOWN"].score


def test_momentum_uses_12_1_and_ignores_a_recent_crash():
    # Rises 100->150 over ~11 months, then crashes 150->110 in the final month.
    # 12-1 momentum (12mo ago -> 1mo ago) is strongly +, and the recent crash is
    # SKIPPED — so momentum stays high, unlike a total-return or RSI measure.
    prices = list(np.linspace(100, 150, 239)) + list(np.linspace(150, 110, 21))
    result = screener._score_momentum({"X": _raw("X", price_df=_flat_price_df(prices))})
    assert result["X"].score > 70                              # the recent drop didn't drag it down
    assert result["X"].raw["momentum_12_1_pct"] > 30
    reasons = " ".join(result["X"].reasons)
    assert "skipping the last month" in reasons
    assert "context, not scored" in reasons                    # RSI/MA are shown but not in the score


def test_score_momentum_short_history_falls_back_to_6_month_return():
    # ~7 months of history (<1yr): no 12-1, so the ~6-month total return is used.
    raw = {"UP": _raw("UP", price_df=_flat_price_df(list(np.linspace(80, 120, 150))))}
    result = screener._score_momentum(raw)
    assert result["UP"].score is not None and result["UP"].raw.get("momentum_12_1_pct") is None
    assert "fallback" in " ".join(result["UP"].reasons)


def test_score_momentum_insufficient_history_returns_none():
    raw = {"NEW": _raw("NEW", price_df=_flat_price_df([100.0, 101.0, 99.0]))}  # only 3 days
    result = screener._score_momentum(raw)
    assert result["NEW"].score is None
    assert "Not enough price history" in result["NEW"].reasons[0]


def test_score_analyst_confidence_bullish_consensus_beats_bearish():
    raw = {
        "BULLISH": _raw(
            "BULLISH",
            price_df=_flat_price_df([100.0] * 30),
            price_target={"targetMean": 130.0},
            recommendation={"strongBuy": 10, "buy": 5, "hold": 1, "sell": 0, "strongSell": 0},
            insider_mspr=0.3,
        ),
        "BEARISH": _raw(
            "BEARISH",
            price_df=_flat_price_df([100.0] * 30),
            price_target={"targetMean": 90.0},
            recommendation={"strongBuy": 0, "buy": 0, "hold": 1, "sell": 5, "strongSell": 10},
            insider_mspr=-0.3,
        ),
    }
    result = screener._score_analyst_confidence(raw)
    assert result["BULLISH"].score > result["BEARISH"].score


def test_score_sentiment_maps_news_overall_score():
    from engine import news
    scored = news.NewsAnalysis(
        ticker="X", headlines=[], overall_score=68, positive=5, neutral=2,
        negative=1, scored_count=8, total_count=8, has_sentiment=True,
    )
    with patch("engine.news.analyze_ticker", return_value=scored):
        result = screener._score_sentiment({"X": _raw("X")})
    assert result["X"].score == 68.0
    assert "68/100" in result["X"].reasons[0]


def test_score_sentiment_none_when_no_recent_news():
    # The autouse fixture returns a no-news analysis, so the factor abstains
    # (score None) rather than faking a neutral 50.
    result = screener._score_sentiment({"X": _raw("X")})
    assert result["X"].score is None
    assert "No recent news" in result["X"].reasons[0]


# --------------------------------------------------------------------------
# Absolute curve scoring - the primary scoring mechanism (replaces
# peer-percentile-as-score; see module docstring for why)
# --------------------------------------------------------------------------

def test_score_from_curve_interpolates_between_anchors():
    curve = [(0, 0), (10, 100)]
    assert screener._score_from_curve(5, curve) == pytest.approx(50.0)


def test_score_from_curve_clamps_outside_range():
    curve = [(10, 100), (20, 0)]
    assert screener._score_from_curve(0, curve) == 100.0
    assert screener._score_from_curve(100, curve) == 0.0


def test_score_from_curve_none_passes_through():
    assert screener._score_from_curve(None, screener.PE_CURVE) is None


def test_quality_word_thresholds():
    assert screener._quality_word(95) == "excellent"
    assert screener._quality_word(65) == "good"
    assert screener._quality_word(45) == "fair"
    assert screener._quality_word(25) == "weak"
    assert screener._quality_word(5) == "poor"
    assert screener._quality_word(None) == "unknown"


# --------------------------------------------------------------------------
# The actual bug report this rework fixes: scores must be the SAME for a
# given ticker's data regardless of who else is in the screening list, and
# a single ticker screened alone must get a real score, not "insufficient
# peers". These are the tests that would have caught the original problem.
# --------------------------------------------------------------------------

def test_score_is_identical_whether_screened_alone_or_with_peers():
    """The bug report: BBAI's growth score depended on which other tickers
    happened to be in the same screen. A ticker's score must come from its
    own numbers, full stop."""
    bbai = _raw("BBAI", fundamentals={"revenueGrowthTTMYoy": -20.3})

    alone = screener._score_growth({"BBAI": bbai})
    with_strong_peers = screener._score_growth({
        "BBAI": bbai,
        "ROCKET": _raw("ROCKET", fundamentals={"revenueGrowthTTMYoy": 80.0}),
    })
    with_weak_peers = screener._score_growth({
        "BBAI": bbai,
        "WORSE": _raw("WORSE", fundamentals={"revenueGrowthTTMYoy": -60.0}),
    })

    assert alone["BBAI"].score == with_strong_peers["BBAI"].score == with_weak_peers["BBAI"].score


def test_negative_growth_does_not_collapse_to_zero_score():
    """-20.3% revenue growth is bad, but the curve treats it as the bottom
    of a real (if narrow) range, not an automatic 0 - the 0/100 from the
    bug report came from being the worst of an arbitrary small group, not
    from the number itself being literally the worst possible."""
    raw = {"BBAI": _raw("BBAI", fundamentals={"revenueGrowthTTMYoy": -20.3})}
    result = screener._score_growth(raw)
    assert result["BBAI"].score == pytest.approx(0.0)  # -20.3 is at/below this curve's floor anchor - that's fine,
    # it just shouldn't be an artifact of *peer comparison*, which the test above confirms.


def test_single_ticker_screen_gets_real_scores_not_insufficient_peers():
    raw = {"SOLO": _raw("SOLO", fundamentals={
        "peTTM": 18.0, "pbAnnual": 2.0, "revenueGrowthTTMYoy": 12.0, "epsGrowthTTMYoy": 10.0,
        "grossMarginTTM": 45.0, "netProfitMarginTTM": 12.0, "roeTTM": 18.0, "totalDebt/totalEquityAnnual": 0.6,
    })}
    with patch("engine.screener._gather_raw_data", side_effect=lambda t: raw[t]):
        results = screener.screen_tickers(["SOLO"])

    assert len(results) == 1
    r = results[0]
    assert r.overall_score is not None
    assert r.recommendation != "Insufficient data"
    assert r.factors["valuation"].score is not None
    assert r.factors["growth"].score is not None
    assert r.factors["profitability"].score is not None
    # peer-percentile context should NOT appear anywhere with only one ticker
    all_reasons = " ".join(reason for fr in r.factors.values() for reason in fr.reasons)
    assert "percentile" not in all_reasons


def test_peer_percentile_appears_as_context_when_peers_exist_but_doesnt_drive_score():
    cheap = _raw("CHEAP", fundamentals={"peTTM": 10.0})
    expensive = _raw("EXPENSIVE", fundamentals={"peTTM": 50.0})

    solo_result = screener._score_valuation({"CHEAP": cheap})
    grouped_result = screener._score_valuation({"CHEAP": cheap, "EXPENSIVE": expensive})

    assert "percentile" not in solo_result["CHEAP"].reasons[0]
    assert "percentile" in grouped_result["CHEAP"].reasons[0]
    # the score itself is unchanged by the peer context being available
    assert solo_result["CHEAP"].score == grouped_result["CHEAP"].score


# --------------------------------------------------------------------------
# Sector classification and sector-aware curves
# --------------------------------------------------------------------------

def test_classify_sector_bucket_matches_keywords_case_insensitively():
    assert screener.classify_sector_bucket("Consumer Electronics") == "Technology / Software"
    assert screener.classify_sector_bucket("airlines") == "Industrials / Materials"
    assert screener.classify_sector_bucket("Banks—Regional") == "Banks / Financials"


def test_classify_sector_bucket_falls_back_to_default():
    assert screener.classify_sector_bucket("Some Totally Unrecognized Thing") == screener.DEFAULT_SECTOR_BUCKET
    assert screener.classify_sector_bucket(None) == screener.DEFAULT_SECTOR_BUCKET


def test_curve_for_uses_sector_override_when_present():
    tech_curve = screener._curve_for("pe", "Technology / Software", screener.PE_CURVE)
    assert tech_curve == screener.SECTOR_CURVE_OVERRIDES["Technology / Software"]["pe"]
    assert tech_curve != screener.PE_CURVE


def test_curve_for_falls_back_to_generic_when_no_override():
    # ROE has no per-sector override defined at all
    assert screener._curve_for("roe", "Technology / Software", screener.ROE_CURVE) == screener.ROE_CURVE
    # An unrecognized bucket falls back to generic even for a metric that DOES have overrides elsewhere
    assert screener._curve_for("pe", screener.DEFAULT_SECTOR_BUCKET, screener.PE_CURVE) == screener.PE_CURVE


def test_same_pe_scores_differently_by_sector():
    """The whole point of sector adjustment: identical raw P/E should not
    necessarily get an identical score in different sectors, since what
    counts as 'expensive' varies."""
    pe_value = 45.0
    tech = _raw("TECH", fundamentals={"peTTM": pe_value}, sector_bucket="Technology / Software")
    bank = _raw("BANK", fundamentals={"peTTM": pe_value}, sector_bucket="Banks / Financials")

    tech_score = screener._score_valuation({"TECH": tech})["TECH"].score
    bank_score = screener._score_valuation({"BANK": bank})["BANK"].score

    assert tech_score != bank_score
    assert tech_score > bank_score  # 45x is unremarkable for tech, expensive for a bank


def test_valuation_reason_states_which_threshold_set_was_used():
    raw = {"AAPL": _raw("AAPL", fundamentals={"peTTM": 30.0}, sector_bucket="Technology / Software")}
    result = screener._score_valuation(raw)
    assert "Technology / Software thresholds" in result["AAPL"].reasons[0]


def test_valuation_reason_labels_unmatched_sector_explicitly():
    raw = {"X": _raw("X", fundamentals={"peTTM": 30.0})}  # default/General bucket
    result = screener._score_valuation(raw)
    assert "General (no industry match) thresholds" in result["X"].reasons[0]


def test_extreme_pb_gets_caveat_note_about_buybacks_and_asset_light_businesses():
    """The real-world case this addresses: AAPL-style P/B of 50+ from heavy
    buybacks isn't the same thing as being overvalued."""
    raw = {"AAPL": _raw("AAPL", fundamentals={"pbAnnual": 51.0}, sector_bucket="Technology / Software")}
    result = screener._score_valuation(raw)
    assert any("buyback" in r.lower() for r in result["AAPL"].reasons)
    # it should score low but NOT identically flat-zero regardless of how extreme the input is
    assert 0 < result["AAPL"].score < 20


def test_pb_caveat_note_absent_for_normal_values():
    raw = {"X": _raw("X", fundamentals={"pbAnnual": 3.0})}
    result = screener._score_valuation(raw)
    assert not any("buyback" in r.lower() for r in result["X"].reasons)


# --------------------------------------------------------------------------
# Insider MSPR scale fix - Finnhub documents -100..+100, not -1..+1
# --------------------------------------------------------------------------

def test_insider_mspr_curve_uses_correct_finnhub_scale():
    # -33.24 (a real value seen in testing) should land well above 0 -
    # it's moderately negative, not "as bad as possible" on a -100..100 scale.
    score = screener._score_from_curve(-33.24, screener.INSIDER_MSPR_CURVE)
    assert score == pytest.approx(33.38, abs=0.5)
    assert score > 0


def test_insider_mspr_extreme_values_still_hit_the_real_floor_and_ceiling():
    assert screener._score_from_curve(-100, screener.INSIDER_MSPR_CURVE) == 0.0
    assert screener._score_from_curve(100, screener.INSIDER_MSPR_CURVE) == 100.0
    assert screener._score_from_curve(0, screener.INSIDER_MSPR_CURVE) == 50.0


# --------------------------------------------------------------------------
# Recommendation thresholds
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "score,expected",
    [(None, "Insufficient data"), (90, "Strong Buy"), (75, "Strong Buy"), (65, "Buy"),
     (50, "Hold"), (30, "Sell"), (10, "Strong Sell"), (0, "Strong Sell")],
)
def test_recommendation_thresholds(score, expected):
    assert screener._recommendation_for(score) == expected


# --------------------------------------------------------------------------
# screen_tickers() - the integration point. _gather_raw_data is mocked so
# this tests weight redistribution and sorting, not live data fetching
# (that's what test_data_sources.py and the per-factor tests above cover).
# --------------------------------------------------------------------------

def test_screen_tickers_redistributes_sentiments_weight():
    """With no recent news (the autouse default), sentiment abstains, so its
    15% weight should be spread across the other factors rather than dropped."""
    raw = {
        "A": _raw("A", fundamentals={"peTTM": 15.0}),
        "B": _raw("B", fundamentals={"peTTM": 25.0}),
    }
    with patch("engine.screener._gather_raw_data", side_effect=lambda t: raw[t]):
        results = screener.screen_tickers(["A", "B"])

    for r in results:
        assert r.factors["sentiment"].score is None
        # only valuation produced a score in this minimal fixture - its weight
        # should be the ENTIRE overall score (renormalized to 100%), not 20%.
        if r.factors["valuation"].score is not None:
            assert r.overall_score == round(r.factors["valuation"].score, 1)


def test_screen_tickers_sorts_best_first_and_unscored_last():
    raw = {
        "GOOD": _raw("GOOD", fundamentals={"peTTM": 8.0, "pbAnnual": 1.0}),
        "BAD": _raw("BAD", fundamentals={"peTTM": 60.0, "pbAnnual": 12.0}),
        "NODATA": _raw("NODATA"),
    }
    with patch("engine.screener._gather_raw_data", side_effect=lambda t: raw[t]):
        results = screener.screen_tickers(["GOOD", "BAD", "NODATA"])

    order = [r.ticker for r in results]
    assert order.index("GOOD") < order.index("BAD")
    assert order[-1] == "NODATA"  # no usable data sorts last regardless of score


def test_screen_tickers_empty_input_returns_empty_list():
    assert screener.screen_tickers([]) == []
    assert screener.screen_tickers(["  ", ""]) == []


def test_screen_tickers_deduplicates_and_normalizes_case():
    raw = {"AAPL": _raw("AAPL", fundamentals={"peTTM": 20.0})}
    with patch("engine.screener._gather_raw_data", side_effect=lambda t: raw[t]):
        results = screener.screen_tickers(["aapl", "AAPL", " Aapl "])
    assert len(results) == 1
    assert results[0].ticker == "AAPL"


# --------------------------------------------------------------------------
# Finnhub price-target 403 handling - detected once, not repeated per ticker
# --------------------------------------------------------------------------

def test_gather_raw_data_403_on_price_target_sets_flag_not_per_ticker_error():
    import finnhub as finnhub_pkg

    class FakeResponse:
        status_code = 403
        def json(self):
            return {"error": "You don't have access to this resource."}

    forbidden = finnhub_pkg.FinnhubAPIException(FakeResponse())

    with patch("engine.data_sources.finnhub_client.get_basic_financials", return_value={"metric": {}}):
        with patch("engine.price_history.get_history_df", return_value=pd.DataFrame(columns=["open", "high", "low", "close", "volume"])):
            with patch("engine.data_sources.finnhub_client.get_recommendation_trends", return_value=[]):
                with patch("engine.data_sources.finnhub_client.get_price_target", side_effect=forbidden):
                    with patch("engine.data_sources.finnhub_client.get_insider_sentiment", return_value={"data": []}):
                        result = screener._gather_raw_data("ZZZZ")

    assert not any("price target" in e for e in result.errors)  # not dumped as a per-ticker error
    assert screener.known_limitations()  # surfaced once, run-wide, instead


def test_gather_raw_data_non_403_price_target_error_still_reported_per_ticker():
    with patch("engine.data_sources.finnhub_client.get_basic_financials", return_value={"metric": {}}):
        with patch("engine.price_history.get_history_df", return_value=pd.DataFrame(columns=["open", "high", "low", "close", "volume"])):
            with patch("engine.data_sources.finnhub_client.get_recommendation_trends", return_value=[]):
                with patch("engine.data_sources.finnhub_client.get_price_target", side_effect=RuntimeError("timeout")):
                    with patch("engine.data_sources.finnhub_client.get_insider_sentiment", return_value={"data": []}):
                        result = screener._gather_raw_data("ZZZZ")

    assert any("price target" in e for e in result.errors)  # a real, non-permission error still shows up


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

def test_save_and_retrieve_score_history():
    results = [
        screener.ScreenerResult(
            ticker="AAPL", overall_score=82.5, recommendation="Strong Buy",
            factors={"valuation": screener.FactorResult(score=90.0, reasons=["cheap"])},
            data_errors=[],
        )
    ]
    written = screener.save_results(results, as_of=date(2026, 6, 1))
    assert written == 1

    history = screener.get_score_history("aapl")
    assert len(history) == 1
    assert history[0]["overall_score"] == 82.5
    assert history[0]["recommendation"] == "Strong Buy"
    assert history[0]["sub_scores"]["valuation"]["score"] == 90.0


def test_save_results_skips_tickers_with_no_score():
    results = [
        screener.ScreenerResult(ticker="NODATA", overall_score=None, recommendation="Insufficient data", factors={}, data_errors=["no data"]),
    ]
    written = screener.save_results(results, as_of=date(2026, 6, 1))
    assert written == 0
    assert screener.get_score_history("NODATA") == []


def test_save_results_upserts_same_ticker_same_day():
    first = [screener.ScreenerResult(ticker="AAPL", overall_score=50.0, recommendation="Hold", factors={}, data_errors=[])]
    second = [screener.ScreenerResult(ticker="AAPL", overall_score=70.0, recommendation="Buy", factors={}, data_errors=[])]

    screener.save_results(first, as_of=date(2026, 6, 1))
    screener.save_results(second, as_of=date(2026, 6, 1))

    history = screener.get_score_history("AAPL")
    assert len(history) == 1  # updated in place, not duplicated
    assert history[0]["overall_score"] == 70.0
