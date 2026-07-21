"""
News & Earnings Analyzer (Sections 6.2 + 6.5). Streamlit only — the fetching,
caching, sentiment scoring, and summarizing all live in engine/news.py and
engine/earnings.py; this file is the picker, tables, and metrics.
"""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pandas as pd
import streamlit as st

from app import _cache
from app import _theme
from app._auth import gate
from db.session import init_db
from engine import earnings, news, portfolio, watchlist

st.set_page_config(page_title="News — Investment Co-Pilot", page_icon="📰", layout="wide")
_theme.apply()
init_db()
gate("news")  # restricted: guests are stopped here (Phase B)

_theme.page_header("News & Earnings", eyebrow="Research")
st.caption(
    "Personal, educational tool — not financial advice. Sentiment is a finance-tuned model's read of "
    "headlines/press releases (FinBERT), not a recommendation. Free-tier data can be delayed or incomplete."
)

_SENTIMENT_EMOJI = {"Positive": "🟢", "Neutral": "⚪", "Negative": "🔴", "—": "⚪"}


def _emoji_label(label: str) -> str:
    return f"{_SENTIMENT_EMOJI.get(label, '⚪')} {label}"


# --------------------------------------------------------------------------
# Ticker picker — your holdings + watchlist, or anything you type
# --------------------------------------------------------------------------

known = sorted({h["ticker"] for h in portfolio.list_holdings()} | {w["ticker"] for w in watchlist.list_watchlist()})

c1, c2 = st.columns([2, 1])
picked = c1.selectbox("Ticker", known, index=0) if known else None
custom = c2.text_input("…or a custom ticker").strip().upper()
ticker = custom or picked

if not ticker:
    st.info("Add a holding or watchlist item — or type a ticker above — to see its news and earnings.")
    st.stop()

# --------------------------------------------------------------------------
# Cross-signal summary (review #5) — opt-in, because it runs the Screener.
# --------------------------------------------------------------------------
# Stance as a badge rather than a coloured circle: the label carries the meaning
# for anyone who can't distinguish the hues, and it matches the badges used for
# ratings elsewhere.
_STANCE_LABEL = {"positive": "POSITIVE", "negative": "NEGATIVE", "neutral": "NEUTRAL", "n/a": "NO DATA"}
_STANCE_CLASS = {"positive": "sb", "negative": "s", "neutral": "h", "n/a": "h"}

if st.checkbox(
    "🔀 Cross-signal summary",
    help="Tally the app's independent reads on this ticker — Screener, news sentiment, last earnings, and "
         "creator mentions — to see where they agree or disagree. Runs the Screener, so it's opt-in. Not a "
         "prediction.",
):
    with st.spinner(f"Gathering signals for {ticker}…"):
        summary = _cache.signal_summary(ticker)

    if not summary["counted"]:
        st.caption("None of the signals have data for this ticker yet.")
    else:
        pos, neu, neg, counted = (summary["positive"], summary["neutral"],
                                  summary["negative"], summary["counted"])
        if pos > neg and pos >= 2:
            lean, lean_kind = "Signals lean positive", "sb"
        elif neg > pos and neg >= 2:
            lean, lean_kind = "Signals lean negative", "s"
        elif pos and neg:
            lean, lean_kind = "Signals are mixed", "faint"
        else:
            lean, lean_kind = "No clear lean", "h"

        rows = "".join(
            f'<tr><td>{r.name}</td>'
            f'<td>{_theme.badge_html(_STANCE_LABEL[r.stance], _STANCE_CLASS[r.stance])}</td>'
            f'<td class="co">{r.detail}</td></tr>'
            for r in summary["reads"]
        )
        _theme.panel(
            "Cross-signal summary",
            f'<div style="margin-bottom:12px">{_theme.badge_html(lean, lean_kind)}'
            f'<span class="co" style="margin-left:10px">{pos} positive · {neu} neutral · '
            f'{neg} negative, of {counted} with data</span></div>'
            '<table class="cp-table"><thead><tr><th>Signal</th><th>Read</th>'
            '<th>Detail</th></tr></thead>'
            f"<tbody>{rows}</tbody></table>"
            '<div class="cp-foot"><b>Not a prediction, and not a combined score.</b> These are the app\'s '
            "independent reads shown side by side — each keeps its own caveats on its own page.</div>",
            tag=ticker,
        )

view = st.radio("View", ["📰 News", "📈 Earnings"], horizontal=True, label_visibility="collapsed")

