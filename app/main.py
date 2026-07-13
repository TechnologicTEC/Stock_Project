"""Streamlit entry point + dashboard. Run with: streamlit run app/main.py"""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from datetime import date

import streamlit as st

from app import _cache
from app._auth import gate
from db.session import current_user_id, init_db
from engine import creator_signals, portfolio

_EARNINGS_HOUR = {"bmo": "before open", "amc": "after close"}

st.set_page_config(page_title="Investment Co-Pilot", page_icon="📊", layout="wide")
init_db()
gate("main")  # resolve the signed-in user and scope the DB to them (Phase B)

st.title("📊 Investment Co-Pilot")

st.info(
    "**This is a personal, educational tool — not financial advice.** "
    "It runs on free-tier data, which can be delayed, incomplete, or occasionally "
    "wrong. Don't make real trading decisions based solely on what it shows you.",
    icon="⚠️",
)


def _money(value) -> str:
    return f"${value:,.2f}" if value is not None else "—"


holdings = portfolio.list_holdings()

# --------------------------------------------------------------------------
# Getting started (no holdings yet)
# --------------------------------------------------------------------------
if not holdings:
    st.markdown(
        """
Welcome. Use the pages in the sidebar to get around:

- **Portfolio** — your holdings, valuation, allocation, and per-holding Screener ratings
- **Screener** — explainable weighted-factor stock scoring (Buy → Sell)
- **Health** — concentration, beta, Sharpe ratio, drawdown, and flags
- **News** — headline + earnings-release sentiment, and a cross-signal summary per ticker
- **Creator Signals** — stocks the creators you follow have been discussing
- **Screener Validation** — does the score actually predict returns? (information coefficient)
- **Backtest**, **Paper Trading**, and the **Assistant** chat

To begin, open the **Portfolio** page and add a holding (one at a time, or import a CSV).
        """
    )
    st.stop()

# --------------------------------------------------------------------------
# Dashboard (you have holdings) — light, cheap data only. No heavy screener/
# news runs here; those stay opt-in on their own pages.
# --------------------------------------------------------------------------
uid = current_user_id()
with st.spinner("Loading your dashboard…"):
    summary = _cache.portfolio_summary(uid)
    valuation = _cache.live_valuation(uid)

s1, s2, s3 = st.columns(3)
s1.metric("Total value", _money(summary["total_value"]))
s2.metric(
    "Total gain / loss", _money(summary["total_gain_loss"]),
    f"{summary['total_gain_loss_pct']:.2f}%" if summary["total_gain_loss_pct"] is not None else None,
)
s3.metric("Today's change", _money(summary["total_day_change"]))

left, right = st.columns(2)

# ---- Today's movers -------------------------------------------------------
with left:
    st.subheader("Today's movers")
    movers = [v for v in valuation if v.get("day_change_pct") is not None]
    if not movers:
        st.caption("No live price changes to show right now (market closed or prices unavailable).")
    else:
        ranked = sorted(movers, key=lambda v: v["day_change_pct"])
        worst, best = ranked[0], ranked[-1]
        c1, c2 = st.columns(2)
        c1.metric(f"🔻 {worst['ticker']}", f"{worst['day_change_pct']:+.2f}%",
                  _money(worst.get("day_change_value")), delta_color="inverse")
        if best is not worst:
            c2.metric(f"🔺 {best['ticker']}", f"{best['day_change_pct']:+.2f}%",
                      _money(best.get("day_change_value")))
        st.caption("Biggest drag and lift in your holdings today. See **Portfolio** for the full table.")

# ---- Creator signals ------------------------------------------------------
with right:
    st.subheader("🔁 Creator signals")
    board = creator_signals.mention_leaderboard()   # cheap DB read; ≥2 mentions, last 3 months
    if not board:
        st.caption("Nothing a creator has repeated yet. See **Creator Signals** to add channels.")
    else:
        for entry in board[:4]:
            seen = entry["last_seen"].strftime("%b %d") if entry["last_seen"] else ""
            st.write(f"**{entry['ticker']}** — mentioned **{entry['mentions']}×**"
                     + (f" · last {seen}" if seen else ""))
        st.caption("Stocks your creators keep coming back to — repetition is attention, not advice.")

# ---- Reporting soon -------------------------------------------------------
upcoming = _cache.upcoming_earnings(tuple(sorted(h["ticker"] for h in holdings)))
if upcoming:
    st.subheader("📅 Reporting soon")
    for e in upcoming:
        d = e["days_until"]
        rel = "**today**" if d == 0 else "**tomorrow**" if d == 1 else f"in **{d} days**"
        when = date.fromisoformat(e["date"]).strftime("%b %d")
        hour = _EARNINGS_HOUR.get(e.get("hour"), "")
        est = f" · est. EPS ${e['eps_estimate']:.2f}" if e.get("eps_estimate") is not None else ""
        st.write(f"**{e['ticker']}** reports {rel} — {when}{', ' + hour if hour else ''}{est}")
    st.caption("Upcoming earnings for your holdings (next 3 weeks). Estimates only — Finnhub's free tier "
               "withholds the actual beat/miss.")

st.divider()
st.caption("This is a personal, educational tool — not financial advice.")
