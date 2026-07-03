"""
Paper Trading (Section 6.8). Streamlit only — all the Alpaca wiring lives in
engine/paper_trading.py + engine/data_sources/alpaca_client.py. This is the
account summary, positions, order ticket, and order history. Everything is
paper money (Alpaca's paper endpoint); the user submits/cancels, never the app.
"""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pandas as pd
import plotly.express as px
import streamlit as st

from db.session import init_db
from engine import paper_trading, portfolio, watchlist

st.set_page_config(page_title="Paper Trading — Investment Co-Pilot", page_icon="📊", layout="wide")
init_db()

st.title("Paper Trading")
st.caption(
    "Personal, educational tool — not financial advice. This trades **paper money** through Alpaca's paper "
    "account (no real funds), on free, real-time-*ish* IEX data. Orders are simulated; you submit them, the app "
    "never trades on its own."
)

# Flash message set by the previous run's submit/cancel, shown after the rerun.
if "pt_flash" in st.session_state:
    kind, msg = st.session_state.pop("pt_flash")
    {"success": st.success, "error": st.error}.get(kind, st.info)(msg)

if not paper_trading.is_configured():
    st.info(
        "**Alpaca isn't connected yet.** Create a free **paper** account at [alpaca.markets]"
        "(https://alpaca.markets), then add your paper keys to the project's `.env`:\n\n"
        "```\nALPACA_API_KEY=your_key\nALPACA_SECRET_KEY=your_secret\n```\n\n"
        "Reload once they're set — nothing here can touch a real-money account (the client runs paper-only)."
    )
    st.stop()

dashboard = paper_trading.get_dashboard()

if dashboard.errors:
    with st.expander("⚠️ Some Alpaca calls had issues"):
        for err in dashboard.errors:
            st.caption(f"- {err}")

# Market status — the first thing you need to know before wondering why an
# order hasn't filled.
_severity, _status = paper_trading.market_status_text(dashboard.clock)
{"success": st.success, "info": st.info}.get(_severity, st.info)(_status)

# --------------------------------------------------------------------------
# Account summary
# --------------------------------------------------------------------------

account = dashboard.account
if account:
    todays = paper_trading.todays_pl(account)
    unrealized = paper_trading.total_unrealized_pl(dashboard.positions)

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Equity", f"${account['equity']:,.2f}")
    m2.metric("Cash", f"${account['cash']:,.2f}")
    m3.metric("Buying power", f"${account['buying_power']:,.2f}")
    m4.metric(
        "Today's P&L", f"${todays:,.2f}" if todays is not None else "—",
        delta=f"{todays:,.2f}" if todays is not None else None,
    )
    m5.metric(
        "Unrealized P&L", f"${unrealized:,.2f}",
        delta=f"{unrealized:,.2f}" if dashboard.positions else None,
    )
    if account.get("status") and account["status"] != "ACTIVE":
        st.warning(f"Alpaca account status is **{account['status']}** — trading may be restricted.")

st.divider()

# --------------------------------------------------------------------------
# Order ticket — the user fills this in and clicks submit.
# --------------------------------------------------------------------------

st.subheader("Place a paper order")

known = sorted(
    {p["symbol"] for p in dashboard.positions}
    | {h["ticker"] for h in portfolio.list_holdings()}
    | {w["ticker"] for w in watchlist.list_watchlist()}
)

s1, s2 = st.columns([2, 1])
picked = s1.selectbox("Symbol", ["— type a ticker —"] + known, index=0)
custom = s2.text_input("…or custom").strip().upper()
symbol = custom or (picked if picked != "— type a ticker —" else "")

# Price panel — current price, bid/ask, and a recent chart to help size a
# limit order. Fetched only once a symbol is chosen. The last trade also
# prefills the limit-price box below.
suggested_limit = 0.0
if symbol:
    snap = paper_trading.get_price_snapshot(symbol)
    if snap.last is not None:
        suggested_limit = round(snap.last, 2)

    pm1, pm2, pm3 = st.columns(3)
    change_txt = None
    if snap.last is not None and snap.prev_close:
        change_txt = f"{(snap.last / snap.prev_close - 1) * 100:+.2f}% vs prior close"
    pm1.metric(
        f"{symbol} last price", f"${snap.last:,.2f}" if snap.last is not None else "—", delta=change_txt,
        help="Most recent trade (Alpaca IEX, real-time-ish).",
    )
    quote_help = "15-min-delayed consolidated quote (free SIP feed) — the same NBBO Alpaca's platform shows."
    pm2.metric("Bid", f"${snap.bid:,.2f}" if snap.bid else "—", help=quote_help)
    pm3.metric("Ask", f"${snap.ask:,.2f}" if snap.ask else "—", help=quote_help)

    if snap.history:
        hist_df = pd.DataFrame(snap.history)
        fig = px.line(hist_df, x="date", y="close", labels={"date": "", "close": "Close (USD)"})
        fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=240)
        st.plotly_chart(fig, width="stretch", key="pt_price_chart")
    st.caption(
        "Last price is real-time-*ish* (Alpaca IEX). **Bid/ask are the 15-min-delayed consolidated quote** "
        "(free SIP feed — the same NBBO Alpaca's platform shows); the free IEX-only quote is a single venue and "
        "is often wildly wide. The chart is daily closes. Use the bid/ask to guide limit prices — buy near the "
        "ask, sell near the bid."
    )

