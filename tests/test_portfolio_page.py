"""
These exercise app/pages/1_portfolio.py itself (not just engine/portfolio.py)
via Streamlit's official AppTest framework, which runs the real page script
in-process. External calls are mocked for determinism — engine/portfolio.py's
own logic already has thorough coverage in test_portfolio.py; what these
tests catch is UI-wiring mistakes (wrong widget indices, a call that only
breaks once Streamlit actually renders it, etc).
"""
from datetime import date
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from engine import portfolio

# Absolute, not relative - AppTest.from_file() first checks the path
# relative to the CURRENT WORKING DIRECTORY, and only falls back to
# resolving relative to this test file if that fails. That fallback would
# look in tests/app/pages/... (wrong - app/ is a sibling of tests/, not
# inside it), so anyone running pytest from somewhere other than the
# project root would get a confusing FileNotFoundError. Building an
# absolute path here sidesteps the whole CWD-dependent lookup.
PAGE_PATH = str(Path(__file__).resolve().parent.parent / "app" / "pages" / "1_portfolio.py")


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
            with patch("engine.price_history.yfinance_client.get_historical_ohlcv", return_value=fake_bars):
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


def test_portfolio_page_wallet_deposit_and_withdraw_round_trip():
    at = AppTest.from_file(PAGE_PATH)
    at.run(timeout=30)

    deposit_input = next(ni for ni in at.number_input if ni.label == "Deposit ($)")
    deposit_input.set_value(500.0)
    at.run(timeout=30)
    next(b for b in at.button if b.label == "Deposit").click()
    at.run(timeout=30)

    assert not at.exception
    assert portfolio.get_wallet_balance() == 500.0

    withdraw_input = next(ni for ni in at.number_input if ni.label == "Withdraw ($)")
    withdraw_input.set_value(200.0)
    at.run(timeout=30)
    next(b for b in at.button if b.label == "Withdraw").click()
    at.run(timeout=30)

    assert not at.exception
    assert portfolio.get_wallet_balance() == 300.0


def test_portfolio_page_sell_holding_round_trip():
    portfolio.add_holding("AAPL", 10, 100.0, date(2025, 6, 1))

    with patch("engine.portfolio.finnhub_client.get_quote", side_effect=lambda t: _fake_quote(t, price=150.0)):
        at = AppTest.from_file(PAGE_PATH)
        at.run(timeout=30)

        shares_input = next(ni for ni in at.number_input if ni.label == "Shares to sell")
        shares_input.set_value(4)
        at.run(timeout=30)

        sell_button = next(b for b in at.button if b.label == "Sell")
        sell_button.click()
        at.run(timeout=30)

    assert not at.exception
    holdings = portfolio.list_holdings()
    assert len(holdings) == 1
    assert holdings[0]["shares"] == 6
    assert portfolio.get_wallet_balance() == 4 * 150.0  # defaults to the live quote price


def test_portfolio_page_still_renders_after_selling_everything():
    # Once all positions are sold there are no current holdings, but the
    # page must still show the value chart and the cash proceeds rather than
    # falling back to the "no holdings yet" getting-started state.
    holding_id = portfolio.add_holding("AAPL", 10, 100.0, date(2025, 6, 1))
    portfolio.sell_holding(holding_id, 10, 150.0, date(2025, 6, 2))
    assert portfolio.list_holdings() == []

    fake_bars = [{"date": date(2026, 1, 2), "open": 1, "high": 1, "low": 1, "close": 1.0, "volume": 1}]
    with patch("engine.price_history.yfinance_client.get_historical_ohlcv", return_value=fake_bars):
        at = AppTest.from_file(PAGE_PATH)
        at.run(timeout=30)

    assert not at.exception
    metric_values = {m.label: m.value for m in at.metric}
    assert metric_values["Wallet (cash)"] == "$1,500.00"     # 10 * $150 proceeds
    assert metric_values["Total value"] == "$1,500.00"       # no holdings, all cash
    # the "you've sold everything" note shows, not the "no holdings yet" one
    assert any("sold all your positions" in el.value for el in at.info)
    assert not any("haven't added any holdings yet" in el.value for el in at.info)
