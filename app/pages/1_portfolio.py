"""
Portfolio Dashboard (Section 6.3). Streamlit only — all the actual logic
lives in engine/portfolio.py; this file is just forms, charts, and tables.
"""
import sys
from datetime import date, timedelta
from pathlib import Path

# Streamlit only adds the *main script's* folder (app/) to sys.path, not the
# project root - without this, `import engine` / `import db` fail the moment
# this page is opened directly (see README for why this is needed).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pandas as pd
import plotly.express as px
import streamlit as st

from db.session import init_db
from engine import portfolio

st.set_page_config(page_title="Portfolio — Investment Co-Pilot", page_icon="📊", layout="wide")
init_db()  # safe to call every run - no-op if the schema's already current

st.title("Portfolio")
st.caption(
    "Personal, educational tool — not financial advice. Free-tier data can be delayed or incomplete."
)

# --------------------------------------------------------------------------
# Add a holding
# --------------------------------------------------------------------------

with st.expander("➕ Add a holding", expanded=False):
    with st.form("add_holding_form", clear_on_submit=True):
        cols = st.columns([1, 1, 1, 1, 1])
        ticker = cols[0].text_input("Ticker").strip().upper()
        shares = cols[1].number_input("Shares", min_value=0.0, step=1.0, format="%.4f")
        cost_basis = cols[2].number_input("Cost basis / share ($)", min_value=0.0, step=1.0, format="%.2f")
        purchase_date = cols[3].date_input("Purchase date", value=date.today(), max_value=date.today())
        asset_type = cols[4].selectbox("Asset type", sorted(portfolio.VALID_ASSET_TYPES))
        submitted = st.form_submit_button("Add holding")

    if submitted:
        if not ticker:
            st.error("Ticker is required.")
        else:
            try:
                portfolio.add_holding(ticker, shares, cost_basis, purchase_date, asset_type)
                st.success(f"Added {ticker}.")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))

with st.expander("📄 Import holdings from CSV", expanded=False):
    st.caption("Required columns: `ticker, shares, cost_basis, purchase_date`. Optional: `asset_type`.")
    template = "ticker,shares,cost_basis,purchase_date,asset_type\nAAPL,10,150.00,2025-06-01,stock\n"
    st.download_button("Download a template", template, file_name="holdings_template.csv", mime="text/csv")

    uploaded = st.file_uploader("Choose a CSV file", type="csv")
    if uploaded is not None and st.button("Import"):
        result = portfolio.import_holdings_from_csv(uploaded)
        if result.added:
            st.success(f"Added {result.added} holding(s).")
        if result.errors:
            st.warning("Some rows were skipped:\n\n" + "\n".join(f"- {e}" for e in result.errors))
        if result.added:
            st.rerun()

holdings = portfolio.list_holdings()

if not holdings:
    st.info(
        "You haven't added any holdings yet. Use **Add a holding** above to enter one manually, "
        "or **Import holdings from CSV** if you're bringing in a list."
    )
    st.stop()

# --------------------------------------------------------------------------
# Live valuation + summary
# --------------------------------------------------------------------------

with st.spinner("Fetching current prices..."):
    summary = portfolio.get_portfolio_summary()
    valuation = portfolio.get_live_valuation()

if summary["holdings_with_errors"]:
    st.warning(
        "Couldn't fetch a current price for: " + ", ".join(summary["holdings_with_errors"]) + ". "
        "Their values are excluded from the totals below until a price is available."
    )

m1, m2, m3, m4 = st.columns(4)
m1.metric("Total value", f"${summary['total_value']:,.2f}")
m2.metric(
    "Total gain / loss",
    f"${summary['total_gain_loss']:,.2f}",
    f"{summary['total_gain_loss_pct']:.2f}%" if summary["total_gain_loss_pct"] is not None else None,
)
m3.metric("Today's change", f"${summary['total_day_change']:,.2f}")
m4.metric("Cost basis", f"${summary['total_cost']:,.2f}")

st.divider()

# --------------------------------------------------------------------------
# Value over time
# --------------------------------------------------------------------------

st.subheader("Value over time")

range_choice = st.radio(
    "Range", ["1M", "3M", "6M", "YTD", "1Y", "All"], index=2, horizontal=True, label_visibility="collapsed"
)
today = date.today()
range_starts = {
    "1M": today - timedelta(days=30),
    "3M": today - timedelta(days=90),
    "6M": today - timedelta(days=182),
    "YTD": date(today.year, 1, 1),
    "1Y": today - timedelta(days=365),
}
start_date = range_starts.get(range_choice) or portfolio.earliest_holding_date() or (today - timedelta(days=365))

