from datetime import date, timedelta
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from engine import screener, screener_history

# 8 quarters (oldest -> newest), each filed ~35 days after its period end.
_ENDS = ["2021-06-30", "2021-09-30", "2021-12-31", "2022-03-31",
         "2022-06-30", "2022-09-30", "2022-12-31", "2023-03-31"]


def _flow(values):
    """Build a flow-metric series (needs start/end for the quarterly filter is
    already applied upstream; here we just carry end + filed + value)."""
    out = []
    for end, val in zip(_ENDS, values):
        filed = (date.fromisoformat(end) + timedelta(days=35)).isoformat()
        out.append({"end": end, "filed": filed, "value": float(val)})
    return out


def _synthetic_series():
    return {
        "revenue": _flow([100, 110, 120, 130, 140, 150, 160, 170]),        # TTM now 620, prior 460
        "net_income": _flow([10, 11, 12, 13, 14, 15, 16, 17]),             # TTM 62
        "gross_profit": _flow([40, 44, 48, 52, 56, 60, 64, 68]),           # TTM 248 -> 40% gross margin
        "eps_diluted": _flow([0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85]),
        "equity": _flow([300, 320, 340, 360, 400, 440, 460, 500]),         # latest 500
        "long_term_debt": _flow([100] * 7 + [250]),                        # latest 250
        "shares": _flow([100] * 8),                                        # latest 100
    }


def _price_df(ticker, start, end, source="yfinance"):
    days = pd.bdate_range(start=start, end=end).date
    closes = np.linspace(55.0, 62.0, len(days))  # ends exactly at 62 on the as-of day
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": [1] * len(days)},
        index=pd.Index(days, name="date"),
    )


# --------------------------------------------------------------------------
# TTM + point-in-time helpers
# --------------------------------------------------------------------------

def test_ttm_sum_respects_filing_dates():
    rev = _synthetic_series()["revenue"]
    # On 2022-06-01 only quarters through 2022-03-31 are filed (Q ending 2022-06-30
    # isn't filed until ~2022-08-04), so TTM = last four *public* quarters.
    assert screener_history._ttm_sum(rev, date(2022, 6, 1)) == 100 + 110 + 120 + 130
    # A year-earlier TTM needs 8 public quarters; only 4 exist yet -> None.
    assert screener_history._ttm_sum(rev, date(2022, 6, 1), quarters_back=4) is None
    # By 2023-06-01 all eight are public.
    assert screener_history._ttm_sum(rev, date(2023, 6, 1)) == 140 + 150 + 160 + 170
    assert screener_history._ttm_sum(rev, date(2023, 6, 1), quarters_back=4) == 100 + 110 + 120 + 130


# --------------------------------------------------------------------------
# Reconstructed ratios
# --------------------------------------------------------------------------

def test_pit_fundamentals_metrics_reconstructs_ratios():
    with patch("engine.screener_history.edgar_fundamentals.get_pit_fundamentals", return_value=_synthetic_series()), \
         patch("engine.screener_history.price_history.get_history_df", side_effect=_price_df):
        m = screener_history.pit_fundamentals_metrics("TEST", date(2023, 6, 1))

    # market cap = price(62) * shares(100) = 6200
    assert m["peTTM"] == pytest.approx(6200 / 62)             # 100
    assert m["psTTM"] == pytest.approx(6200 / 620)            # 10
    assert m["pbAnnual"] == pytest.approx(6200 / 500)         # 12.4
    assert m["grossMarginTTM"] == pytest.approx(40.0)
    assert m["netProfitMarginTTM"] == pytest.approx(10.0)
    assert m["roeTTM"] == pytest.approx(62 / 500 * 100)       # 12.4
    assert m["totalDebt/totalEquityAnnual"] == pytest.approx(0.5)
    assert m["revenueGrowthTTMYoy"] == pytest.approx((620 / 460 - 1) * 100)
    assert m["epsGrowthTTMYoy"] == pytest.approx((3.1 / 2.3 - 1) * 100)


# --------------------------------------------------------------------------
# Full historical score (reusing the live scorers)
# --------------------------------------------------------------------------

def test_historical_score_uses_real_scorers_with_missing_factors_redistributed():
    with patch("engine.screener_history.edgar_fundamentals.get_pit_fundamentals", return_value=_synthetic_series()), \
         patch("engine.screener_history.price_history.get_history_df", side_effect=_price_df), \
         patch("engine.screener_history._profile_bits", return_value=(screener.DEFAULT_SECTOR_BUCKET, None, "Test Co")), \
         patch("engine.screener_history.analyst_history.recommendation_as_of", return_value=None), \
         patch("engine.screener_history.gdelt_client.sentiment_as_of", return_value=None):
        result = screener_history.historical_screener_score("TEST", date(2023, 6, 1))

    assert result is not None
    assert result["overall_score"] is not None
    # Fundamentals + momentum score. With no reconstructed consensus and no
    # news coverage, those two factors are None and their weight is redistributed.
    assert result["factor_scores"]["valuation"] is not None
    assert result["factor_scores"]["profitability"] is not None
    assert result["factor_scores"]["analyst_confidence"] is None
    assert result["factor_scores"]["sentiment"] is None
    assert result["recommendation"] in {"Strong Buy", "Buy", "Hold", "Sell", "Strong Sell"}


def test_historical_score_includes_reconstructed_analyst_and_gdelt_sentiment():
    buy_heavy = {"strongBuy": 6, "buy": 12, "hold": 3, "sell": 0, "strongSell": 0}
    with patch("engine.screener_history.edgar_fundamentals.get_pit_fundamentals", return_value=_synthetic_series()), \
         patch("engine.screener_history.price_history.get_history_df", side_effect=_price_df), \
         patch("engine.screener_history._profile_bits", return_value=(screener.DEFAULT_SECTOR_BUCKET, None, "Test Co")), \
         patch("engine.screener_history.analyst_history.recommendation_as_of", return_value=buy_heavy), \
         patch("engine.screener_history.gdelt_client.sentiment_as_of", return_value=72.0):
        result = screener_history.historical_screener_score("TEST", date(2023, 6, 1))

    # All six factors now reconstructed: the analyst factor from consensus, and
    # the sentiment factor from GDELT tone (72/100).
    assert result["factor_scores"]["analyst_confidence"] is not None
    assert result["factor_scores"]["sentiment"] == 72.0


def test_historical_score_none_when_edgar_has_no_data():
    with patch("engine.screener_history.edgar_fundamentals.get_pit_fundamentals", return_value={}):
        assert screener_history.historical_screener_score("NOPE", date(2023, 6, 1)) is None
