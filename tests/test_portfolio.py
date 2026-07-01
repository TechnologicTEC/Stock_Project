import io
from datetime import date
from unittest.mock import patch

import pytest

from db.models import Holding, Transaction, Wallet
from db.session import get_session
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


def test_add_holding_also_writes_a_matching_buy_transaction():
    portfolio.add_holding("AAPL", 10, 150.0, date(2025, 6, 1))

    txns = portfolio.list_transactions("AAPL")
    assert len(txns) == 1
    assert txns[0]["type"] == "buy"
    assert txns[0]["shares"] == 10
    assert txns[0]["price"] == 150.0
    assert txns[0]["date"] == date(2025, 6, 1)


# --------------------------------------------------------------------------
# Selling
# --------------------------------------------------------------------------

def test_sell_partial_reduces_shares_keeps_holding_and_credits_wallet():
    holding_id = portfolio.add_holding("AAPL", 10, 100.0, date(2025, 1, 1))

    result = portfolio.sell_holding(holding_id, 4, 180.0, date(2025, 6, 1))

    assert result == {
        "ticker": "AAPL", "shares_sold": 4, "proceeds": 720.0,
        "remaining_shares": 6, "holding_closed": False,
    }
    holdings = portfolio.list_holdings()
    assert len(holdings) == 1
    assert holdings[0]["shares"] == 6
    assert holdings[0]["cost_basis"] == 100.0  # average-cost accounting - unchanged on a partial sell
    assert portfolio.get_wallet_balance() == 720.0

    txns = portfolio.list_transactions("AAPL")
    assert [t["type"] for t in txns] == ["buy", "sell"]


def test_sell_all_shares_removes_the_holding():
    holding_id = portfolio.add_holding("AAPL", 10, 100.0, date(2025, 1, 1))

    result = portfolio.sell_holding(holding_id, 10, 150.0, date(2025, 6, 1))

    assert result["holding_closed"] is True
    assert result["remaining_shares"] == 0.0
    assert portfolio.list_holdings() == []
    assert portfolio.get_wallet_balance() == 1500.0
    # transaction history survives even though the holding is gone
    assert [t["type"] for t in portfolio.list_transactions("AAPL")] == ["buy", "sell"]


def test_sell_rejects_more_shares_than_held():
    holding_id = portfolio.add_holding("AAPL", 10, 100.0, date(2025, 1, 1))
    with pytest.raises(ValueError):
        portfolio.sell_holding(holding_id, 11, 150.0, date(2025, 6, 1))


def test_sell_rejects_nonexistent_holding():
    with pytest.raises(ValueError):
        portfolio.sell_holding(999999, 1, 150.0, date(2025, 6, 1))


def test_sell_rejects_zero_or_negative_shares():
    holding_id = portfolio.add_holding("AAPL", 10, 100.0, date(2025, 1, 1))
    with pytest.raises(ValueError):
        portfolio.sell_holding(holding_id, 0, 150.0, date(2025, 6, 1))


# --------------------------------------------------------------------------
# Wallet
# --------------------------------------------------------------------------

def test_wallet_starts_at_zero():
    assert portfolio.get_wallet_balance() == 0.0


def test_wallet_deposit_and_withdraw():
    assert portfolio.deposit_to_wallet(500.0) == 500.0
    assert portfolio.withdraw_from_wallet(200.0) == 300.0
    assert portfolio.get_wallet_balance() == 300.0


def test_wallet_movements_are_logged_as_dated_cash_flows():
    portfolio.deposit_to_wallet(500.0, when=date(2026, 1, 5))
    portfolio.withdraw_from_wallet(200.0, when=date(2026, 1, 6))

    flows = portfolio.list_cash_flows()
    assert [(f["type"], f["amount"], f["date"]) for f in flows] == [
        ("deposit", 500.0, date(2026, 1, 5)),
        ("withdraw", 200.0, date(2026, 1, 6)),
    ]


def test_selling_does_not_create_a_manual_cash_flow():
    # sale proceeds are dated via the transactions ledger, not CashFlow, so
    # the cash series doesn't double-count them
    holding_id = portfolio.add_holding("AAPL", 10, 100.0, date(2026, 1, 5))
    portfolio.sell_holding(holding_id, 10, 120.0, date(2026, 1, 6))
    assert portfolio.list_cash_flows() == []


