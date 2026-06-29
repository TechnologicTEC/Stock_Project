"""
Investment Screener (Section 6.1). Streamlit only — all the scoring logic
lives in engine/screener.py; this file is forms, tables, and charts.
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
from engine import portfolio, screener, watchlist

st.set_page_config(page_title="Screener — Investment Co-Pilot", page_icon="📊", layout="wide")
init_db()

st.title("Screener")
st.caption(
    "Personal, educational tool — not financial advice. Scores are a transparent, "
    "weighted heuristic over free-tier data, not a prediction."
)

# --------------------------------------------------------------------------
# Candidate list management
# --------------------------------------------------------------------------

with st.expander("⭐ Watchlist", expanded=False):
    wl_cols = st.columns([3, 1])
    new_ticker = wl_cols[0].text_input("Add a ticker to your watchlist", key="wl_add_input").strip().upper()
    if wl_cols[1].button("Add", key="wl_add_btn") and new_ticker:
        if watchlist.add_to_watchlist(new_ticker):
            st.success(f"Added {new_ticker} to your watchlist.")
            st.rerun()
        else:
            st.info(f"{new_ticker} is already on your watchlist.")

    wl_items = watchlist.list_watchlist()
    if wl_items:
        for item in wl_items:
            c1, c2 = st.columns([4, 1])
            c1.write(item["ticker"])
            if c2.button("Remove", key=f"wl_remove_{item['ticker']}"):
                watchlist.remove_from_watchlist(item["ticker"])
                st.rerun()
    else:
        st.caption("Nothing on your watchlist yet — add tickers above.")

st.subheader("Choose what to screen")

holdings_tickers = sorted({h["ticker"] for h in portfolio.list_holdings()})
watchlist_tickers = sorted({w["ticker"] for w in watchlist.list_watchlist()})
known_tickers = sorted(set(holdings_tickers) | set(watchlist_tickers))

c1, c2 = st.columns([2, 2])
with c1:
    selected = st.multiselect(
        "From your holdings + watchlist", options=known_tickers, default=known_tickers,
    )
with c2:
    extra_raw = st.text_input("Add other tickers (comma-separated)", placeholder="e.g. NVDA, AMD")
extra = [t.strip().upper() for t in extra_raw.split(",") if t.strip()]

candidate_tickers = sorted(set(selected) | set(extra))

st.caption(
    "Each score is based on fixed, documented thresholds for that metric (e.g. what generally "
    "counts as a cheap P/E or healthy revenue growth) — it doesn't depend on what else you screen "
    "alongside it, and works fine for a single ticker. When you screen more than one together, "
    "you'll also see how each one compares to the others as extra context, since these thresholds "
    "are sector-agnostic rules of thumb, not sector-adjusted fair value — screening similar "
    "businesses together makes that context more useful."
)

if len(candidate_tickers) > 30:
    st.warning(
        f"{len(candidate_tickers)} tickers selected — that's a lot of Finnhub calls per run "
        "(60/min free-tier limit). Consider screening in smaller batches."
    )

run_clicked = st.button("▶️ Run screener", type="primary", disabled=not candidate_tickers)

if run_clicked:
    with st.spinner(f"Screening {len(candidate_tickers)} ticker(s)..."):
        st.session_state["screener_results"] = screener.screen_tickers(candidate_tickers)
        st.session_state["screener_tickers"] = candidate_tickers

for note in screener.known_limitations():
    st.info(note, icon="ℹ️")

results = st.session_state.get("screener_results")

if not results:
    st.info("Pick some tickers above and click **Run screener** to see scores.")
    st.stop()

screened_tickers = st.session_state.get("screener_tickers", [])
st.caption(f"Showing results for: {', '.join(screened_tickers)}")

# --------------------------------------------------------------------------
# Results table + chart
# --------------------------------------------------------------------------

st.divider()
st.subheader("Results")

table_rows = [
    {"Ticker": r.ticker, "Score": r.overall_score, "Recommendation": r.recommendation}
    for r in results
]
results_df = pd.DataFrame(table_rows)

chart_col, table_col = st.columns([3, 2])
with chart_col:
    scored = results_df.dropna(subset=["Score"])
    if not scored.empty:
        fig = px.bar(scored.sort_values("Score"), x="Score", y="Ticker", orientation="h", range_x=[0, 100])
        fig.update_traces(marker_color="#2563eb")
        fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=max(220, 32 * len(scored)))
        st.plotly_chart(fig, width="stretch")
with table_col:
    st.dataframe(
        results_df.style.format({"Score": "{:.1f}"}, na_rep="—"),
        width="stretch", hide_index=True,
    )

any_with_score = any(r.overall_score is not None for r in results)
if any_with_score and st.button("💾 Save today's scores"):
    written = screener.save_results(results)
    st.success(f"Saved {written} score(s) for today.")

errors_present = {r.ticker: r.data_errors for r in results if r.data_errors}
if errors_present:
    with st.expander("⚠️ Data issues during this run"):
        for ticker, errs in errors_present.items():
            st.caption(f"**{ticker}**: " + "; ".join(errs))

# --------------------------------------------------------------------------
# Explainability - the whole point of Section 6.1's design
# --------------------------------------------------------------------------

st.divider()
st.subheader("Why each score? (factor breakdown)")

with st.expander("ℹ️ How the overall score is built", expanded=False):
    weight_rows = [
        {"Factor": screener.FACTOR_LABELS[name], "Nominal weight": f"{weight:.0%}"}
        for name, weight in screener.FACTOR_WEIGHTS.items()
    ]
    st.dataframe(pd.DataFrame(weight_rows), width="stretch", hide_index=True)
    st.caption(
        "Sentiment is part of the design (Section 6.1) but needs the FinBERT news pipeline, which "
        "arrives in Phase 4 — until then it's marked unavailable and its 15% weight is spread "
        "proportionally across the other five factors below, rather than faking a neutral score."
    )
    st.caption(
        "Valuation (P/E, P/B, P/S) and gross margin are scored against thresholds adjusted for the "
        "ticker's detected industry (e.g. software vs. banking get different 'cheap'/'expensive' "
        "ranges) — shown per ticker below. This is a hand-picked approximation, not live market "
        "data; there's no free source for real-time sector medians. Growth, net margin, ROE, and "
        "debt/equity still use one general threshold set for every industry."
    )

for r in results:
    label = f"{r.ticker} — {r.overall_score:.1f} ({r.recommendation})" if r.overall_score is not None else f"{r.ticker} — Insufficient data"
    with st.expander(label):
        valuation_factor = r.factors.get("valuation")
        if valuation_factor is not None:
            bucket = valuation_factor.raw.get("sector_bucket")
            raw_industry = valuation_factor.raw.get("raw_industry")
            if bucket and bucket != screener.DEFAULT_SECTOR_BUCKET:
                st.caption(f"📁 Valuation/margin thresholds use the **{bucket}** peer group (Finnhub industry: *{raw_industry}*)")
            elif raw_industry:
                st.caption(f"📁 Industry **{raw_industry}** didn't match a known peer group — using general thresholds")
            else:
                st.caption("📁 Industry unknown — using general thresholds")
        for name, weight in screener.FACTOR_WEIGHTS.items():
            fr = r.factors.get(name)
            if fr is None:
                continue
            score_str = f"{fr.score:.0f}/100" if fr.score is not None else "n/a"
            st.markdown(f"**{screener.FACTOR_LABELS[name]}** ({weight:.0%} weight) — {score_str}")
            for reason in fr.reasons:
                st.markdown(f"- {reason}")
