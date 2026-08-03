"""
Investment Screener (Section 6.1). Streamlit only — all the scoring logic
lives in engine/screener.py; this file is forms, tables, and charts.
"""
import sys
from datetime import date
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pandas as pd
import plotly.express as px
import streamlit as st

from app import _cache, _theme
from app._auth import gate
from db.session import current_user_id, init_db
from engine import portfolio, screener, screener_validation, watchlist

st.set_page_config(page_title="Screener — Investment Co-Pilot", page_icon="📊", layout="wide")
_theme.apply()
init_db()
gate("screener")  # restricted: guests are stopped here (Phase B)

_theme.page_header("Screener", eyebrow="Research")
st.caption(
    "Personal, educational tool — not financial advice. Scores are a transparent, "
    "weighted heuristic over free-tier data, not a prediction."
)

# --------------------------------------------------------------------------
# S&P 500 leaderboard — a live screen of the whole index, ranked. Produced by the
# weekly batch job (scripts/screen_universe.py); this only reads it. The
# honest framing is the whole point: this ranking is exactly what the
# cross-sectional validation measured, so its measured IC sits right beside it.
# --------------------------------------------------------------------------

def _add_to_watchlist_button(column, ticker: str, watched: set[str], key: str) -> None:
    """One-click add, matching the Creator Signals board. Already-watched names
    show a tick instead of a button so the row still reads as "handled"."""
    if ticker in watched:
        column.markdown('<span class="cp-watched" title="On your watchlist">✓ watching</span>',
                        unsafe_allow_html=True)
    elif column.button("➕ Add", key=key, use_container_width=True):
        watchlist.add_to_watchlist(ticker)
        st.rerun()