def test_wallet_withdraw_rejects_insufficient_balance():
    portfolio.deposit_to_wallet(100.0)
    with pytest.raises(ValueError):
        portfolio.withdraw_from_wallet(150.0)


def test_wallet_rejects_non_positive_amounts():
    with pytest.raises(ValueError):
        portfolio.deposit_to_wallet(0.0)
    with pytest.raises(ValueError):
        portfolio.withdraw_from_wallet(-10.0)


# --------------------------------------------------------------------------
# Backfill (Section 6.10)
# --------------------------------------------------------------------------

def test_backfill_creates_synthetic_buy_for_legacy_holding_without_transactions():
    with get_session() as session:
        session.add(Holding(ticker="AAPL", shares=4, cost_basis=100.0, purchase_date=date(2025, 3, 1)))

    created = portfolio.backfill_missing_transactions()

    assert created == 1
    txns = portfolio.list_transactions("AAPL")
    assert len(txns) == 1
    assert txns[0] == {
        "id": txns[0]["id"], "ticker": "AAPL", "type": "buy", "shares": 4, "price": 100.0,
        "date": date(2025, 3, 1),
    }


def test_backfill_skips_holdings_that_already_have_transactions():
    portfolio.add_holding("AAPL", 10, 150.0, date(2025, 6, 1))  # already writes its own buy transaction

    created = portfolio.backfill_missing_transactions()

    assert created == 0
    assert len(portfolio.list_transactions("AAPL")) == 1


def test_backfill_is_idempotent_across_repeated_calls():
    with get_session() as session:
        session.add(Holding(ticker="AAPL", shares=4, cost_basis=100.0, purchase_date=date(2025, 3, 1)))

    first = portfolio.backfill_missing_transactions()
    second = portfolio.backfill_missing_transactions()

    assert first == 1
    assert second == 0
    assert len(portfolio.list_transactions("AAPL")) == 1


# --------------------------------------------------------------------------
# Wallet cash-flow backfill (reconciles balances that pre-date the ledger)
# --------------------------------------------------------------------------

def test_wallet_cash_flow_backfill_reconstructs_undated_manual_balance():
    # Simulate a wallet that existed before manual movements were dated:
    # a balance with no CashFlow rows and no sale to explain it.
    with get_session() as session:
        session.add(Holding(ticker="AAPL", shares=4, cost_basis=100.0, purchase_date=date(2025, 3, 1)))
        session.add(Wallet(balance=250.0))

    created = portfolio.backfill_wallet_cash_flows()

    assert created == 1
    flows = portfolio.list_cash_flows()
    assert flows == [{"id": flows[0]["id"], "type": "deposit", "amount": 250.0, "date": date(2025, 3, 1)}]


def test_wallet_cash_flow_backfill_ignores_balance_explained_by_sales():
    # A balance that's entirely sale proceeds needs no synthetic cash flow —
    # those proceeds are already dated in the transactions ledger.
    holding_id = portfolio.add_holding("AAPL", 10, 100.0, date(2025, 1, 1))
    portfolio.sell_holding(holding_id, 10, 120.0, date(2025, 6, 1))  # wallet now $1200, all from the sale

    created = portfolio.backfill_wallet_cash_flows()

    assert created == 0
    assert portfolio.list_cash_flows() == []


def test_wallet_cash_flow_backfill_is_idempotent():
    with get_session() as session:
        session.add(Wallet(balance=250.0))

    assert portfolio.backfill_wallet_cash_flows() == 1
    assert portfolio.backfill_wallet_cash_flows() == 0
    assert len(portfolio.list_cash_flows()) == 1