try:
    with st.spinner("Loading price history..."):
        history = portfolio.get_value_history(start_date, today)
except Exception as exc:
    history = []
    st.error(f"Couldn't load historical prices right now: {exc}")

if history:
    history_df = pd.DataFrame(history)
    fig = px.line(history_df, x="date", y="value", labels={"date": "", "value": "Portfolio value ($)"})
    fig.update_traces(line_color="#2563eb")
    fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), hovermode="x unified")
    st.plotly_chart(fig, width="stretch", key="value_history_chart")
else:
    st.caption("No historical value to show for this range yet.")

st.divider()

# --------------------------------------------------------------------------
# Allocation
# --------------------------------------------------------------------------

st.subheader("Allocation")

a1, a2 = st.columns(2)
a3, a4, a5 = st.columns(3)

with a1:
    st.caption("By ticker")
    by_ticker = portfolio.get_allocation_by_ticker()
    if by_ticker:
        st.plotly_chart(
            px.pie(pd.DataFrame(by_ticker), values="value", names="label", hole=0.35),
            width="stretch", key="alloc_ticker",
        )

with a2:
    st.caption("By asset type")
    by_type = portfolio.get_allocation_by_asset_type()
    if by_type:
        st.plotly_chart(
            px.pie(pd.DataFrame(by_type), values="value", names="label", hole=0.35),
            width="stretch", key="alloc_asset_type",
        )

with a3:
    st.caption("By sector")
    with st.spinner("Looking up sectors..."):
        by_sector = portfolio.get_allocation_by_sector()
    if by_sector:
        st.plotly_chart(
            px.pie(pd.DataFrame(by_sector), values="value", names="label", hole=0.35),
            width="stretch", key="alloc_sector",
        )

with a4:
    st.caption("By country")
    with st.spinner("Looking up countries..."):
        by_country = portfolio.get_allocation_by_country()
    if by_country:
        st.plotly_chart(
            px.pie(pd.DataFrame(by_country), values="value", names="label", hole=0.35),
            width="stretch", key="alloc_country",
        )

with a5:
    st.caption("By market cap")
    with st.spinner("Looking up market caps..."):
        by_market_cap = portfolio.get_allocation_by_market_cap()
    if by_market_cap:
        st.plotly_chart(
            px.pie(pd.DataFrame(by_market_cap), values="value", names="label", hole=0.35),
            width="stretch", key="alloc_market_cap",
        )

st.divider()

# --------------------------------------------------------------------------
# Holdings table - the "heat map" look via conditional background color
# --------------------------------------------------------------------------

st.subheader("Holdings")


def _pct_color(value) -> str:
    """Green for gains, red for losses, intensity scaled to magnitude
    (capped at +-5%) - the 'heat map' look from Section 6.3, done as a
    styled table rather than a separate go.Heatmap."""
    if value is None or pd.isna(value):
        return ""
    intensity = min(abs(value) / 5.0, 1.0)
    return f"background-color: rgba(34, 197, 94, {0.12 + 0.35 * intensity})" if value >= 0 else (
        f"background-color: rgba(239, 68, 68, {0.12 + 0.35 * intensity})"
    )


table_df = pd.DataFrame(valuation)[
    ["ticker", "asset_type", "shares", "cost_basis", "current_price", "market_value", "gain_loss_pct", "day_change_pct"]
].rename(
    columns={
        "ticker": "Ticker", "asset_type": "Type", "shares": "Shares", "cost_basis": "Cost/share",
        "current_price": "Price", "market_value": "Market value", "gain_loss_pct": "Gain/loss %",
        "day_change_pct": "Today %",
    }
)

styled = (
    table_df.style.map(_pct_color, subset=["Gain/loss %", "Today %"])
    .format(
        {
            "Cost/share": "${:,.2f}", "Price": "${:,.2f}", "Market value": "${:,.2f}",
            "Gain/loss %": "{:+.2f}%", "Today %": "{:+.2f}%", "Shares": "{:,.4f}",
        },
        na_rep="—",
    )
)
st.dataframe(styled, width="stretch", hide_index=True)

with st.expander("🗑️ Remove a holding"):
    options = {f"{h['ticker']}  ·  {h['shares']} shares  (id {h['id']})": h["id"] for h in holdings}
    choice = st.selectbox("Holding", list(options.keys()))
    if st.button("Delete", type="secondary"):
        portfolio.delete_holding(options[choice])
        st.rerun()