def _render_leaderboard() -> None:
    lb = screener.load_leaderboard()
    with st.expander("🏆 S&P 500 leaderboard — highest-scoring names right now", expanded=False):
        if not lb:
            st.info(
                "No leaderboard yet. Run the **S&P 500 leaderboard** GitHub Action (or "
                "`python scripts/screen_universe.py`) and the ranking appears here. It live-screens all "
                "~500 names — including news sentiment and analyst consensus, which the historical "
                "validation can't reconstruct — so it takes ~an hour and runs as a scheduled job."
            )
            return

        rows = lb.get("rows", [])
        # Show the AGE, not just the date. The job is weekly and the cache holds
        # for three weeks, so a missed run can leave stale scores on screen looking
        # exactly like fresh ones — the same trap that hid an 11-day-old FX rate.
        generated = lb.get("generated_at")
        age_days = None
        if generated:
            try:
                age_days = (date.today() - date.fromisoformat(generated)).days
            except (TypeError, ValueError):
                age_days = None
        age_txt = "" if age_days is None else (
            " · today" if age_days == 0 else f" · {age_days} day{'s' if age_days != 1 else ''} ago")
        st.caption(f"Live screen of {lb.get('n_scored', len(rows))} S&P 500 names · "
                   f"run {generated or '—'}{age_txt} (refreshes weekly)")
        if age_days is not None and age_days > 10:
            st.warning(
                f"**These scores are {age_days} days old** — the weekly refresh looks like it hasn't run. "
                "Prices and news have moved since; re-run the **S&P 500 leaderboard** Action for a current "
                "ranking.",
                icon="🕒",
            )

        # The measured track record of THIS ranking, pulled from the universe
        # validation. Shown up front so "highest-scoring" can't be misread as
        # "will go up".
        uni = screener_validation.load_universe_result()
        ic = (uni or {}).get("overall", {}).get("mean_ic") if uni else None
        if ic is not None:
            o = uni["overall"]
            sig = "distinguishable from zero" if o.get("significant") else "**not** statistically distinguishable from zero"
            st.warning(
                f"**What this ranking is worth.** Across the S&P 500 this exact score has a cross-sectional "
                f"information coefficient of **{ic:+.3f}** (t={o.get('t_stat')}, hit rate "
                f"{o.get('hit_rate')}) — a *faint tilt*, {sig}. The top of the list is where the screener "
                "is **most positive right now**, not a prediction these names will outperform. Not financial "
                "advice.",
                icon="📉",
            )
        else:
            st.warning(
                "**Not a buy list.** These are the names the screener rates highest right now — a ranking, "
                "not a prediction. Run the S&P 500 validation to see how predictive this ordering has "
                "actually been. Not financial advice.",
                icon="📉",
            )

        top_n = st.radio("Show", [10, 20, 50], horizontal=True, index=1, key="lb_top_n")
        show = rows[:top_n]
        factor_labels = screener.FACTOR_LABELS
        watched = {w["ticker"] for w in watchlist.list_watchlist()}

        # Short column heads — six full factor names would force a horizontal
        # scroll on every screen width.
        short = {"valuation": "VAL", "growth": "GRW", "profitability": "PRF",
                 "momentum": "MOM", "sentiment": "SEN", "analyst_confidence": "ANA"}

        # A column grid rather than a .cp-table, for the same reason the creator
        # signals board is one: each row carries a real "add to watchlist" button
        # and Streamlit widgets can't live inside raw HTML. The factor columns are
        # deliberately narrow — they hold two digits.
        # Tuned against the narrowest real case (1280px window, sidebar open):
        # the button column has to hold "➕ Add" on ONE line — at 1.0 it wrapped
        # and every row grew to 55px — and the factor columns only ever hold two
        # digits, so the width goes to Name and the button instead.
        widths = [0.4, 1.0, 1.9, 0.75, 1.2] + [0.55] * len(factor_labels) + [1.45]
        with _theme.section("Highest-scoring right now",
                            tag=f"top {len(show)} of {lb.get('n_scored', len(rows))}"), \
                st.container(key="lb_grid"):
            head = st.columns(widths, vertical_alignment="bottom")
            labels = ["#", "Ticker", "Name", "Score", "Rating"] + \
                     [short.get(f, f[:3].upper()) for f in factor_labels] + [""]
            for col, label in zip(head, labels):
                col.markdown(f'<div class="cp-eyebrow">{label}</div>', unsafe_allow_html=True)

            for r in show:
                c = st.columns(widths, vertical_alignment="center")
                c[0].markdown(f'<span class="cp-dim">{r["rank"]}</span>', unsafe_allow_html=True)
                c[1].markdown(f'<span class="cp-tick">{r["ticker"]}</span>', unsafe_allow_html=True)
                c[2].markdown(f'<span class="cp-co">{r.get("name") or "—"}</span>', unsafe_allow_html=True)
                c[3].markdown(f'<span class="cp-num">{r["score"]:.1f}</span>', unsafe_allow_html=True)
                c[4].markdown(_theme.badge_html(r["recommendation"]), unsafe_allow_html=True)
                for i, f in enumerate(factor_labels):
                    v = (r.get("factor_scores") or {}).get(f)
                    c[5 + i].markdown(
                        f'<span class="cp-num">{v:.0f}</span>' if v is not None
                        else '<span class="cp-dim">—</span>', unsafe_allow_html=True)
                _add_to_watchlist_button(c[-1], r["ticker"], watched, key=f"lb_add_{r['ticker']}")

            st.caption(
                "Sentiment (SEN) and Analyst (ANA) are **live-only** factors — not part of the "
                "historical IC above, which covers the fundamentals-plus-momentum core."
            )