def test_wallet_cash_flow_backfill_dates_missing_cash_at_the_end_not_the_start():
    # Anomalous legacy state: sale proceeds were recorded but the wallet is
    # empty (cash already left). The reconciling withdrawal must be dated at
    # the latest activity, so the chart's cash pile isn't dragged negative
    # for the whole history before the sale.
    with get_session() as session:
        session.add(Transaction(ticker="AAPL", type="buy", shares=10, price=100.0, date=date(2026, 1, 5)))
        session.add(Transaction(ticker="AAPL", type="sell", shares=10, price=120.0, date=date(2026, 6, 1)))
        session.add(Wallet(balance=0.0))  # proceeds of $1200 are NOT here

    created = portfolio.backfill_wallet_cash_flows()

    assert created == 1
    flow = portfolio.list_cash_flows()[0]
    assert flow["type"] == "withdraw"
    assert flow["amount"] == 1200.0
    assert flow["date"] == date(2026, 6, 1)  # latest activity, not the 2026-01-05 buy


def test_earliest_activity_date_considers_holdings_and_transactions():
    assert portfolio.earliest_activity_date() is None
    portfolio.add_holding("AAPL", 10, 100.0, date(2025, 6, 1))
    portfolio.record_transaction("MSFT", "buy", 5, 200.0, date(2025, 1, 15))  # earlier than the holding
    assert portfolio.earliest_activity_date() == date(2025, 1, 15)


# --------------------------------------------------------------------------
# Activity history & corrections (undo / delete)
# --------------------------------------------------------------------------

def test_list_activity_includes_all_action_types_newest_first():
    holding_id = portfolio.add_holding("AAPL", 10, 100.0, date(2026, 1, 5))  # buy
    portfolio.sell_holding(holding_id, 4, 150.0, date(2026, 2, 1))           # sell
    portfolio.deposit_to_wallet(500.0, when=date(2026, 3, 1))                # deposit
    portfolio.withdraw_from_wallet(50.0, when=date(2026, 4, 1))              # withdraw

    activity = portfolio.list_activity()
    assert [e["action"] for e in activity] == ["Withdraw", "Deposit", "Sell", "Buy"]  # newest first
    buy = next(e for e in activity if e["action"] == "Buy")
    assert (buy["kind"], buy["ticker"], buy["shares"], buy["amount"]) == ("transaction", "AAPL", 10, 1000.0)
    deposit = next(e for e in activity if e["action"] == "Deposit")
    assert (deposit["kind"], deposit["ticker"], deposit["amount"]) == ("cash_flow", None, 500.0)


def test_delete_activity_undoes_a_buy_and_removes_it_from_history():
    portfolio.add_holding("AAPL", 10, 100.0, date(2026, 1, 5))
    buy = portfolio.list_activity()[0]

    portfolio.delete_activity(buy["kind"], buy["id"])

    assert portfolio.list_holdings() == []          # the position is gone
    assert portfolio.list_activity() == []          # and so is its history entry


def test_delete_activity_undoes_a_sell_restoring_shares_and_debiting_the_wallet():
    holding_id = portfolio.add_holding("AAPL", 10, 100.0, date(2026, 1, 5))
    portfolio.sell_holding(holding_id, 4, 150.0, date(2026, 2, 1))  # 6 shares left, $600 in wallet
    sell = next(e for e in portfolio.list_activity() if e["action"] == "Sell")

    portfolio.delete_activity(sell["kind"], sell["id"])

    holdings = portfolio.list_holdings()
    assert len(holdings) == 1
    assert holdings[0]["shares"] == 10            # shares restored
    assert holdings[0]["cost_basis"] == 100.0      # average cost unchanged
    assert portfolio.get_wallet_balance() == 0.0   # proceeds removed from the wallet
    assert [e["action"] for e in portfolio.list_activity()] == ["Buy"]  # only the buy remains


def test_delete_activity_undoes_a_deposit():
    portfolio.deposit_to_wallet(500.0, when=date(2026, 1, 5))
    deposit = portfolio.list_activity()[0]

    portfolio.delete_activity(deposit["kind"], deposit["id"])

    assert portfolio.get_wallet_balance() == 0.0
    assert portfolio.list_cash_flows() == []


