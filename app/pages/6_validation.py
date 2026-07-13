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
import plotly.graph_objects as go
import streamlit as st

from app._auth import gate
from db.session import init_db
from engine import portfolio, projections, screener_validation as validation, watchlist

st.set_page_config(page_title="Screener Validation — Investment Co-Pilot", page_icon="📊", layout="wide")
init_db()
gate("validation")  # restricted: guests are stopped here (Phase B)

st.title("Screener Validation")
st.caption(
    "Personal, educational tool — not financial advice. This checks whether the Screener's scores "
    "have historically preceded better returns — it is not a prediction of future performance."
)

with st.expander("ℹ️ What this checks (and its limits)", expanded=False):
    st.markdown(
        "The live Screener uses *today's* fundamentals, so it can't be replayed in the past directly. "
        "Instead this **reconstructs** what it would have scored on past dates using only data knowable "
        "then, run through the **exact same scoring curves**, then pairs each past score with the stock's "
        "**actual return over the following months** and asks: did higher scores tend to precede higher "
        "returns?\n\n"
        "All six factors are reconstructed point-in-time: **fundamentals** (P/E, margins, growth, …) from "
        "**SEC EDGAR**, respecting each filing's date so there's no look-ahead; **momentum** from the "
        "historical price; **analyst** consensus approximated from the dated stream of rating changes; and "
        "**news sentiment** from **GDELT** article tone over the prior 30 days.\n\n"
        "**Read it as suggestive, not proof.** It's a single ticker and a small sample (the rigorous "
        "version is cross-sectional across many names); the analyst factor is an *approximation* of "
        "consensus from change events, and the sentiment factor is GDELT's own tone rather than FinBERT. "
        "The **information coefficient** below is a rank correlation from **−1 to +1** — above 0 means "
        "higher scores tended to precede higher returns; 0 means no relationship."
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

include_news = st.checkbox(
    "Include news sentiment (queries GDELT on BigQuery — slower, uses your free quota)",
    value=False,
    help="Off: a fast, quota-free run on 5 factors (fundamentals, momentum, analyst). On: adds the 6th "
         "factor — GDELT news tone — which scans ~2 GB of BigQuery per month of look-back and is cached "
         "after the first run for a ticker.",
)

today = date.today()
start_date = today - timedelta(days=LOOKBACKS[lookback_label])
horizon_days = HORIZONS[horizon_label]
step_days = STEPS[step_label]

if st.button("▶️ Run validation", type="primary"):
    sources = "SEC filings, prices, analyst ratings" + (", and GDELT news" if include_news else "")
    with st.spinner(
        f"Reconstructing point-in-time scores for {ticker} from {sources} — the first run for a ticker "
        "can take a couple of minutes (cached afterwards)…"
    ):
        points = validation.walk_forward(
            ticker, start_date, today, step_days=step_days, horizon_days=horizon_days, include_news=include_news
        )
        summary = validation.summarize(points)
        # Remember the IC so the Health page's projection can use it as the
        # confidence behind its optional median tilt (no need to re-run this).
        if summary.get("information_coefficient") is not None:
            projections.remember_validation_ic(
                ticker, summary["information_coefficient"], n=summary.get("n"),
                horizon_days=horizon_days, include_news=include_news,
            )
        st.session_state["validation_result"] = {
            "ticker": ticker, "horizon_days": horizon_days, "include_news": include_news,
            "points": points, "summary": summary,
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
    help="Spearman rank correlation between the score and the subsequent return, from −1 to +1. "
         "Above 0 means higher scores tended to precede higher returns; near 0 means no relationship; "
         "below 0 is the opposite. Real single-name ICs are small — consistently above ~+0.05 is notable.",
)
m3.metric("Forward horizon", f"{result['horizon_days']} days")

# What the IC actually covers (#7): the reconstructed score reuses the live
# scorers point-in-time for every factor except news sentiment.
if result.get("include_news"):
    st.caption("This validates the point-in-time score **including** a news-sentiment factor rebuilt from "
               "GDELT tone — which isn't the same signal as the live FinBERT-headline sentiment, so it's an "
               "approximation of the live score, not an exact match.")
else:
    st.caption("This validates the point-in-time score **excluding news sentiment** (its 15% weight is "
               "redistributed). The live Screener recommendation does weight current news sentiment, so the "
               "IC reflects the fundamentals/momentum core rather than the exact live score.")

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

trend = summary.get("trend")
if trend:
    fig.add_trace(go.Scatter(
        x=[trend["x0"], trend["x1"]], y=[trend["y0"], trend["y1"]],
        mode="lines", name=f"Trend {trend['slope']:+.2f}%/pt",
        line=dict(color="#444444", width=2), hoverinfo="skip",
    ))

fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), legend=dict(orientation="h", yanchor="top", y=-0.15, x=0))
st.plotly_chart(fig, width="stretch", key="validation_scatter")

if trend:
    r_txt = f"{trend['pearson_r']:+.2f}" if trend["pearson_r"] is not None else "—"
    st.caption(
        f"The trend line is a least-squares fit: **{trend['slope']:+.2f}%** of forward return per score point "
        f"(Pearson r = {r_txt} on the raw values). The information coefficient above is a **rank** correlation, "
        "so it shrugs off a single outlier that can swing this line — read them as agreeing on *direction*, "
        "not on magnitude."
    )
elif not summary.get("insufficient_data"):
    st.caption("No trend line: the scores in this window don't vary enough to fit one.")

# --------------------------------------------------------------------------
# Per-observation factor breakdown — shows every factor (news included)
# actually feeding each score, not just the final number.
# --------------------------------------------------------------------------

st.subheader("Factor breakdown per observation")
st.caption(
    "Each score is the weighted blend of these factors (a factor with no data has its weight "
    "redistributed). **News** is GDELT article tone over the 30 days before each date — populated only "
    "when *Include news sentiment* is ticked (GDELT provides tone, not the individual headlines)."
)
_FACTOR_COLUMNS = [
    ("valuation", "Valuation"), ("growth", "Growth"), ("profitability", "Profitability"),
    ("momentum", "Momentum"), ("analyst_confidence", "Analyst"), ("sentiment", "News"),
]
breakdown_rows = []
for p in points:
    factors = p.get("factors") or {}
    row = {"Date": p["date"], "Score": p["score"]}
    for key, label in _FACTOR_COLUMNS:
        row[label] = factors.get(key)
    breakdown_rows.append(row)

breakdown_df = pd.DataFrame(breakdown_rows)
factor_fmt = {label: "{:.0f}" for _, label in _FACTOR_COLUMNS}
st.dataframe(
    breakdown_df.style.format({"Score": "{:.1f}", **factor_fmt}, na_rep="—"),
    width="stretch", hide_index=True,
)
