"""
Exercises app/pages/5_backtest.py via Streamlit's AppTest — catches UI-wiring
mistakes. Price history + risk-free rate are mocked; the engine logic itself is
covered in test_backtest.py.
"""
from datetime import date
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
from streamlit.testing.v1 import AppTest

from engine import portfolio

PAGE_PATH = str(Path(__file__).resolve().parent.parent / "app" / "pages" / "5_backtest.py")


def _rising_history(ticker, start, end, source="yfinance"):
    days = pd.bdate_range(start=start, end=end).date
    closes = 100.0 * (1.001 ** np.arange(len(days)))
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": [1] * len(days)},
        index=pd.Index(days, name="date"),
    )


_FIXED_RF = patch("engine.backtest.health._get_risk_free_rate_annual", return_value=(0.04, "test-fixed"))


def test_backtest_page_prompts_when_no_ticker():
    at = AppTest.from_file(PAGE_PATH)
    at.run(timeout=30)

    assert not at.exception
    assert any("backtest a strategy" in el.value for el in at.info)


def test_backtest_page_runs_and_renders_comparison():
    portfolio.add_holding("TEST", 10, 100.0, date(2023, 1, 2))

    with _FIXED_RF, patch("engine.backtest.price_history.get_history_df", side_effect=_rising_history):
        at = AppTest.from_file(PAGE_PATH)
        at.run(timeout=60)

        next(b for b in at.button if "Run backtest" in b.label).click()
        at.run(timeout=60)

    assert not at.exception
    metric_labels = {m.label for m in at.metric}
    assert "SPY buy & hold" in metric_labels
    assert any("Growth of your starting capital" in str(h.value) for h in at.subheader)


def test_backtest_page_save_run_persists():
    portfolio.add_holding("TEST", 10, 100.0, date(2023, 1, 2))

    with _FIXED_RF, patch("engine.backtest.price_history.get_history_df", side_effect=_rising_history):
        at = AppTest.from_file(PAGE_PATH)
        at.run(timeout=60)
        next(b for b in at.button if "Run backtest" in b.label).click()
        at.run(timeout=60)

        # The result persists in session_state, so the Save button works on its
        # own interaction (not gated on the Run button being freshly clicked).
        next(b for b in at.button if "Save this run" in b.label).click()
        at.run(timeout=60)

    assert not at.exception
    from engine import backtest
    assert len(backtest.list_backtest_runs()) == 1