def test_delete_activity_refuses_to_orphan_a_later_sell():
    holding_id = portfolio.add_holding("AAPL", 10, 100.0, date(2026, 1, 5))
    portfolio.sell_holding(holding_id, 4, 150.0, date(2026, 2, 1))
    buy = next(e for e in portfolio.list_activity() if e["action"] == "Buy")

    with pytest.raises(ValueError, match="more shares sold than bought"):
        portfolio.delete_activity(buy["kind"], buy["id"])

    # nothing changed — the deletion rolled back
    assert portfolio.list_holdings()[0]["shares"] == 6
    assert {e["action"] for e in portfolio.list_activity()} == {"Buy", "Sell"}


def test_delete_activity_refuses_when_it_would_make_the_wallet_negative():
    holding_id = portfolio.add_holding("AAPL", 10, 100.0, date(2026, 1, 5))
    portfolio.sell_holding(holding_id, 4, 150.0, date(2026, 2, 1))  # +$600
    portfolio.withdraw_from_wallet(600.0, when=date(2026, 3, 1))    # wallet back to $0
    sell = next(e for e in portfolio.list_activity() if e["action"] == "Sell")

    with pytest.raises(ValueError, match="negative"):
        portfolio.delete_activity(sell["kind"], sell["id"])

    assert portfolio.get_wallet_balance() == 0.0  # unchanged
    assert {e["action"] for e in portfolio.list_activity()} == {"Buy", "Sell", "Withdraw"}


def test_delete_activity_undo_leaves_the_value_chart_as_if_it_never_happened():
    holding_id = portfolio.add_holding("AAPL", 10, 10.0, date(2026, 1, 5))
    fake_bars = [
        {"date": date(2026, 1, 5), "open": 10, "high": 10, "low": 10, "close": 10.0, "volume": 1},
        {"date": date(2026, 1, 6), "open": 10, "high": 10, "low": 10, "close": 10.0, "volume": 1},
    ]
    with patch("engine.price_history.yfinance_client.get_historical_ohlcv", return_value=fake_bars):
        before = portfolio.get_value_history(date(2026, 1, 5), date(2026, 1, 6))

        portfolio.sell_holding(holding_id, 5, 30.0, date(2026, 1, 6))   # mistaken sale
        sell = next(e for e in portfolio.list_activity() if e["action"] == "Sell")
        portfolio.delete_activity(sell["kind"], sell["id"])             # undo it

        after = portfolio.get_value_history(date(2026, 1, 5), date(2026, 1, 6))

    assert after == before  # the chart is identical to before the mistaken sale


def test_delete_position_purges_holding_and_its_transactions():
    holding_id = portfolio.add_holding("AAPL", 10, 100.0, date(2026, 1, 5))
    portfolio.add_holding("MSFT", 5, 200.0, date(2026, 1, 5))
    portfolio.sell_holding(holding_id, 4, 150.0, date(2026, 2, 1))  # AAPL: $600 proceeds in wallet

    portfolio.delete_position("AAPL")

    assert {h["ticker"] for h in portfolio.list_holdings()} == {"MSFT"}
    assert portfolio.list_transactions("AAPL") == []
    assert portfolio.get_wallet_balance() == 0.0  # AAPL's sale proceeds removed with it
    assert {e["ticker"] for e in portfolio.list_activity() if e["kind"] == "transaction"} == {"MSFT"}


def test_reset_portfolio_clears_everything():
    holding_id = portfolio.add_holding("AAPL", 10, 100.0, date(2026, 1, 5))
    portfolio.sell_holding(holding_id, 4, 150.0, date(2026, 2, 1))
    portfolio.deposit_to_wallet(500.0)

    portfolio.reset_portfolio()

    assert portfolio.list_holdings() == []
    assert portfolio.list_transactions() == []
    assert portfolio.list_cash_flows() == []
    assert portfolio.get_wallet_balance() == 0.0
    assert portfolio.list_activity() == []


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

    assert summary["invested_value"] == 10 * 150.0 + 2 * 250.0
    assert summary["wallet_balance"] == 0.0
    assert summary["total_value"] == summary["invested_value"]  # no wallet balance yet
    assert summary["total_cost"] == 10 * 100.0 + 2 * 200.0
    assert summary["holdings_with_errors"] == []


