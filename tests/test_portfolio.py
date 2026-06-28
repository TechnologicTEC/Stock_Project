import io
from datetime import date
from unittest.mock import patch

import pytest

from engine import portfolio


# --------------------------------------------------------------------------
# Holdings CRUD
# --------------------------------------------------------------------------

def test_add_and_list_holding():
    holding_id = portfolio.add_holding("aapl", 10, 150.0, date(2025, 6, 1))
    holdings = portfolio.list_holdings()

    assert len(holdings) == 1
    assert holdings[0]["id"] == holding_id
    assert holdings[0]["ticker"] == "AAPL"  # normalized to uppercase
    assert holdings[0]["asset_type"] == "stock"  # default


def test_add_holding_rejects_invalid_shares_and_cost():
    with pytest.raises(ValueError):
        portfolio.add_holding("AAPL", 0, 150.0, date(2025, 6, 1))
    with pytest.raises(ValueError):
        portfolio.add_holding("AAPL", 10, -1, date(2025, 6, 1))


def test_add_holding_normalizes_unknown_asset_type_to_other():
    portfolio.add_holding("BTC", 1, 50000.0, date(2025, 6, 1), asset_type="cryptocurrency")
    holdings = portfolio.list_holdings()
    assert holdings[0]["asset_type"] == "other"


def test_delete_holding():
    holding_id = portfolio.add_holding("AAPL", 10, 150.0, date(2025, 6, 1))
    assert portfolio.delete_holding(holding_id) is True
    assert portfolio.list_holdings() == []
    assert portfolio.delete_holding(999999) is False  # already gone / never existed


def test_record_and_list_transactions():
    portfolio.record_transaction("AAPL", "buy", 10, 150.0, date(2025, 1, 1))
    portfolio.record_transaction("AAPL", "sell", 4, 180.0, date(2025, 6, 1))

    txns = portfolio.list_transactions("aapl")
    assert len(txns) == 2
    assert [t["type"] for t in txns] == ["buy", "sell"]


def test_record_transaction_rejects_bad_type():
    with pytest.raises(ValueError):
        portfolio.record_transaction("AAPL", "transfer", 1, 100.0, date(2025, 1, 1))


# --------------------------------------------------------------------------
# CSV import
# --------------------------------------------------------------------------

def test_csv_import_adds_valid_rows_and_skips_bad_ones():
    csv_text = (
        "ticker,shares,cost_basis,purchase_date\n"
        "AAPL,10,150.0,2025-06-01\n"
        "MSFT,not_a_number,300.0,2025-01-01\n"  # bad shares - should be skipped
        "NVDA,5,120.0,2025-03-15\n"
    )
    result = portfolio.import_holdings_from_csv(io.StringIO(csv_text))

    assert result.added == 2
    assert len(result.errors) == 1
    assert "Row 3" in result.errors[0]
    tickers = {h["ticker"] for h in portfolio.list_holdings()}
    assert tickers == {"AAPL", "NVDA"}


def test_csv_import_reports_missing_columns():
    csv_text = "ticker,shares\nAAPL,10\n"
    result = portfolio.import_holdings_from_csv(io.StringIO(csv_text))

    assert result.added == 0
    assert "cost_basis" in result.errors[0]
    assert "purchase_date" in result.errors[0]


def test_csv_import_respects_optional_asset_type_column():
    csv_text = "ticker,shares,cost_basis,purchase_date,asset_type\nVTI,3,200.0,2025-02-01,etf\n"
    result = portfolio.import_holdings_from_csv(io.StringIO(csv_text))

    assert result.added == 1
    assert portfolio.list_holdings()[0]["asset_type"] == "etf"


# --------------------------------------------------------------------------
# Quotes — caching + fallback behavior
# --------------------------------------------------------------------------

def test_get_quote_cached_only_calls_finnhub_once_within_ttl():
    calls = {"n": 0}

    def fake_quote(ticker):
        calls["n"] += 1
        return {"ticker": ticker, "current_price": 100.0, "change": 1.0, "percent_change": 1.0,
                "high": 101, "low": 99, "open": 99.5, "previous_close": 99.0, "fetched_at": "now"}

    with patch("engine.portfolio.finnhub_client.get_quote", side_effect=fake_quote):
        portfolio.get_quote_cached("AAPL")
        portfolio.get_quote_cached("AAPL")

    assert calls["n"] == 1


def test_quote_falls_back_to_alpaca_when_finnhub_fails():
    with patch("engine.portfolio.finnhub_client.get_quote", side_effect=RuntimeError("finnhub down")):
        with patch(
            "engine.portfolio.alpaca_client.get_latest_quote",
            return_value={"ticker": "AAPL", "ask_price": 101.0, "bid_price": 99.0, "timestamp": "now"},
        ):
            quote = portfolio.get_quote_cached("AAPL")

    assert quote["current_price"] == 100.0  # midpoint of bid/ask
    assert quote["source"] == "alpaca_fallback"


def test_quote_raises_original_error_when_both_sources_fail():
    with patch("engine.portfolio.finnhub_client.get_quote", side_effect=RuntimeError("finnhub down")):
        with patch("engine.portfolio.alpaca_client.get_latest_quote", side_effect=RuntimeError("alpaca down too")):
            with pytest.raises(RuntimeError, match="finnhub down"):
                portfolio.get_quote_cached("ZZZZ")


# --------------------------------------------------------------------------
# Valuation + summary
# --------------------------------------------------------------------------

def _fake_quote(ticker, price, change=1.0, pct=1.0):
    return {
        "ticker": ticker, "current_price": price, "change": change, "percent_change": pct,
        "high": price + 1, "low": price - 1, "open": price, "previous_close": price - change,
        "fetched_at": "now",
    }


