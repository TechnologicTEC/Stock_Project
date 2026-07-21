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
import plotly.graph_objects as go
import streamlit as st

from app import _cache
from app import _theme
from app._auth import gate
from db.session import current_user_id, init_db
from engine import currency, portfolio

st.set_page_config(page_title="Portfolio — Investment Co-Pilot", page_icon="📊", layout="wide")
_theme.apply()
init_db()  # safe to call every run - no-op if the schema's already current
gate("portfolio")  # resolve the signed-in user and scope the DB to them (Phase B)
portfolio.backfill_missing_transactions()  # Section 6.10 - safe/idempotent, cheap to run every load
portfolio.backfill_wallet_cash_flows()     # reconciles pre-dating wallet balances into the cash ledger

_theme.page_header("Holdings", eyebrow="Portfolio")
st.caption(
    "Personal, educational tool — not financial advice. Free-tier data can be delayed or incomplete."
)

# --------------------------------------------------------------------------
# Display currency (USD default; NZD via FRED's USD/NZD rate). Everything is
# stored and priced in USD — this only converts what's *shown*. Amounts you
# enter (cost basis, sale price, deposits) stay in USD.
# --------------------------------------------------------------------------

currency_choice = st.radio(
    "Display currency", currency.SUPPORTED_CURRENCIES, horizontal=True, key="display_currency",
    help="Shows all values in this currency. Data is priced in USD; amounts you enter stay in USD.",
)
try:
    fx_rate = currency.get_rate(currency_choice)
    active_currency = currency_choice
    info = currency.rate_info(currency_choice)
    if info:
        st.caption(f"1 USD = {info['nzd_per_usd']:.4f} {currency_choice} · {info['source']}"
                   + (f", as of {info['as_of']}" if info.get("as_of") else ""))
except Exception:
    if currency_choice != currency.BASE_CURRENCY:
        st.warning(f"Couldn't load the {currency_choice} exchange rate right now — showing USD.")
    active_currency, fx_rate = currency.BASE_CURRENCY, 1.0


def money(amount_usd):
    """Format a USD amount in the currently-selected display currency."""
    return currency.format_amount(amount_usd, active_currency, fx_rate)


def _metric_value_max_rem(longest_value_len: int) -> float:
    """Largest font (rem) to show metric values at, shrinking as the values
    get longer so six/seven-figure or NZD amounts ('NZ$172,642.82') stay
    fully readable instead of getting ellipsis-clipped by st.metric."""
    for max_len, rem in ((9, 1.9), (12, 1.55), (14, 1.3), (16, 1.1)):
        if longest_value_len <= max_len:
            return rem
    return 0.95