def test_portfolio_summary_total_value_includes_wallet_balance():
    portfolio.add_holding("AAPL", 10, 100.0, date(2025, 1, 1))
    portfolio.deposit_to_wallet(500.0)

    with patch("engine.portfolio.finnhub_client.get_quote", return_value=_fake_quote("AAPL", 150.0)):
        summary = portfolio.get_portfolio_summary()

    assert summary["invested_value"] == 1500.0
    assert summary["wallet_balance"] == 500.0
    assert summary["total_value"] == 2000.0
    # gain/loss is based on invested value only - the wallet has no cost basis
    assert summary["total_gain_loss"] == 500.0


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


def test_bucket_market_cap_thresholds():
    assert portfolio.bucket_market_cap(250_000) == "Mega cap (>$200B)"
    assert portfolio.bucket_market_cap(50_000) == "Large cap ($10B-$200B)"
    assert portfolio.bucket_market_cap(5_000) == "Mid cap ($2B-$10B)"
    assert portfolio.bucket_market_cap(1_000) == "Small cap ($300M-$2B)"
    assert portfolio.bucket_market_cap(100) == "Micro cap (<$300M)"
    assert portfolio.bucket_market_cap(None) is None


def test_allocation_by_market_cap_groups_by_bucket():
    portfolio.add_holding("MEGA", 10, 100.0, date(2025, 1, 1))
    portfolio.add_holding("MICRO", 10, 100.0, date(2025, 1, 1))

    def fake_profile(ticker):
        cap = 500_000 if ticker == "MEGA" else 100
        return {"ticker": ticker, "name": ticker, "sector": "Tech", "country": "US", "market_cap": cap, "currency": "USD"}

    with patch("engine.portfolio.finnhub_client.get_quote", side_effect=lambda t: _fake_quote(t, 100.0)):
        with patch("engine.portfolio.finnhub_client.get_company_profile", side_effect=fake_profile):
            allocation = portfolio.get_allocation_by_market_cap()

    labels = {a["label"] for a in allocation}
    assert labels == {"Mega cap (>$200B)", "Micro cap (<$300M)"}


def test_allocation_by_country_uses_display_names_with_fallback():
    portfolio.add_holding("US_STOCK", 10, 100.0, date(2025, 1, 1))
    portfolio.add_holding("XX_STOCK", 10, 100.0, date(2025, 1, 1))

    def fake_profile(ticker):
        country = "US" if ticker == "US_STOCK" else "ZZ"  # unmapped code
        return {"ticker": ticker, "name": ticker, "sector": "Tech", "country": country, "market_cap": 1000, "currency": "USD"}

    with patch("engine.portfolio.finnhub_client.get_quote", side_effect=lambda t: _fake_quote(t, 100.0)):
        with patch("engine.portfolio.finnhub_client.get_company_profile", side_effect=fake_profile):
            allocation = portfolio.get_allocation_by_country()

    labels = {a["label"] for a in allocation}
    assert labels == {"United States", "ZZ"}  # known code mapped, unknown code falls back to raw


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
    # Section 6.10: add_holding() now writes its own matching "buy"
    # transaction, so this is enough on its own — no separate
    # record_transaction() call needed (that would double-count shares).
    portfolio.add_holding("AAPL", 10, 100.0, date(2026, 1, 5))  # a Monday

    fake_bars = [
        {"date": date(2026, 1, 5), "open": 10, "high": 10, "low": 10, "close": 10.0, "volume": 1},
        {"date": date(2026, 1, 6), "open": 10, "high": 10, "low": 10, "close": 12.0, "volume": 1},
    ]
    with patch("engine.price_history.yfinance_client.get_historical_ohlcv", return_value=fake_bars):
        history = portfolio.get_value_history(date(2026, 1, 1), date(2026, 1, 6))

    by_date = {h["date"]: h["value"] for h in history}
    assert by_date[date(2026, 1, 1)] == 0.0      # before the buy transaction
    assert by_date[date(2026, 1, 5)] == 100.0     # 10 shares * $10
    assert by_date[date(2026, 1, 6)] == 120.0     # 10 shares * $12