def test_get_live_valuation_computes_gain_loss():
    portfolio.add_holding("AAPL", 10, 100.0, date(2025, 1, 1))

    with patch("engine.portfolio.finnhub_client.get_quote", return_value=_fake_quote("AAPL", 120.0)):
        valuation = portfolio.get_live_valuation()

    assert valuation[0]["market_value"] == 1200.0
    assert valuation[0]["cost_total"] == 1000.0
    assert valuation[0]["gain_loss"] == 200.0
    assert valuation[0]["gain_loss_pct"] == 20.0
    assert valuation[0]["error"] is None


def test_get_live_valuation_reports_per_holding_error_without_failing_others():
    portfolio.add_holding("AAPL", 10, 100.0, date(2025, 1, 1))
    portfolio.add_holding("BADTICKER", 5, 50.0, date(2025, 1, 1))

    def quote_side_effect(ticker):
        if ticker == "BADTICKER":
            raise RuntimeError("no data for this ticker")
        return _fake_quote(ticker, 120.0)

    with patch("engine.portfolio.finnhub_client.get_quote", side_effect=quote_side_effect):
        with patch("engine.portfolio.alpaca_client.get_latest_quote", side_effect=RuntimeError("also down")):
            valuation = portfolio.get_live_valuation()

    by_ticker = {v["ticker"]: v for v in valuation}
    assert by_ticker["AAPL"]["market_value"] == 1200.0
    assert by_ticker["BADTICKER"]["market_value"] is None
    assert by_ticker["BADTICKER"]["error"] is not None


def test_portfolio_summary_aggregates_across_holdings():
    portfolio.add_holding("AAPL", 10, 100.0, date(2025, 1, 1))
    portfolio.add_holding("MSFT", 2, 200.0, date(2025, 1, 1))

    def quote_side_effect(ticker):
        return _fake_quote(ticker, 150.0 if ticker == "AAPL" else 250.0, change=5.0)

    with patch("engine.portfolio.finnhub_client.get_quote", side_effect=quote_side_effect):
        summary = portfolio.get_portfolio_summary()

    assert summary["total_value"] == 10 * 150.0 + 2 * 250.0
    assert summary["total_cost"] == 10 * 100.0 + 2 * 200.0
    assert summary["holdings_with_errors"] == []


# --------------------------------------------------------------------------
# Allocation
# --------------------------------------------------------------------------

def test_allocation_by_asset_type_groups_correctly():
    portfolio.add_holding("AAPL", 10, 100.0, date(2025, 1, 1), asset_type="stock")
    portfolio.add_holding("VTI", 5, 200.0, date(2025, 1, 1), asset_type="etf")

    with patch("engine.portfolio.finnhub_client.get_quote", side_effect=lambda t: _fake_quote(t, 100.0)):
        allocation = portfolio.get_allocation_by_asset_type()

    labels = {a["label"] for a in allocation}
    assert labels == {"stock", "etf"}


def test_allocation_by_sector_falls_back_to_unknown_on_profile_failure():
    portfolio.add_holding("AAPL", 10, 100.0, date(2025, 1, 1))

    with patch("engine.portfolio.finnhub_client.get_quote", return_value=_fake_quote("AAPL", 100.0)):
        with patch("engine.portfolio.finnhub_client.get_company_profile", side_effect=RuntimeError("no profile")):
            allocation = portfolio.get_allocation_by_sector()

    assert allocation == [{"label": "Unknown", "value": 1000.0}]


# --------------------------------------------------------------------------
# Value history
# --------------------------------------------------------------------------

def test_value_history_uses_transactions_when_present():
    portfolio.record_transaction("AAPL", "buy", 10, 100.0, date(2026, 1, 5))   # a Monday
    portfolio.add_holding("AAPL", 10, 100.0, date(2026, 1, 5))

    fake_bars = [
        {"date": date(2026, 1, 5), "open": 10, "high": 10, "low": 10, "close": 10.0, "volume": 1},
        {"date": date(2026, 1, 6), "open": 10, "high": 10, "low": 10, "close": 12.0, "volume": 1},
    ]
    with patch("engine.portfolio.yfinance_client.get_historical_ohlcv", return_value=fake_bars):
        history = portfolio.get_value_history(date(2026, 1, 1), date(2026, 1, 6))

    by_date = {h["date"]: h["value"] for h in history}
    assert by_date[date(2026, 1, 1)] == 0.0      # before the buy transaction
    assert by_date[date(2026, 1, 5)] == 100.0     # 10 shares * $10
    assert by_date[date(2026, 1, 6)] == 120.0     # 10 shares * $12


def test_value_history_falls_back_to_holding_snapshot_without_transactions():
    portfolio.add_holding("AAPL", 4, 100.0, date(2026, 1, 5))  # no transaction logged for this one

    fake_bars = [{"date": date(2026, 1, 5), "open": 10, "high": 10, "low": 10, "close": 25.0, "volume": 1}]
    with patch("engine.portfolio.yfinance_client.get_historical_ohlcv", return_value=fake_bars):
        history = portfolio.get_value_history(date(2026, 1, 1), date(2026, 1, 5))

    by_date = {h["date"]: h["value"] for h in history}
    assert by_date[date(2026, 1, 1)] == 0.0       # before purchase_date
    assert by_date[date(2026, 1, 5)] == 100.0      # 4 shares * $25


def test_value_history_empty_when_no_holdings():
    assert portfolio.get_value_history(date(2026, 1, 1), date(2026, 1, 31)) == []
