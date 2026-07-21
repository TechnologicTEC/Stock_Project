"""
Backtesting (Section 6.7). Streamlit only — the engine (vectorized strategy
simulation, metrics, persistence) lives in engine/backtest.py; this is the
form, the comparison table, and the equity-curve chart.
"""
import sys
from datetime import date, timedelta
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pandas as pd
import plotly.express as px
import streamlit as st

from app import _theme
from app._auth import gate
from db.session import init_db
from engine import backtest, portfolio, watchlist

st.set_page_config(page_title="Backtest — Investment Co-Pilot", page_icon="📊", layout="wide")
_theme.apply()
init_db()
gate("backtest")  # guest-accessible (Phase B) — sets the current user scope

_theme.page_header("Backtesting", eyebrow="Execution")
st.caption(
    "Personal, educational tool — not financial advice. Backtests use free-tier price history, "
    "assume no trading costs or slippage, and — as always — past performance doesn't predict the future."
)

with st.expander("ℹ️ What this does (and honestly can't do)", expanded=False):
    st.markdown(
        "This backtests **technical** strategies — rules computed purely from price history "
        "(moving averages, RSI, momentum) — with **no look-ahead**: a signal built from prices up to "
        "day *t* is only acted on at day *t+1*. Each run is compared to simply **buying and holding** "
        "the same ticker, and to holding **SPY**.\n\n"
        "It deliberately does **not** backtest the fundamental Screener. That scorer uses *today's* "
        "P/E, margins, and analyst data, and free-tier APIs have no point-in-time history for those — "
        "replaying it in the past would secretly use information you couldn't have known then "
        "(look-ahead bias), making the result meaningless. The honest path to validating the Screener "
        "is the `screener_scores` table: keep saving daily scores, and over time they can be checked "
        "against what actually happened next."
    )

# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------

known = sorted({h["ticker"] for h in portfolio.list_holdings()} | {w["ticker"] for w in watchlist.list_watchlist()})

c1, c2 = st.columns([2, 1])
picked = c1.selectbox("Ticker", known, index=0) if known else None
custom = c2.text_input("…or a custom ticker").strip().upper()
ticker = custom or picked

if not ticker:
    st.info("Add a holding or watchlist item — or type a ticker above — to backtest a strategy on it.")
    st.stop()

f1, f2, f3 = st.columns([2, 1, 1])
strategy_label = f1.selectbox("Strategy", [label for _, (label, _) in backtest.STRATEGIES.items()])
strategy_key = next(k for k, (label, _) in backtest.STRATEGIES.items() if label == strategy_label)

LOOKBACKS = {"1Y": 365, "2Y": 730, "3Y": 1095, "5Y": 1825}
lookback_label = f2.selectbox("Period", list(LOOKBACKS.keys()), index=1)
starting_capital = f3.number_input("Starting capital ($)", min_value=100.0, value=10_000.0, step=1000.0, format="%.0f")

today = date.today()
start_date = today - timedelta(days=LOOKBACKS[lookback_label])

run = st.button("▶️ Run backtest", type="primary")

# Persist the result in session_state so it survives later interactions (e.g.
# clicking "Save this run") — a button is True only on the run it's clicked.
if run:
    with st.spinner(f"Backtesting {strategy_label} on {ticker}…"):
        st.session_state["backtest_result"] = backtest.run_backtest(
            ticker, strategy_key, start_date, today, starting_capital=starting_capital
        )

result = st.session_state.get("backtest_result")


def _pct(v):
    return f"{v:+.1f}%" if v is not None else "—"


def _num(v):
    return f"{v:.2f}" if v is not None else "—"


def _row(name, m):
    if m is None:
        return {"": name, "Total return": "—", "Annualized": "—", "Sharpe": "—", "Max drawdown": "—", "Volatility": "—"}
    return {
        "": name,
        "Total return": _pct(m.total_return_pct),
        "Annualized": _pct(m.annualized_return_pct),
        "Sharpe": _num(m.sharpe),
        "Max drawdown": _pct(m.max_drawdown_pct),
        "Volatility": _pct(m.volatility_pct),
    }


if result is not None and result.error:
    st.warning(result.error)

if result is not None and not result.error:
    for note in result.notes:
        st.info(note, icon="💡")

    strat_end = result.strategy.total_return_pct if result.strategy else None
    bh_end = result.buy_hold.total_return_pct if result.buy_hold else None
    spy_end = result.spy.total_return_pct if result.spy else None

    m1, m2, m3 = st.columns(3)
    m1.metric(f"{result.strategy_label}", _pct(strat_end))
    m2.metric(f"{result.ticker} buy & hold", _pct(bh_end),
              f"{strat_end - bh_end:+.1f} pts vs strategy" if strat_end is not None and bh_end is not None else None)
    m3.metric("SPY buy & hold", _pct(spy_end),
              f"{strat_end - spy_end:+.1f} pts vs strategy" if strat_end is not None and spy_end is not None else None)

    table = pd.DataFrame([
        _row(result.strategy_label, result.strategy),
        _row(f"{result.ticker} buy & hold", result.buy_hold),
        _row("SPY buy & hold", result.spy),
    ])
    st.dataframe(table, width="stretch", hide_index=True)
    st.caption(
        f"{result.trades} strategy trade(s) over the window · risk-free rate for Sharpe: "
        f"{result.risk_free_rate_source}. Returns are hypothetical, in USD, and ignore trading costs."
    )

    st.subheader("Growth of your starting capital")
    eq_df = pd.DataFrame(result.equity_curve)
    if not eq_df.empty:
        label_map = {"strategy": result.strategy_label, "buy_hold": f"{result.ticker} buy & hold", "spy": "SPY"}
        long = eq_df.melt(id_vars="date", value_vars=["strategy", "buy_hold", "spy"],
                          var_name="series", value_name="value").dropna(subset=["value"])
        long["series"] = long["series"].map(label_map)
        fig = px.line(long, x="date", y="value", color="series",
                      labels={"value": "Portfolio value ($)", "date": "", "series": ""})
        fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), hovermode="x unified",
                          legend=dict(orientation="h", yanchor="top", y=-0.12, x=0))
        st.plotly_chart(fig, width="stretch", key="backtest_equity_chart", theme=None)

    if st.button("💾 Save this run"):
        backtest.save_backtest_run(result)
        st.success("Saved.")
        st.rerun()

# --------------------------------------------------------------------------
# Saved runs — always shown
# --------------------------------------------------------------------------

saved = backtest.list_backtest_runs()
if saved:
    st.subheader("Saved runs")
    saved_df = pd.DataFrame([
        {
            "Ticker": r["ticker"], "Strategy": r["strategy_label"],
            "From": r["start_date"], "To": r["end_date"],
            "Strategy return": _pct(r["strategy_return_pct"]),
            "SPY return": _pct(r["spy_return_pct"]),
            "Sharpe": _num(r["strategy_sharpe"]),
        }
        for r in saved
    ])
    st.dataframe(saved_df, width="stretch", hide_index=True)