def test_value_history_falls_back_to_holding_snapshot_without_transactions():
    # Simulates a holding from before Phase 3.5 (Section 6.10): inserted
    # directly via the model, bypassing add_holding()'s automatic "buy"
    # transaction, since add_holding() itself no longer produces this case.
    with get_session() as session:
        session.add(Holding(ticker="AAPL", shares=4, cost_basis=100.0, purchase_date=date(2026, 1, 5)))

    fake_bars = [{"date": date(2026, 1, 5), "open": 10, "high": 10, "low": 10, "close": 25.0, "volume": 1}]
    with patch("engine.price_history.yfinance_client.get_historical_ohlcv", return_value=fake_bars):
        history = portfolio.get_value_history(date(2026, 1, 1), date(2026, 1, 5))

    by_date = {h["date"]: h["value"] for h in history}
    assert by_date[date(2026, 1, 1)] == 0.0       # before purchase_date
    assert by_date[date(2026, 1, 5)] == 100.0      # 4 shares * $25


def test_value_history_empty_when_no_holdings():
    assert portfolio.get_value_history(date(2026, 1, 1), date(2026, 1, 31)) == []


# --------------------------------------------------------------------------
# Value history with cash — selling converts a holding into a flat cash pile
# rather than erasing its history (the Phase 3.5 follow-up fix)
# --------------------------------------------------------------------------

def test_value_history_keeps_sold_position_as_a_flat_cash_pile():
    holding_id = portfolio.add_holding("AAPL", 10, 10.0, date(2026, 1, 5))   # Mon, buy 10 @ $10
    portfolio.sell_holding(holding_id, 10, 12.0, date(2026, 1, 7))            # Wed, sell all 10 @ $12 → $120 cash

    fake_bars = [
        {"date": date(2026, 1, 5), "open": 10, "high": 10, "low": 10, "close": 10.0, "volume": 1},
        {"date": date(2026, 1, 6), "open": 10, "high": 10, "low": 10, "close": 11.0, "volume": 1},
        {"date": date(2026, 1, 7), "open": 10, "high": 10, "low": 10, "close": 12.0, "volume": 1},
        {"date": date(2026, 1, 8), "open": 10, "high": 10, "low": 10, "close": 20.0, "volume": 1},
    ]
    with patch("engine.price_history.yfinance_client.get_historical_ohlcv", return_value=fake_bars):
        history = portfolio.get_value_history(date(2026, 1, 5), date(2026, 1, 8))

    by_date = {h["date"]: h["value"] for h in history}
    assert by_date[date(2026, 1, 5)] == 100.0   # 10 shares * $10, no cash yet
    assert by_date[date(2026, 1, 6)] == 110.0   # 10 shares * $11
    assert by_date[date(2026, 1, 7)] == 120.0   # sold: 0 shares, but $120 now in cash
    assert by_date[date(2026, 1, 8)] == 120.0   # stays flat at $120 even as the price runs to $20


def test_value_history_is_flat_when_everything_is_sold_even_with_no_current_holdings():
    holding_id = portfolio.add_holding("AAPL", 10, 10.0, date(2026, 1, 5))
    portfolio.sell_holding(holding_id, 10, 12.0, date(2026, 1, 6))
    assert portfolio.list_holdings() == []  # nothing currently held

    fake_bars = [
        {"date": date(2026, 1, 5), "open": 10, "high": 10, "low": 10, "close": 10.0, "volume": 1},
        {"date": date(2026, 1, 6), "open": 10, "high": 10, "low": 10, "close": 12.0, "volume": 1},
        {"date": date(2026, 1, 7), "open": 10, "high": 10, "low": 10, "close": 99.0, "volume": 1},
    ]
    with patch("engine.price_history.yfinance_client.get_historical_ohlcv", return_value=fake_bars):
        history = portfolio.get_value_history(date(2026, 1, 5), date(2026, 1, 7))

    by_date = {h["date"]: h["value"] for h in history}
    assert by_date[date(2026, 1, 5)] == 100.0   # still held
    assert by_date[date(2026, 1, 6)] == 120.0   # sold → cash
    assert by_date[date(2026, 1, 7)] == 120.0   # flat, unaffected by the later price


