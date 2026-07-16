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

# The news factor is the ONLY one that needs a credential this environment might not
# have (Google Cloud, for BigQuery). Without it every GDELT call fails, the factor
# scores None, and the run finishes with "Sentiment — 0 observations" and no reason.
# Say it up front instead of letting a long run end in a silent blank.
if include_news and not validation.news_sentiment_available():
    st.warning(
        "**News sentiment can't be reconstructed in this environment — it will come back blank "
        "(0 observations).** The historical news factor is GDELT article tone, queried through Google "
        "BigQuery, and no Google Cloud credentials are configured here. Every other factor is unaffected, "
        "so the run is still valid — just on 5 factors, exactly as if this box were unticked.\n\n"
        "It works wherever you've run `gcloud auth application-default login` and set `GOOGLE_CLOUD_PROJECT` "
        "(your local machine). To enable it on the deployed app you'd need to add a Google Cloud "
        "service-account key as a Space secret.",
        icon="⚠️",
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

# --------------------------------------------------------------------------
# Cross-sectional validation on a NEUTRAL universe. This is the one that can
# actually justify reweighting: a few hundred names you didn't pick, ranked
# against each other on each date. Produced by scripts/validate_universe.py (a
# batch job — thousands of reconstructions outlive a browser connection), so the
# page only ever reads the stored result.
# --------------------------------------------------------------------------
st.divider()
with st.expander("🌍 Validate across the S&P 500 — the read that could justify reweighting", expanded=False):
    uni = validation.load_universe_result()
    if not uni:
        st.info(
            "No universe run stored yet. Run the **Validate screener across the S&P 500** GitHub Action "
            "(or `python scripts/validate_universe.py` locally), and the result appears here.\n\n"
            "Why bother: the run above measures the Screener on the ~10 stocks *you already picked* — a "
            "biased sample, and with overlapping return windows it yields only ~50 independent observations, "
            "so every factor's error bar swallows its score. A few hundred names you didn't choose is what "
            "makes the answer mean something."
        )
    else:
        o = uni["overall"]
        st.caption(
            f"**{uni.get('universe', 'sp500').upper()}** · {uni['n_tickers']} tickers · {uni['n_points']} "
            f"reconstructions · horizon {uni['horizon_days']}d, step {uni['step_days']}d · "
            f"run {uni.get('generated_at', '—')}"
        )
        st.markdown(
            "Unlike the pooled run above, this ranks names **against each other on each date** — the only "
            "thing the Screener actually claims (\"this stock is better than that one\"), rather than mixing "
            "it with \"is now a good time to own stocks\", which it can't know."
        )

        u1, u2, u3, u4 = st.columns(4)
        u1.metric("Cross-sectional IC", f"{o['mean_ic']:+.3f}" if o["mean_ic"] is not None else "—",
                  help="Average per-date rank correlation between score and the following return, across names.")
        u2.metric("t-stat", f"{o['t_stat']:+.2f}" if o["t_stat"] is not None else "—",
                  help="Deflated for overlapping return windows. |t| > 1.96 = distinguishable from zero.")
        u3.metric("Independent dates", f"{o['n_dates_eff']} of {o['n_dates']}",
                  help="Sampling a return more often than its own horizon re-measures the same move, so "
                       "the dates sampled are NOT all independent trials.")
        u4.metric("Hit rate", f"{o['hit_rate']:.0%}" if o["hit_rate"] is not None else "—",
                  help="Share of dates where the IC was positive. 50% is a coin flip.")

        if not o.get("significant"):
            st.warning(
                "**Still not statistically significant.** The binding constraint is *independent time "
                f"periods*, not tickers: {o['n_dates']} sampled dates collapse to **{o['n_dates_eff']}** "
                "once overlapping windows are removed. More names sharpen each date's estimate, but only a "
                "longer look-back (or a shorter horizon) buys more independent periods. Not a reweighting "
                "mandate yet.",
                icon="📉",
            )

        def _verdict(f):
            if f["significant"]:
                return "yes"
            return "no (marginal)" if f.get("significant_uncorrected") else "no"

        rows = [{"Factor": f["label"], "Mean IC": f["mean_ic"], "t-stat": f["t_stat"],
                 "IC-IR": f["ic_ir"], "Hit rate": f["hit_rate"],
                 "Distinguishable from zero?": _verdict(f),
                 "Dates": f["n_dates"]}
                for f in uni["factor_ic"].values()]
        uni_df = pd.DataFrame(rows).sort_values("Mean IC", ascending=False, na_position="last")
        st.dataframe(
            uni_df.style.format({"Mean IC": "{:+.3f}", "t-stat": "{:+.2f}",
                                 "IC-IR": "{:+.2f}", "Hit rate": "{:.0%}"}, na_rep="—"),
            width="stretch", hide_index=True,
        )
        thr, n_tests = uni.get("t_threshold"), uni.get("n_tests")
        if thr:
            st.caption(
                f"⚖️ The bar here is **|t| > {thr}**, not the familiar 1.96, because we test "
                f"**{n_tests} factors against the same data at once**. With 6 factors there's a ~26% chance "
                "at least one clears 1.96 by pure luck — so reporting whichever one does would be p-hacking "
                "by accident. Rows marked *“no (marginal)”* passed 1.96 but not this bar: that's exactly what "
                "a false positive looks like."
            )
        st.caption(
            "**IC-IR** is consistency (mean ÷ spread): a small IC that shows up every date beats a big one "
            "that flips sign. **Read against the pooled run above — where a factor's sign flips between the "
            "two, the personal-holdings number is the one to distrust**, because those are stocks you chose.\n\n"
            "Caveats, honestly: the universe is **today's** index, so failed companies are missing "
            "(survivorship bias — it inflates return *levels*, but hits every factor alike, which is why "
            "comparing factors is still fair). It covers one macro regime. And if the run came from the "
            "GitHub Action, **analyst confidence will be thin** — it reconstructs from Yahoo, which blocks "
            "datacenter IPs; run the script locally for full coverage."
        )

# --------------------------------------------------------------------------
# Pooled, per-factor validation (review #8) — which factors actually predict,
# across your whole universe. This is the honest basis for reweighting.
# --------------------------------------------------------------------------
st.divider()
with st.expander("📊 Validate across ALL your tickers — which factors predict? (per-factor IC)"):
    st.caption(
        "Pools the walk-forward across your holdings + watchlist and measures the information coefficient "
        "**per factor** — i.e. which factors' scores have tracked subsequent returns for *your* tickers. "
        "It reuses the look-back / horizon / news settings above.\n\n"
        "**This is not a reweighting basis, despite how it looks.** It measures the Screener on the stocks "
        "you already picked (a biased sample), and ~10 names with overlapping return windows leave so few "
        "independent observations that every factor's error bar swallows its score. Use the **S&P 500 "
        "section above** for that question; treat this as a description of your own book."
    )
    pooled_key = validation.pooled_cache_key(
        known, lookback_days=LOOKBACKS[lookback_label], horizon_days=horizon_days,
        step_days=step_days, include_news=include_news,
    )

    if include_news:
        st.warning(
            "**News sentiment is on.** The first pooled run has to pull GDELT tone from BigQuery for every "
            "ticker — expect a few minutes and a chunk of your free BigQuery quota. It's cached per company "
            "per year, so re-runs (and other tickers sharing the window) are fast and free.",
            icon="⏳",
        )

    if st.button("▶️ Run pooled validation", key="run_pooled"):
        progress = st.progress(0.0, text="Starting…")

        def _on_progress(done, total, tk):
            progress.progress(done / total, text=f"Validating {tk} — {done}/{total}")

        with st.spinner("Reconstructing point-in-time scores for each ticker…"):
            pooled_points = validation.pooled_walk_forward(
                known, start_date, today, step_days=step_days, horizon_days=horizon_days,
                include_news=include_news, on_progress=_on_progress,
            )
        progress.empty()
        pooled_summary = validation.summarize_pooled(pooled_points, horizon_days=horizon_days)
        # Persist OUTSIDE session_state: a long run can outlive the websocket, and
        # the reconnected browser gets a FRESH session with empty session_state —
        # which silently threw away the finished result. The stored copy survives.
        validation.save_pooled_result(pooled_key, pooled_summary)
        # Tag the in-session copy with the exact settings key it was computed for.
        # Without this, a result from earlier settings (or an interrupted run) got
        # redisplayed after you changed the look-back / ticked news — showing a
        # stale "not enough" that no longer matched the controls above it.
        st.session_state["pooled_result"] = {
            "summary": pooled_summary, "horizon_days": horizon_days, "key": pooled_key,
        }

    pooled = st.session_state.get("pooled_result")
    if pooled is not None and pooled.get("key") != pooled_key:
        pooled = None                        # session result is for different settings — ignore it
    if pooled is None:                       # session dropped mid-run? show the stored run for THESE settings.
        stored = validation.load_pooled_result(pooled_key)
        if stored:
            pooled = {"summary": stored, "horizon_days": horizon_days, "from_store": True}

    if pooled:
        if pooled.get("from_store"):
            st.caption("Showing the last completed run for these settings (restored after the page reloaded).")
        s = pooled["summary"]
        if not s.get("n_tickers"):
            # Zero tickers reconstructed at all — with a real universe this means the
            # run didn't complete (usually a long first news run that outlived the
            # connection), not that history is too short. Say so, and how to fix it.
            st.info(
                "No reconstructed history came back for this run. If it was a first-time run **with news "
                "sentiment on**, the GDELT pull can outlast the page connection — the tone is now cached, so "
                "click **Run pooled validation** again and it'll complete quickly."
            )
        elif s.get("insufficient_data"):
            st.info(f"Only {s['n_tickers']} ticker(s) reconstructed and too few dated points to summarise — "
                    "try a longer look-back or a shorter forward horizon.")
        else:
            pooled_ic = s["information_coefficient"]
            ci95, n_eff = s.get("ci95"), s.get("n_eff")
            p1, p2, p3, p4 = st.columns(4)
            p1.metric("Tickers pooled", s["n_tickers"])
            p2.metric("Observations", s["n"],
                      help="Raw count. See 'Independent observations' — overlapping return windows mean "
                           "these are NOT that many separate bets.")
            p3.metric("Independent observations", n_eff if n_eff else "—",
                      help=f"Effective sample after de-duplicating overlapping {pooled['horizon_days']}-day "
                           "return windows and correlated tickers. This is what the error bars are built on.")
            p4.metric("Pooled overall IC",
                      f"{pooled_ic:+.3f}" if pooled_ic is not None else "—",
                      delta=f"± {ci95:.3f} (95%)" if ci95 is not None else None,
                      delta_color="off")

            # The headline claim this page has to be honest about: with a handful of
            # tickers and overlapping windows the interval swallows every factor, so
            # a positive-looking IC is not evidence of signal.
            if pooled_ic is not None and ci95 is not None and not s.get("significant"):
                st.warning(
                    f"**Not statistically significant.** With {n_eff} independent observations the 95% interval "
                    f"is ±{ci95:.3f}, so a pooled IC of {pooled_ic:+.3f} can't be told apart from zero (no "
                    "signal). Treat everything below as a hypothesis, **not** a basis for reweighting — that "
                    "needs a broader, neutral universe of tickers, not your own holdings.",
                    icon="📉",
                )

            factor_rows = [
                {"Factor": v["label"], "IC": v["ic"],
                 "95% interval": (f"±{v['ci95']:.3f}" if v.get("ci95") is not None else "—"),
                 "Distinguishable from zero?": ("yes" if v.get("significant") else "no"),
                 "Observations": v["n"], "Independent": v.get("n_eff")}
                for v in s["factor_ic"].values()
            ]
            factor_df = pd.DataFrame(factor_rows).sort_values("IC", ascending=False, na_position="last")
            st.dataframe(
                factor_df.style.format({"IC": "{:+.3f}"}, na_rep="—"),
                width="stretch", hide_index=True,
            )
            rho = s.get("avg_ticker_correlation")
            st.caption(
                f"Forward horizon {pooled['horizon_days']} days. A higher IC means that factor's score tracked "
                "returns better — **but only read a row whose interval excludes zero.** The interval is built on "
                "*independent* observations: a forward return sampled more often than its own horizon re-measures "
                "the same price move, so the raw count overstates the evidence"
                + (f" (your tickers' returns correlate {rho:+.2f} with each other)." if rho is not None else ".")
                + " **Not an auto-fit, and not yet a reweighting signal** — it's a pooled (not per-date "
                "cross-sectional) read on the stocks you already picked, which is a biased sample."
            )


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
    "redistributed). A blank cell means that factor couldn't be reconstructed point-in-time for this "
    "ticker: **Valuation / Growth / Profitability** come from SEC XBRL filings, so they're blank for ETFs "
    "and many non-US filers; **News** is GDELT article tone over the 30 days before each date, populated "
    "only when *Include news sentiment* is ticked and the company has coverage (it provides tone, not "
    "individual headlines)."
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

