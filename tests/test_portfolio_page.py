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


def test_portfolio_page_shows_holdings_value_distinct_from_cost_basis():
    # Paid $1,500 (10 × $150); now worth $1,000 (10 × $100) — a $500 loss.
    portfolio.add_holding("AAPL", 10, 150.0, date(2025, 6, 1))
    fake_bars = [{"date": date(2026, 1, 2), "open": 1, "high": 1, "low": 1, "close": 1.0, "volume": 1}]

    with patch("engine.portfolio.finnhub_client.get_quote", side_effect=lambda t: _fake_quote(t, price=100.0)), \
         patch("engine.portfolio.finnhub_client.get_company_profile", side_effect=RuntimeError("no profile")), \
         patch("engine.price_history.yfinance_client.get_historical_ohlcv", return_value=fake_bars):
        at = AppTest.from_file(PAGE_PATH)
        at.run(timeout=30)

    assert not at.exception
    m = {metric.label: metric.value for metric in at.metric}
    assert m["Holdings value"] == "$1,000.00"     # current market value (was previously not shown at all)
    assert m["Cost basis"] == "$1,500.00"          # what was paid
    assert m["Total gain / loss"] == "$-500.00"    # the difference
    assert m["Total value"] == "$1,000.00"         # holdings value + $0 wallet


def test_portfolio_page_screener_ratings_are_opt_in_and_add_a_column():
    from types import SimpleNamespace
    portfolio.add_holding("AAPL", 10, 150.0, date(2025, 6, 1))
    fake_bars = [{"date": date(2026, 1, 2), "open": 1, "high": 1, "low": 1, "close": 1.0, "volume": 1}]
    result = [SimpleNamespace(ticker="AAPL", overall_score=68.0, recommendation="Buy")]

    with patch("engine.portfolio.finnhub_client.get_quote", side_effect=lambda t: _fake_quote(t)), \
         patch("engine.portfolio.finnhub_client.get_company_profile", side_effect=RuntimeError("no profile")), \
         patch("engine.price_history.yfinance_client.get_historical_ohlcv", return_value=fake_bars), \
         patch("engine.screener.screen_tickers", return_value=result) as screen:
        at = AppTest.from_file(PAGE_PATH)
        at.run(timeout=30)
        screen.assert_not_called()                       # heavy screener is NOT run by default

        next(c for c in at.checkbox if "Rate my holdings" in c.label).set_value(True).run()

    assert not at.exception
    screen.assert_called_once()                          # ...only after opting in
    holdings_df = next(d.value for d in at.dataframe if "Screener" in list(d.value.columns))
    assert "Buy · 68" in holdings_df["Screener"].tolist()


def test_portfolio_page_currency_toggle_converts_displayed_values_to_nzd():
    portfolio.add_holding("AAPL", 10, 100.0, date(2025, 6, 1))
    fake_bars = [{"date": date(2026, 1, 2), "open": 1, "high": 1, "low": 1, "close": 1.0, "volume": 1}]
    fx = {"value": 0.5, "date": "2026-06-30", "source": "ECB (frankfurter.app)"}  # USD/NZD 0.5 -> 1 USD = 2 NZD

    at = AppTest.from_file(PAGE_PATH)
    with patch("engine.portfolio.finnhub_client.get_quote", side_effect=lambda t: _fake_quote(t, price=150.0)):
        with patch("engine.portfolio.finnhub_client.get_company_profile", side_effect=RuntimeError("no profile")):
            with patch("engine.price_history.yfinance_client.get_historical_ohlcv", return_value=fake_bars):
                with patch("engine.currency.frankfurter_client.usd_per_nzd", return_value=fx):
                    at.run(timeout=30)
                    assert {m.label: m.value for m in at.metric}["Total value"] == "$1,500.00"  # USD default

                    next(r for r in at.radio if r.label == "Display currency").set_value("NZD")
                    at.run(timeout=30)

    assert not at.exception
    metric_values = {m.label: m.value for m in at.metric}
    assert metric_values["Total value"] == "NZ$3,000.00"  # 10 * $150 = $1,500 -> NZ$3,000
    assert metric_values["Cost basis"] == "NZ$2,000.00"    # 10 * $100 = $1,000 -> NZ$2,000


def test_portfolio_page_event_markers_toggle_renders_both_ways():
    portfolio.add_holding("AAPL", 10, 150.0, date(2025, 6, 1))  # writes a buy transaction
    portfolio.deposit_to_wallet(500.0, when=date(2025, 6, 2))   # a cash-flow event too
    fake_bars = [{"date": date(2026, 1, 2), "open": 1, "high": 1, "low": 1, "close": 1.0, "volume": 1}]

    with patch("engine.portfolio.finnhub_client.get_quote", side_effect=lambda t: _fake_quote(t)):
        with patch("engine.portfolio.finnhub_client.get_company_profile", side_effect=RuntimeError("no profile")):
            with patch("engine.price_history.yfinance_client.get_historical_ohlcv", return_value=fake_bars):
                at = AppTest.from_file(PAGE_PATH)
                at.run(timeout=30)
                assert not at.exception  # markers shown by default

                marker_toggle = next(cb for cb in at.checkbox if "event markers" in cb.label)
                assert marker_toggle.value is True
                marker_toggle.set_value(False)
                at.run(timeout=30)

    assert not at.exception  # toggling markers off still renders cleanly


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
    assert portfolio.list_transactions("AAPL") == []  # removing the position purges its history too


def test_portfolio_page_undo_a_sell_from_history_round_trip():
    holding_id = portfolio.add_holding("AAPL", 10, 100.0, date(2025, 6, 1))
    portfolio.sell_holding(holding_id, 4, 150.0, date(2025, 6, 2))  # 6 left, $600 in wallet
    sell = next(e for e in portfolio.list_activity() if e["action"] == "Sell")

    at = AppTest.from_file(PAGE_PATH)
    with patch("engine.portfolio.finnhub_client.get_quote", side_effect=lambda t: _fake_quote(t)):
        at.run(timeout=30)

        entry_select = next(sb for sb in at.selectbox if sb.label == "Entry to remove")
        # pick the Sell entry's label
        sell_label = next(o for o in entry_select.options if "Sell" in o)
        entry_select.set_value(sell_label)
        at.run(timeout=30)

        next(b for b in at.button if b.label == "Delete entry").click()
        at.run(timeout=30)

    assert not at.exception
    assert portfolio.list_holdings()[0]["shares"] == 10     # shares restored
    assert portfolio.get_wallet_balance() == 0.0            # proceeds removed
    assert all(e["action"] != "Sell" for e in portfolio.list_activity())


def test_portfolio_page_reset_clears_everything():
    portfolio.add_holding("AAPL", 10, 100.0, date(2025, 6, 1))
    portfolio.deposit_to_wallet(500.0)

    at = AppTest.from_file(PAGE_PATH)
    with patch("engine.portfolio.finnhub_client.get_quote", side_effect=lambda t: _fake_quote(t)):
        at.run(timeout=30)

        # the reset button is disabled until the confirmation checkbox is ticked
        next(cb for cb in at.checkbox if "clear everything" in cb.label).set_value(True)
        at.run(timeout=30)

        next(b for b in at.button if b.label == "Reset portfolio").click()
        at.run(timeout=30)

    assert not at.exception
    assert portfolio.list_holdings() == []
    assert portfolio.list_activity() == []
    assert portfolio.get_wallet_balance() == 0.0


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
