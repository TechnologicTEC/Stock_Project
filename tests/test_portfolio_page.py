"""
These exercise app/pages/1_portfolio.py itself (not just engine/portfolio.py)
via Streamlit's official AppTest framework, which runs the real page script
in-process. External calls are mocked for determinism — engine/portfolio.py's
own logic already has thorough coverage in test_portfolio.py; what these
tests catch is UI-wiring mistakes (wrong widget indices, a call that only
breaks once Streamlit actually renders it, etc).
"""
from datetime import date
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from engine import portfolio

PAGE_PATH = "app/pages/1_portfolio.py"


def _fake_quote(ticker, price=100.0):
    return {
        "ticker": ticker, "current_price": price, "change": 1.5, "percent_change": 1.5,
        "high": price + 1, "low": price - 1, "open": price, "previous_close": price - 1.5,
        "fetched_at": "now",
    }


def test_portfolio_page_renders_empty_state_without_error():
    at = AppTest.from_file(PAGE_PATH)
    at.run(timeout=30)

    assert not at.exception
    assert any("haven't added any holdings yet" in el.value for el in at.info)


def test_portfolio_page_renders_with_holdings_without_error():
    portfolio.add_holding("AAPL", 10, 150.0, date(2025, 6, 1))
    portfolio.add_holding("VTI", 5, 200.0, date(2025, 1, 1), asset_type="etf")

    fake_bars = [{"date": date(2026, 1, 2), "open": 1, "high": 1, "low": 1, "close": 1.0, "volume": 1}]

    with patch("engine.portfolio.finnhub_client.get_quote", side_effect=lambda t: _fake_quote(t)):
        with patch("engine.portfolio.finnhub_client.get_company_profile", side_effect=RuntimeError("no profile in test")):
            with patch("engine.portfolio.yfinance_client.get_historical_ohlcv", return_value=fake_bars):
                at = AppTest.from_file(PAGE_PATH)
                at.run(timeout=30)

    assert not at.exception
    metric_values = {m.label: m.value for m in at.metric}
    assert metric_values["Total value"] == "$1,500.00"  # AAPL: 10*$100 + VTI: 5*$100, fake quote price=100


def test_portfolio_page_add_holding_form_round_trip():
    at = AppTest.from_file(PAGE_PATH)
    at.run(timeout=30)

    at.text_input[0].set_value("NVDA")
    at.number_input[0].set_value(7)   # shares
    at.number_input[1].set_value(120.5)  # cost basis
    at.run(timeout=30)

    add_button = next(b for b in at.button if b.label == "Add holding")
    add_button.click()
    at.run(timeout=30)

    assert not at.exception
    holdings = portfolio.list_holdings()
    assert any(h["ticker"] == "NVDA" and h["shares"] == 7 for h in holdings)


def test_portfolio_page_delete_holding_round_trip():
    portfolio.add_holding("AAPL", 10, 150.0, date(2025, 6, 1))

    at = AppTest.from_file(PAGE_PATH)
    with patch("engine.portfolio.finnhub_client.get_quote", side_effect=lambda t: _fake_quote(t)):
        at.run(timeout=30)

    remove_selectbox = next(sb for sb in at.selectbox if sb.label == "Holding")
    remove_selectbox.set_value(remove_selectbox.options[0])
    with patch("engine.portfolio.finnhub_client.get_quote", side_effect=lambda t: _fake_quote(t)):
        at.run(timeout=30)

    delete_button = next(b for b in at.button if b.label == "Delete")
    with patch("engine.portfolio.finnhub_client.get_quote", side_effect=lambda t: _fake_quote(t)):
        delete_button.click()
        at.run(timeout=30)

    assert not at.exception
    assert portfolio.list_holdings() == []
