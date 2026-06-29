from datetime import date, timedelta
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from engine import screener


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
# Absolute momentum sub-scores
# --------------------------------------------------------------------------

def test_rsi_sweet_spot_scores_highest_near_60():
    assert screener._absolute_rsi_score(60) == 100.0
    assert screener._absolute_rsi_score(60) > screener._absolute_rsi_score(85)
    assert screener._absolute_rsi_score(60) > screener._absolute_rsi_score(20)
    assert screener._absolute_rsi_score(None) is None


def test_ma_position_score_rewards_price_above_sma():
    above = screener._absolute_ma_position_score(price=110, sma=100)  # +10%
    below = screener._absolute_ma_position_score(price=90, sma=100)   # -10%
    assert above == pytest.approx(100.0)
    assert below == pytest.approx(0.0)
    assert screener._absolute_ma_position_score(None, 100) is None
    assert screener._absolute_ma_position_score(100, 0) is None


# --------------------------------------------------------------------------
# Factor scorers - constructed directly from synthetic TickerRawData,
# bypassing _gather_raw_data entirely so the scoring math is tested in
# isolation from any network/cache behavior.
# --------------------------------------------------------------------------

def _raw(ticker, fundamentals=None, price_df=None, recommendation=None, price_target=None, insider_mspr=None):
    if price_df is None:
        price_df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    return screener.TickerRawData(
        ticker=ticker, fundamentals=fundamentals, price_df=price_df,
        recommendation=recommendation, price_target=price_target, insider_mspr=insider_mspr, errors=[],
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
    uptrend = list(np.linspace(80, 120, 220))
    downtrend = list(np.linspace(120, 80, 220))
    raw = {"UP": _raw("UP", price_df=_flat_price_df(uptrend)), "DOWN": _raw("DOWN", price_df=_flat_price_df(downtrend))}

    result = screener._score_momentum(raw)
    assert result["UP"].score > result["DOWN"].score


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


def test_score_sentiment_always_returns_none_for_now():
    raw = {"X": _raw("X")}
    result = screener._score_sentiment(raw)
    assert result["X"].score is None
    assert "Phase 4" in result["X"].reasons[0]


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
    """Sentiment always returns None right now, so its 15% weight should be
    spread across the other five factors rather than just dropped."""
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
