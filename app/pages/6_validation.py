"""
Screener Validation. Streamlit only — the point-in-time reconstruction and
walk-forward analysis live in engine/screener_history.py + screener_validation.py;
this file is the form, the verdict, and the score-vs-return chart.
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

from db.session import init_db
from engine import portfolio, screener_validation as validation, watchlist

st.set_page_config(page_title="Screener Validation — Investment Co-Pilot", page_icon="📊", layout="wide")
init_db()

st.title("Screener Validation")
st.caption(
    "Personal, educational tool — not financial advice. This checks whether the Screener's scores "
    "have historically preceded better returns — it is not a prediction of future performance."
)

with st.expander("ℹ️ What this checks (and its limits)", expanded=False):
    st.markdown(
        "The live Screener uses *today's* fundamentals, so it can't be replayed in the past directly. "
        "Instead this **reconstructs** what it would have scored on past dates using only data knowable "
        "then — quarterly fundamentals from **SEC EDGAR** (respecting each filing's date, so no "
        "look-ahead) combined with the historical price — run through the **exact same scoring curves**. "
        "It then pairs each past score with the stock's **actual return over the following months** and "
        "asks: did higher scores tend to precede higher returns?\n\n"
        "**Read it as suggestive, not proof.** It's a single ticker and a small sample; the rigorous "
        "version is cross-sectional across many names. And it currently reconstructs the "
        "fundamentals + momentum core (~75% of the score) — the analyst and news-sentiment factors "
        "aren't reconstructed historically yet."
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
    st.info("Add a holding or watchlist item — or type a US-listed ticker above — to validate the Screener on it.")
    st.stop()

LOOKBACKS = {"2 years": 730, "3 years": 1095, "5 years": 1825}
HORIZONS = {"1 month": 30, "3 months": 91, "6 months": 182}
STEPS = {"Every month": 30, "Every 2 weeks": 14, "Every quarter": 91}

f1, f2, f3 = st.columns(3)
lookback_label = f1.selectbox("Look back", list(LOOKBACKS.keys()), index=0)
horizon_label = f2.selectbox("Forward return horizon", list(HORIZONS.keys()), index=1)
step_label = f3.selectbox("Score", list(STEPS.keys()), index=0)

today = date.today()
start_date = today - timedelta(days=LOOKBACKS[lookback_label])
horizon_days = HORIZONS[horizon_label]
step_days = STEPS[step_label]

if st.button("▶️ Run validation", type="primary"):
    with st.spinner(
        f"Reconstructing point-in-time scores for {ticker} from SEC filings + prices "
        "— the first run for a ticker can take up to a minute…"
    ):
        points = validation.walk_forward(ticker, start_date, today, step_days=step_days, horizon_days=horizon_days)
        st.session_state["validation_result"] = {
            "ticker": ticker, "horizon_days": horizon_days,
            "points": points, "summary": validation.summarize(points),
        }

result = st.session_state.get("validation_result")
if result is None:
    st.stop()

points, summary = result["points"], result["summary"]
if not points:
    st.warning(
        f"Couldn't reconstruct any scored dates for {result['ticker']} in that window. It may not be a "
        "US filer in SEC EDGAR, or there isn't enough filing history yet for this range."
    )
    st.stop()

# --------------------------------------------------------------------------
# Verdict
# --------------------------------------------------------------------------

ic = summary["information_coefficient"]
m1, m2, m3 = st.columns(3)
m1.metric("Observations", summary["n"])
m2.metric(
    "Information coefficient", f"{ic:+.2f}" if ic is not None else "—",
    help="Rank correlation between the score and the subsequent return. Above 0 means higher scores "
         "tended to precede higher returns; near 0 means no relationship; below 0 is the opposite.",
)
m3.metric("Forward horizon", f"{result['horizon_days']} days")

if summary.get("insufficient_data"):
    st.info("Not enough scored dates in this window to draw a conclusion — try a longer look-back.")
elif ic is not None:
    if ic > 0.2:
        verdict = "🟢 **Positive** — for this stock over this window, higher scores tended to precede higher returns."
    elif ic < -0.2:
        verdict = "🔴 **Negative** — higher scores tended to precede *lower* returns here (the opposite of the goal)."
    else:
        verdict = "⚪ **Roughly flat** — no clear relationship between score and subsequent return here."
    st.markdown(verdict + "  \n*Single ticker, small sample — suggestive, not proof.*")

# --------------------------------------------------------------------------
# Average forward return by score band
# --------------------------------------------------------------------------

if summary["bands"]:
    st.subheader("Average forward return by score band")
    bands_df = pd.DataFrame(summary["bands"]).rename(
        columns={"band": "Score band", "n": "Observations", "avg_forward_return_pct": "Avg forward return"}
    )
    st.dataframe(
        bands_df.style.format({"Avg forward return": "{:+.1f}%"}),
        width="stretch", hide_index=True,
    )
    st.caption("If the Screener has signal, the higher bands should show higher average forward returns.")

# --------------------------------------------------------------------------
# Score vs. subsequent return
# --------------------------------------------------------------------------

st.subheader("Score vs. subsequent return")
df = pd.DataFrame(points)
fig = px.scatter(
    df, x="score", y="forward_return_pct", color="recommendation",
    hover_data={"date": True, "score": ":.1f", "forward_return_pct": ":.1f"},
    labels={"score": "Screener score (as of that date)", "forward_return_pct": f"Return over next {result['horizon_days']} days (%)", "recommendation": ""},
)
fig.add_hline(y=0, line_dash="dot", line_color="#888780")
fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), legend=dict(orientation="h", yanchor="top", y=-0.15, x=0))
st.plotly_chart(fig, width="stretch", key="validation_scatter")

with st.expander("All observations"):
    table = df.rename(columns={
        "date": "Date", "score": "Score", "recommendation": "Recommendation", "forward_return_pct": "Forward return %",
    })
    st.dataframe(
        table.style.format({"Score": "{:.1f}", "Forward return %": "{:+.1f}%"}),
        width="stretch", hide_index=True,
    )