o1, o2, o3 = st.columns(3)
qty = o1.number_input("Quantity", min_value=0.0, value=1.0, step=1.0)
side = o2.radio("Side", ["Buy", "Sell"], horizontal=True)
order_type = o3.radio("Type", ["Market", "Limit"], horizontal=True)

limit_price = None
extended_hours = False
if order_type == "Limit":
    l1, l2 = st.columns([1, 2])
    limit_price = l1.number_input("Limit price", min_value=0.0, value=suggested_limit, step=0.01)
    extended_hours = l2.checkbox(
        "Extended / overnight hours (24/5)",
        value=False,
        help="Route to Alpaca's pre-market, after-hours, and overnight (24/5) sessions. Only limit day orders "
             "are eligible, symbol availability varies, and overnight liquidity is thinner — so a marketable "
             "limit isn't guaranteed to fill.",
    )

st.caption(
    "Market orders fill at the next available price during regular hours; limit orders only fill at your price "
    "or better. Both are day orders. Fractional quantities work for market orders; use whole shares for limit "
    "and extended-hours orders."
)

if st.button("▶️ Submit paper order", type="primary"):
    try:
        order = paper_trading.place_order(
            symbol, qty, side, order_type=order_type.lower(),
            limit_price=limit_price if order_type == "Limit" else None,
            extended_hours=extended_hours,
        )
        session = " (extended/overnight)" if extended_hours else ""
        st.session_state["pt_flash"] = (
            "success",
            f"Submitted: {order['side']} {order['qty'] or qty} {order['symbol']} "
            f"({order['type']}{session}) — status **{order['status']}**.",
        )
    except paper_trading.PaperTradingError as exc:
        st.session_state["pt_flash"] = ("error", str(exc))
    st.rerun()

st.divider()

# --------------------------------------------------------------------------
# Positions
# --------------------------------------------------------------------------

st.subheader("Open positions")

if dashboard.positions:
    pos_df = pd.DataFrame(
        [
            {
                "Symbol": p["symbol"],
                "Qty": p["qty"],
                "Side": p["side"],
                "Avg entry": p["avg_entry_price"],
                "Current": p["current_price"],
                "Market value": p["market_value"],
                "Unrealized P&L": p["unrealized_pl"],
                "Unrealized %": p["unrealized_plpc"],
            }
            for p in dashboard.positions
        ]
    )
    st.dataframe(
        pos_df.style.format(
            {
                "Qty": "{:g}", "Avg entry": "${:,.2f}", "Current": "${:,.2f}",
                "Market value": "${:,.2f}", "Unrealized P&L": "${:,.2f}", "Unrealized %": "{:+.2f}%",
            },
            na_rep="—",
        ),
        width="stretch", hide_index=True,
    )
else:
    st.caption("No open positions yet — place an order above to get started.")

# --------------------------------------------------------------------------
# Open (working) orders — each cancelable
# --------------------------------------------------------------------------

if dashboard.open_orders:
    st.subheader("Working orders")
    if not (dashboard.clock or {}).get("is_open"):
        st.caption(
            "The market is closed, so these stay **accepted** and won't fill until the next eligible session "
            "(regular hours — or the extended/overnight session for extended-hours orders, if the paper feed "
            "has data for it)."
        )
    for o in dashboard.open_orders:
        c1, c2 = st.columns([5, 1])
        limit_txt = f" @ ${o['limit_price']:,.2f}" if o.get("limit_price") else ""
        ext_txt = " · extended/overnight" if o.get("extended_hours") else ""
        c1.write(
            f"**{o['side']} {o['qty']:g} {o['symbol']}** ({o['type']}{limit_txt}{ext_txt}) — {o['status']}"
        )
        if c2.button("Cancel", key=f"cancel_{o['id']}"):
            try:
                paper_trading.cancel_order(o["id"])
                st.session_state["pt_flash"] = ("success", f"Canceled order for {o['symbol']}.")
            except paper_trading.PaperTradingError as exc:
                st.session_state["pt_flash"] = ("error", str(exc))
            st.rerun()

# --------------------------------------------------------------------------
# Recent order history
# --------------------------------------------------------------------------

st.subheader("Recent orders")

if dashboard.recent_orders:
    hist_df = pd.DataFrame(
        [
            {
                "Submitted": (o["submitted_at"] or "")[:19].replace("T", " "),
                "Symbol": o["symbol"],
                "Side": o["side"],
                "Qty": o["qty"],
                "Type": o["type"],
                "Limit": o["limit_price"],
                "Ext": "✓" if o.get("extended_hours") else "",
                "Status": o["status"],
                "Filled qty": o["filled_qty"],
                "Filled avg": o["filled_avg_price"],
            }
            for o in dashboard.recent_orders
        ]
    )
    st.dataframe(
        hist_df.style.format(
            {"Qty": "{:g}", "Limit": "${:,.2f}", "Filled qty": "{:g}", "Filled avg": "${:,.2f}"},
            na_rep="—",
        ),
        width="stretch", hide_index=True,
    )
else:
    st.caption("No orders yet.")