_render_leaderboard()

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
        perf = _cache.watchlist_performance(
            current_user_id(), tuple((w["ticker"], w["added_at"]) for w in wl_items))
        # Worst-first: the point of tracking this is to notice the ones that got
        # away from you, and a name you watched but never bought is exactly where
        # that's easy to miss. Unpriced rows sink to the bottom rather than
        # sorting as if they were zero.
        perf.sort(key=lambda r: (r["change_pct"] is None, r["change_pct"] or 0))

        def _money(v):
            return f"${v:,.2f}" if v else '<span class="dim">—</span>'

        wl_body = []
        for r in perf:
            pct, cls = r["change_pct"], ""
            if pct is None:
                pct_cell = '<td class="num dim">—</td>'
            else:
                cls = "up" if pct >= 0 else "down"
                pct_cell = f'<td class="num {cls}">{pct:+.2f}%</td>'
            held = f"{r['days_held']}d" if r["days_held"] else "today"
            wl_body.append(
                f'<tr><td><span class="tick">{r["ticker"]}</span></td>'
                f'<td class="num dim">{r["added_on"]}</td>'
                f'<td class="num dim">{held}</td>'
                f'<td class="num">{_money(r["added_price"])}</td>'
                f'<td class="num">{_money(r["current_price"])}</td>'
                f"{pct_cell}</tr>"
            )
        _theme.panel(
            "Since you added it",
            '<div class="cp-scroll"><table class="cp-table">'
            '<thead><tr><th>Ticker</th><th class="num">Added</th><th class="num">Held</th>'
            '<th class="num">Price then</th><th class="num">Price now</th>'
            '<th class="num">Change</th></tr></thead>'
            f"<tbody>{''.join(wl_body)}</tbody></table></div>"
            '<div class="cp-foot"><b>Price then</b> is the closing price on the day you added the ticker, '
            "not the intraday price you were looking at — so a name added mid-session can read a little "
            "off on day one. This is price movement only: no dividends, and it tracks the stock, not a "
            "position you hold.</div>",
            tag=f"{len(perf)} watched",
        )

        rm_cols = st.columns([4, 1])
        to_remove = rm_cols[0].multiselect(
            "Remove from watchlist", options=[r["ticker"] for r in perf], key="wl_remove_pick",
            placeholder="Pick tickers to remove…",
        )
        rm_cols[1].markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
        if rm_cols[1].button("Remove", key="wl_remove_btn", use_container_width=True, disabled=not to_remove):
            for t in to_remove:
                watchlist.remove_from_watchlist(t)
            st.rerun()
    else:
        st.caption("Nothing on your watchlist yet — add tickers above, or from the S&P 500 leaderboard.")

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
        st.plotly_chart(fig, width="stretch", theme=None)
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
        "Sentiment (15%) is scored from recent news headlines via the FinBERT pipeline (the same one "
        "behind the News page) — 50 = neutral, higher = more positive. When a ticker has no recent news "
        "(or FinBERT isn't installed), that factor is marked unavailable and its weight is spread "
        "proportionally across the other five, rather than faking a neutral score."
    )
    st.caption(
        "Valuation (P/E, P/B, P/S) and gross margin are scored against thresholds adjusted for the "
        "ticker's detected industry (e.g. software vs. banking get different 'cheap'/'expensive' "
        "ranges) — shown per ticker below. This is a hand-picked approximation, not live market "
        "data; there's no free source for real-time sector medians. Growth, net margin, ROE, and "
        "debt/equity still use one general threshold set for every industry."
    )

_TRACK_ICON = {"positive": "🟢", "weak": "🟡", "none": "⚪", "negative": "🔴"}

for r in results:
    label = f"{r.ticker} — {r.overall_score:.1f} ({r.recommendation})" if r.overall_score is not None else f"{r.ticker} — Insufficient data"
    with st.expander(label):
        # Review #6: pair the recommendation with its own measured track record.
        track = screener_validation.track_record(r.ticker)
        if track:
            st.markdown(
                f"{_TRACK_ICON[track['stance']]} **Track record:** this Screener score {track['text']} "
                f"(validation IC **{track['ic']:+.2f}**"
                + (f", n={track['n']}" if track["n"] else "")
                + (f", as of {track['as_of']}" if track["as_of"] else "") + ")."
                + track.get("scope_note", "")
            )
        else:
            st.caption("↪️ Not validated yet — run **Screener Validation** on this ticker to see whether the "
                       "score has actually predicted its returns before trusting the rating.")

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