def test_value_history_partial_sell_keeps_remaining_shares_plus_cash():
    holding_id = portfolio.add_holding("AAPL", 10, 10.0, date(2026, 1, 5))
    portfolio.sell_holding(holding_id, 4, 12.0, date(2026, 1, 6))   # sell 4 @ $12 → $48 cash, 6 shares left

    fake_bars = [
        {"date": date(2026, 1, 5), "open": 10, "high": 10, "low": 10, "close": 10.0, "volume": 1},
        {"date": date(2026, 1, 6), "open": 10, "high": 10, "low": 10, "close": 12.0, "volume": 1},
        {"date": date(2026, 1, 7), "open": 10, "high": 10, "low": 10, "close": 20.0, "volume": 1},
    ]
    with patch("engine.price_history.yfinance_client.get_historical_ohlcv", return_value=fake_bars):
        history = portfolio.get_value_history(date(2026, 1, 5), date(2026, 1, 7))

    by_date = {h["date"]: h["value"] for h in history}
    assert by_date[date(2026, 1, 5)] == 100.0           # 10 shares * $10
    assert by_date[date(2026, 1, 6)] == 6 * 12 + 48      # 6 shares * $12 + $48 cash = 120
    assert by_date[date(2026, 1, 7)] == 6 * 20 + 48      # 6 shares * $20 + $48 cash = 168


def test_value_history_includes_manual_wallet_deposit():
    portfolio.add_holding("AAPL", 10, 10.0, date(2026, 1, 5))
    portfolio.deposit_to_wallet(500.0, when=date(2026, 1, 6))

    fake_bars = [
        {"date": date(2026, 1, 5), "open": 10, "high": 10, "low": 10, "close": 10.0, "volume": 1},
        {"date": date(2026, 1, 6), "open": 10, "high": 10, "low": 10, "close": 10.0, "volume": 1},
    ]
    with patch("engine.price_history.yfinance_client.get_historical_ohlcv", return_value=fake_bars):
        history = portfolio.get_value_history(date(2026, 1, 5), date(2026, 1, 6))

    by_date = {h["date"]: h["value"] for h in history}
    assert by_date[date(2026, 1, 5)] == 100.0    # holding only, deposit not yet made
    assert by_date[date(2026, 1, 6)] == 600.0    # holding + $500 deposited


def test_value_history_endpoint_matches_invested_plus_wallet():
    # The last point of the chart should equal the summary's total value
    # (invested holdings + wallet), keeping the two views consistent.
    holding_id = portfolio.add_holding("AAPL", 10, 10.0, date(2026, 1, 5))
    portfolio.sell_holding(holding_id, 4, 15.0, date(2026, 1, 6))   # $60 to wallet, 6 shares left
    portfolio.deposit_to_wallet(100.0, when=date(2026, 1, 6))

    fake_bars = [{"date": date(2026, 1, 6), "open": 20, "high": 20, "low": 20, "close": 20.0, "volume": 1}]
    with patch("engine.price_history.yfinance_client.get_historical_ohlcv", return_value=fake_bars):
        history = portfolio.get_value_history(date(2026, 1, 6), date(2026, 1, 6))
        with patch("engine.portfolio.finnhub_client.get_quote", return_value=_fake_quote("AAPL", 20.0)):
            summary = portfolio.get_portfolio_summary()

    assert history[-1]["value"] == summary["total_value"]  # 6*$20 + ($60 + $100) = 280


def test_value_history_window_starting_after_a_sale_shows_cash_from_the_start():
    holding_id = portfolio.add_holding("AAPL", 10, 10.0, date(2026, 1, 5))
    portfolio.sell_holding(holding_id, 10, 12.0, date(2026, 1, 6))  # sold before the window below

    fake_bars = [{"date": date(2026, 1, 12), "open": 50, "high": 50, "low": 50, "close": 50.0, "volume": 1}]
    with patch("engine.price_history.yfinance_client.get_historical_ohlcv", return_value=fake_bars):
        # window starts a week AFTER the sale - the $120 pile should already be there
        history = portfolio.get_value_history(date(2026, 1, 12), date(2026, 1, 13))

    assert all(point["value"] == 120.0 for point in history)
