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

o1, o2, o3 = st.columns([2, 1, 1])
picked = o1.selectbox("Symbol", ["— type a ticker —"] + known, index=0)
custom = o2.text_input("…or custom").strip().upper()
symbol = custom or (picked if picked != "— type a ticker —" else "")
qty = o3.number_input("Quantity", min_value=0.0, value=1.0, step=1.0)

o4, o5, o6 = st.columns([1, 1, 1])
side = o4.radio("Side", ["Buy", "Sell"], horizontal=True)
order_type = o5.radio("Type", ["Market", "Limit"], horizontal=True)
limit_price = None
if order_type == "Limit":
    limit_price = o6.number_input("Limit price", min_value=0.0, value=0.0, step=0.01)

st.caption(
    "Market orders fill at the next available price; limit orders only fill at your price or better. Both are "
    "day orders. Fractional quantities work for market orders; use whole shares for limit orders."
)

if st.button("▶️ Submit paper order", type="primary"):
    try:
        order = paper_trading.place_order(
            symbol, qty, side, order_type=order_type.lower(),
            limit_price=limit_price if order_type == "Limit" else None,
        )
        st.session_state["pt_flash"] = (
            "success",
            f"Submitted: {order['side']} {order['qty'] or qty} {order['symbol']} "
            f"({order['type']}) — status **{order['status']}**.",
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
    for o in dashboard.open_orders:
        c1, c2 = st.columns([5, 1])
        limit_txt = f" @ ${o['limit_price']:,.2f}" if o.get("limit_price") else ""
        c1.write(
            f"**{o['side']} {o['qty']:g} {o['symbol']}** ({o['type']}{limit_txt}) — {o['status']}"
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