# --------------------------------------------------------------------------
# News
# --------------------------------------------------------------------------

if view == "📰 News":
    force = st.button("🔄 Refresh news", help="Fetch the latest headlines now instead of using the cache.")
    with st.spinner(f"Analyzing news for {ticker}…"):
        if force:
            _cache.clear()  # drop the memoized copy so the refresh re-reads fresh data
            analysis = news.analyze_ticker(ticker, force=True)
        else:
            analysis = _cache.news_analysis(ticker)

    st.caption(analysis.summary)

    if analysis.has_sentiment:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Overall sentiment", f"{analysis.overall_score}/100")
        m2.metric("🟢 Positive", analysis.positive)
        m3.metric("⚪ Neutral", analysis.neutral)
        m4.metric("🔴 Negative", analysis.negative)
        st.caption(
            "**How to read this:** 0–100 where **50 is neutral** — below 50 means the model read the "
            "recent *headlines* as net-negative, above 50 as net-positive. It scores headlines only "
            "(not full articles), so it can differ from your own read, and because headline sentiment is "
            "usually mild, scores often sit near the middle."
        )
    elif analysis.total_count:
        st.info(
            "Showing headlines without sentiment scores — the FinBERT model isn't available in this "
            "environment (`pip install transformers torch`). Everything else still works.",
            icon="ℹ️",
        )

    if analysis.headlines:
        table = pd.DataFrame(
            [
                {
                    "Sentiment": _emoji_label(h["sentiment_label"]),
                    "Headline": h["headline"],
                    "Source": h["source"],
                    "Published": pd.to_datetime(h["published_at"]).strftime("%Y-%m-%d %H:%M"),
                    "Link": h["url"],
                }
                for h in analysis.headlines
            ]
        )
        st.dataframe(
            table, width="stretch", hide_index=True,
            column_config={"Link": st.column_config.LinkColumn("Link", display_text="open ↗")},
        )
    else:
        st.caption("No headlines to show.")

# --------------------------------------------------------------------------
# Earnings
# --------------------------------------------------------------------------

else:
    with st.spinner(f"Analyzing earnings for {ticker}…"):
        report = earnings.analyze_ticker(ticker)

    st.caption(report.summary)

    if report.latest and report.latest.get("beat") is not None:
        latest = report.latest
        e1, e2, e3 = st.columns(3)
        e1.metric("Latest EPS (actual)", f"${latest['eps_actual']:.2f}")
        e2.metric(
            "vs. estimate",
            f"${latest['eps_estimate']:.2f}" if latest["eps_estimate"] is not None else "—",
            f"{latest['eps_surprise_pct']:+.1f}%" if latest["eps_surprise_pct"] is not None else None,
        )
        e3.metric("Result", "Beat ✅" if latest["beat"] else "Miss ❌")

    if report.surprises:
        st.subheader("Recent quarters")
        surprises_df = pd.DataFrame(
            [
                {
                    "Period": s["period"],
                    "EPS actual": s["eps_actual"],
                    "EPS estimate": s["eps_estimate"],
                    "Surprise %": s["eps_surprise_pct"],
                    "Result": "Beat" if s["beat"] else ("Miss" if s["beat"] is False else "—"),
                }
                for s in report.surprises
            ]
        )
        st.dataframe(
            surprises_df.style.format(
                {"EPS actual": "${:,.2f}", "EPS estimate": "${:,.2f}", "Surprise %": "{:+.1f}%"}, na_rep="—"
            ),
            width="stretch", hide_index=True,
        )

    st.subheader("Latest press release (SEC 8-K)")
    if report.release:
        release = report.release
        r1, r2 = st.columns([1, 2])
        r1.metric("Release sentiment", _emoji_label(release["sentiment_label"]))
        with r2:
            st.write(f"**Filed:** {release.get('filing_date') or '—'}")
            st.markdown(f"[View the full filing on SEC.gov ↗]({release['url']})")

        highlights = release.get("highlights_md") or []
        if highlights:
            st.markdown("**Key figures from the release**")
            st.markdown("\n".join(f"- {h}" for h in highlights))

        with st.expander("Read the full press-release text"):
            st.markdown(release.get("body_md") or "_No text extracted._")
    else:
        st.caption(
            "No 8-K earnings press release found for this ticker. Not every company files an EX-99.1 "
            "exhibit, and non-US-listed tickers aren't in SEC EDGAR (see the blueprint's free-tier notes)."
        )