def apply_metric_value_sizing(values: list[str]) -> None:
    """Size the metric-value font to the longest value on show and stop it
    truncating. Streamlit's st.metric has no adaptive sizing, so this injects
    a small scoped style. The font is driven by container-query units (a % of
    the metric's own width), with the coefficient sized so the *current*
    longest value fills its column without overflowing — so values shrink as
    the numbers grow and columns narrow, staying fully readable at any width.
    `base_rem` caps it so short values don't balloon on very wide screens."""
    max_len = max((len(v) for v in values), default=1)
    base_rem = _metric_value_max_rem(max_len)
    # ~160/len keeps the longest value inside its column (chars average well
    # under 0.6em wide); clamped so it never gets absurdly large or too small.
    cqi = max(9.0, min(18.0, 160.0 / max_len))
    st.markdown(
        f"""
        <style>
        div[data-testid="stMetric"] {{ container-type: inline-size; }}
        div[data-testid="stMetricValue"],
        div[data-testid="stMetricValue"] > div {{
            white-space: nowrap; overflow: visible; text-overflow: clip;
        }}
        div[data-testid="stMetricValue"] {{
            font-size: {base_rem}rem;                          /* fallback: no container-query support */
            font-size: clamp(0.7rem, {cqi:.1f}cqi, {base_rem}rem);
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def _alloc_df(data):
    """Allocation rows (label/value in USD) with the value converted to the
    display currency, for the pie-chart hovers."""
    df = pd.DataFrame(data)
    df["value"] = df["value"] * fx_rate
    return df


# Event categories drawn on the value-over-time chart, with a legend label
# and colour each (matches the ledger's four action types).
_EVENT_CATEGORIES = {
    "buy": ("Buy", "#22c55e"),        # green
    "sell": ("Sell", "#ef4444"),      # red
    "deposit": ("Deposit", "#3b82f6"),  # blue
    "withdraw": ("Withdraw", "#f59e0b"),  # amber
}


def _event_marker_label(event) -> str:
    """A one-line description of a ledger event for the chart's hover, in the
    display currency — e.g. 'Bought 0.71 ASML @ $1,396.07' or 'Withdrew $50.00'."""
    if event["kind"] == "transaction":
        verb = "Bought" if event["action"] == "Buy" else "Sold"
        return f"{verb} {event['shares']:g} {event['ticker']} @ {money(event['price'])}"
    verb = "Deposited" if event["action"] == "Deposit" else "Withdrew"
    return f"{verb} {money(event['amount'])}"


def event_marker_traces(markers: list[dict]) -> list[go.Scatter]:
    """Turn positioned ledger markers (portfolio.value_history_markers) into
    one Plotly scatter trace per category, so the chart gets coloured dots on
    the line with a category legend and per-event hover text. Events sharing a
    date+category are merged into a single dot whose hover lists them all."""
    grouped: dict[tuple, list[dict]] = {}
    for m in markers:
        grouped.setdefault((m["date"], m["category"]), []).append(m)

    traces = []
    for category, (legend_name, color) in _EVENT_CATEGORIES.items():
        xs, ys, texts = [], [], []
        for (marker_date, cat), items in grouped.items():
            if cat != category:
                continue
            xs.append(marker_date)
            ys.append(items[0]["value"] * fx_rate)  # sits on the (converted) line
            lines = "<br>".join(_event_marker_label(i["event"]) for i in items)
            texts.append(f"<b>{marker_date:%b %d, %Y}</b><br>{lines}")
        if xs:
            traces.append(
                go.Scatter(
                    x=xs, y=ys, mode="markers", name=legend_name, text=texts,
                    marker=dict(size=11, color=color, line=dict(width=1.5, color="#0e1117")),
                    hovertemplate="%{text}<extra></extra>",
                )
            )
    return traces


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
                _cache.clear()
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
            _cache.clear()
            st.rerun()

# --------------------------------------------------------------------------
# Wallet (Section 6.10) — independent of holdings, so it's usable even
# before you've added a single position.
# --------------------------------------------------------------------------

with st.expander("👛 Wallet", expanded=False):
    wallet_balance = portfolio.get_wallet_balance()
    st.metric("Cash balance", money(wallet_balance))

    w1, w2 = st.columns(2)
    with w1:
        with st.form("deposit_form", clear_on_submit=True):
            deposit_amount = st.number_input("Deposit ($)", min_value=0.0, step=10.0, format="%.2f")
            if st.form_submit_button("Deposit"):
                try:
                    portfolio.deposit_to_wallet(deposit_amount)
                    st.success(f"Deposited ${deposit_amount:,.2f}.")
                    _cache.clear()
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
    with w2:
        with st.form("withdraw_form", clear_on_submit=True):
            withdraw_amount = st.number_input("Withdraw ($)", min_value=0.0, step=10.0, format="%.2f")
            if st.form_submit_button("Withdraw"):
                try:
                    portfolio.withdraw_from_wallet(withdraw_amount)
                    st.success(f"Withdrew ${withdraw_amount:,.2f}.")
                    _cache.clear()
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))

holdings = portfolio.list_holdings()

if holdings:
    with st.expander("💵 Sell a holding", expanded=False):
        sell_options = {f"{h['ticker']}  ·  {h['shares']} shares  (id {h['id']})": h for h in holdings}
        sell_choice_label = st.selectbox("Holding to sell", list(sell_options.keys()), key="sell_holding_select")
        sell_choice = sell_options[sell_choice_label]

        default_price = sell_choice["cost_basis"]
        try:
            default_price = portfolio.get_quote_cached(sell_choice["ticker"])["current_price"] or default_price
        except Exception:
            pass  # fine to fall back to cost basis if a live quote isn't available

        sell_all = st.checkbox("Sell all shares", key="sell_all_checkbox")
        with st.form("sell_holding_form", clear_on_submit=True):
            s1, s2, s3 = st.columns(3)
            shares_to_sell = s1.number_input(
                "Shares to sell", min_value=0.0,
                max_value=float(sell_choice["shares"]),
                value=float(sell_choice["shares"]) if sell_all else 0.0,
                step=1.0, format="%.4f",
            )
            sell_price = s2.number_input(
                "Sale price / share ($)", min_value=0.0, value=float(default_price), step=1.0, format="%.2f"
            )
            sell_date = s3.date_input("Sale date", value=date.today(), max_value=date.today())
            sell_submitted = st.form_submit_button("Sell")

        if sell_submitted:
            try:
                result = portfolio.sell_holding(sell_choice["id"], shares_to_sell, sell_price, sell_date)
                st.success(
                    f"Sold {result['shares_sold']} shares of {result['ticker']} for "
                    f"${result['proceeds']:,.2f} — credited to your wallet."
                )
                _cache.clear()
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))

# We keep showing the dashboard (summary + value-over-time) even with no
# *current* holdings, as long as there's history or cash to show — e.g. once
# you've sold everything, your proceeds still sit in the wallet and the chart
# stays meaningful. Only the truly-empty case gets the getting-started note.
has_history = bool(portfolio.list_transactions()) or portfolio.get_wallet_balance() > 0

if not holdings and not has_history:
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

total_value_str = money(summary["total_value"])
holdings_value_str = money(summary["invested_value"])
gain_loss_str = money(summary["total_gain_loss"])
day_change_str = money(summary["total_day_change"])
cost_basis_str = money(summary["total_cost"])
wallet_str = money(summary["wallet_balance"])

# Size the value font to the longest of these so nothing gets ellipsis-clipped
# (worse in NZD / six-figure totals) — see apply_metric_value_sizing.
apply_metric_value_sizing([total_value_str, holdings_value_str, gain_loss_str, day_change_str,
                           cost_basis_str, wallet_str])

# Order groups the story: total, then the two numbers whose difference IS the
# gain/loss (current value vs. what you paid), then the change and cash.
m1, m2, m3, m4, m5, m6 = st.columns(6)
m1.metric("Total value", total_value_str,
          help="Everything you have here: your holdings' current market value plus wallet cash.")
m2.metric("Holdings value", holdings_value_str,
          help="Current market value of your stocks (shares × today's price). Excludes wallet cash — "
               "this is the sum of the Market value column below.")
m3.metric("Cost basis", cost_basis_str,
          help="What you PAID for your current holdings (shares × your average cost/share) — not what "
               "they're worth today. Holdings value minus cost basis is your gain/loss.")
m4.metric(
    "Total gain / loss",
    gain_loss_str,
    f"{summary['total_gain_loss_pct']:.2f}%" if summary["total_gain_loss_pct"] is not None else None,
    help="Holdings value minus cost basis.",
)
m5.metric("Today's change", day_change_str, help="Change in your holdings' value since the previous close.")
m6.metric("Wallet (cash)", wallet_str)

st.divider()

# --------------------------------------------------------------------------
# Value over time
# --------------------------------------------------------------------------

st.subheader("Value over time")
st.caption("Holdings (current and previously sold) plus cash — sold positions live on as a flat cash pile.")

activity = portfolio.list_activity()  # also drives the Transaction history section below

range_choice = st.radio(
    "Range", ["1M", "3M", "6M", "YTD", "1Y", "All"], index=2, horizontal=True, label_visibility="collapsed"
)
show_markers = st.checkbox(
    "Show event markers (buys, sells, deposits, withdrawals)", value=True, key="show_value_markers"
)
today = date.today()
range_starts = {
    "1M": today - timedelta(days=30),
    "3M": today - timedelta(days=90),
    "6M": today - timedelta(days=182),
    "YTD": date(today.year, 1, 1),
    "1Y": today - timedelta(days=365),
}
start_date = range_starts.get(range_choice) or portfolio.earliest_activity_date() or (today - timedelta(days=365))

try:
    with st.spinner("Loading price history..."):
        history = _cache.value_history(current_user_id(), start_date, today)
except Exception as exc:
    history = []
    st.error(f"Couldn't load historical prices right now: {exc}")

if history:
    history_df = pd.DataFrame(history)
    history_df["value"] = history_df["value"] * fx_rate
    fig = px.line(
        history_df, x="date", y="value",
        labels={"date": "", "value": f"Portfolio value ({active_currency})"},
    )
    fig.update_traces(line_color="#2563eb", showlegend=False)  # keep the line out of the legend

    marker_traces = []
    if show_markers:
        markers = portfolio.value_history_markers(activity, history)
        marker_traces = event_marker_traces(markers)
        for trace in marker_traces:
            fig.add_trace(trace)

    fig.update_layout(
        margin=dict(l=0, r=0, t=10, b=10),
        hovermode="x unified",
        showlegend=bool(marker_traces),
        legend=dict(orientation="h", yanchor="top", y=-0.12, xanchor="left", x=0, title_text=""),
    )
    st.plotly_chart(fig, width="stretch", key="value_history_chart")
    if show_markers and not marker_traces:
        st.caption("No buys, sells, deposits, or withdrawals fall within this range yet.")
else:
    st.caption("No historical value to show for this range yet.")

st.divider()

# --------------------------------------------------------------------------
# Transaction / activity history — and undoing mistakes
# --------------------------------------------------------------------------

st.subheader("Transaction history")
st.caption(
    "Every buy, sell, deposit, and withdrawal. Delete an entry to undo a mistake "
    "(wrong stock, wrong amount, ...) — your holdings, wallet, and the chart above are "
    "restored exactly as if it never happened."
)

if not activity:  # fetched once, up by the chart
    st.caption("No activity yet.")
else:
    activity_df = pd.DataFrame(
        [
            {
                "Date": e["date"].isoformat(),
                "Action": e["action"],
                "Ticker": e["ticker"] or "—",
                "Shares": f"{e['shares']:,.4f}" if e["shares"] is not None else "—",
                "Price": money(e["price"]),
                "Amount": money(e["amount"]),
            }
            for e in activity
        ]
    )
    st.dataframe(activity_df, width="stretch", hide_index=True)

    with st.expander("↩️ Undo / delete an entry"):
        def _entry_label(e):
            if e["kind"] == "transaction":
                base = f"{e['date']} · {e['action']} {e['shares']:g} {e['ticker']} @ {money(e['price'])}"
            else:
                base = f"{e['date']} · {e['action']} {money(e['amount'])}"
            return f"{base}   (id {e['kind'][0]}{e['id']})"  # kind-prefixed id keeps labels unique

        entry_options = {_entry_label(e): (e["kind"], e["id"]) for e in activity}
        entry_choice = st.selectbox("Entry to remove", list(entry_options.keys()), key="activity_delete_select")
        if st.button("Delete entry", type="secondary", key="activity_delete_btn"):
            try:
                portfolio.delete_activity(*entry_options[entry_choice])
                st.success("Entry removed — your holdings, wallet, and chart have been restored.")
                _cache.clear()
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))

    with st.expander("⚠️ Reset everything"):
        st.caption(
            "Permanently delete **all** holdings, transactions, deposits/withdrawals, and reset "
            "the wallet to $0. This can't be undone."
        )
        reset_confirmed = st.checkbox("Yes, clear everything.", key="reset_confirm")
        if st.button("Reset portfolio", type="secondary", disabled=not reset_confirmed, key="reset_btn"):
            portfolio.reset_portfolio()
            st.success("Portfolio reset to an empty slate.")
            _cache.clear()
            st.rerun()

if not holdings:
    st.info("You've sold all your positions — your proceeds are sitting in the wallet. Add a holding to start investing again.")
    st.stop()

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
            px.pie(_alloc_df(by_ticker), values="value", names="label", hole=0.35),
            width="stretch", key="alloc_ticker",
        )

with a2:
    st.caption("By asset type")
    by_type = portfolio.get_allocation_by_asset_type()
    if by_type:
        st.plotly_chart(
            px.pie(_alloc_df(by_type), values="value", names="label", hole=0.35),
            width="stretch", key="alloc_asset_type",
        )

with a3:
    st.caption("By sector")
    with st.spinner("Looking up sectors..."):
        by_sector = portfolio.get_allocation_by_sector()
    if by_sector:
        st.plotly_chart(
            px.pie(_alloc_df(by_sector), values="value", names="label", hole=0.35),
            width="stretch", key="alloc_sector",
        )

with a4:
    st.caption("By country")
    with st.spinner("Looking up countries..."):
        by_country = portfolio.get_allocation_by_country()
    if by_country:
        st.plotly_chart(
            px.pie(_alloc_df(by_country), values="value", names="label", hole=0.35),
            width="stretch", key="alloc_country",
        )

with a5:
    st.caption("By market cap")
    with st.spinner("Looking up market caps..."):
        by_market_cap = portfolio.get_allocation_by_market_cap()
    if by_market_cap:
        st.plotly_chart(
            px.pie(_alloc_df(by_market_cap), values="value", names="label", hole=0.35),
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


def _reco_color(text) -> str:
    """Tint the Screener cell by its Buy/Sell lean."""
    if not isinstance(text, str):
        return ""
    if "Buy" in text:
        return "background-color: rgba(34, 197, 94, 0.20)"
    if "Sell" in text:
        return "background-color: rgba(239, 68, 68, 0.20)"
    return ""


rate_holdings = st.checkbox(
    "📊 Rate my holdings with the Screener",
    help="Runs the Investment Screener on each holding to show its Buy/Hold/Sell rating. It fetches "
         "analyst data per ticker, so it's heavier than the rest of the page — off by default. Educational, "
         "not advice.",
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

if rate_holdings:
    with st.spinner("Rating your holdings with the Screener…"):
        ratings = _cache.screener_ratings(tuple(sorted(h["ticker"] for h in holdings)))

    def _rating_label(ticker: str) -> str:
        r = ratings.get(ticker)
        if not r or r.get("score") is None:
            return "—"
        return f"{r['recommendation']} · {r['score']:.0f}"

    table_df["Screener"] = table_df["Ticker"].map(_rating_label)

# Convert the USD money columns into the display currency (% columns are
# unit-invariant, so they're left alone).
money_columns = ["Cost/share", "Price", "Market value"]
for col in money_columns:
    table_df[col] = pd.to_numeric(table_df[col], errors="coerce") * fx_rate

money_fmt = currency.symbol(active_currency) + "{:,.2f}"
styled = table_df.style.map(_pct_color, subset=["Gain/loss %", "Today %"])
if rate_holdings:
    styled = styled.map(_reco_color, subset=["Screener"])
styled = styled.format(
    {
        "Cost/share": money_fmt, "Price": money_fmt, "Market value": money_fmt,
        "Gain/loss %": "{:+.2f}%", "Today %": "{:+.2f}%", "Shares": "{:,.4f}",
    },
    na_rep="—",
)
st.dataframe(styled, width="stretch", hide_index=True)
if rate_holdings:
    st.caption("**Screener** = Buy/Hold/Sell rating · score out of 100. An explainable weighted-factor score "
               "from free data — educational, not financial advice.")

with st.expander("🗑️ Remove a holding"):
    st.caption("Erases the position and its entire transaction history — use this for an entry added by mistake. To undo just one buy/sell, use **Transaction history** above instead.")
    options = {f"{h['ticker']}  ·  {h['shares']} shares": h["ticker"] for h in holdings}
    choice = st.selectbox("Holding", list(options.keys()))
    if st.button("Delete", type="secondary"):
        try:
            portfolio.delete_position(options[choice])
            _cache.clear()
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
