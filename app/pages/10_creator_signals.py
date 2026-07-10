"""
Creator Signals (docs/creator-signals-plan.md) — the read-only view over stocks
mentioned in the creators' latest YouTube videos, each with the screener's take
and a one-click "add to watchlist". The scanning/extraction runs in a scheduled
job (scripts/scan_creators.py); this page only reads what it stored.
"""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st

from app._auth import gate
from db.session import init_db
from engine import creator_signals, watchlist

st.set_page_config(page_title="Creator Signals — Investment Co-Pilot", page_icon="📊", layout="wide")
init_db()
gate("creator_signals")
# Idempotent: makes the built-in creator(s) visible here before the first scan
# has run, so nobody has to add them by hand.
creator_signals.seed_default_creators()

_STANCE = {"bullish": "🟢 Bullish", "bearish": "🔴 Bearish", "neutral": "⚪ Neutral", "unknown": "· —"}

st.title("Creator Signals")
st.caption(
    "Stocks **mentioned** in the creators' latest videos, run through the app's screener. A mention is **not** "
    "an endorsement — the creator may be bearish — and the screener score is an explainable, educational signal, "
    "**not advice**. Updated automatically after new uploads."
)

with st.expander("⚙️ Manage creators"):
    new_channel = st.text_input("Add a YouTube channel (URL or @handle)", key="add_creator_input",
                                placeholder="https://www.youtube.com/@ZipTrader")
    if st.button("Add creator") and new_channel.strip():
        try:
            info = creator_signals.add_creator(new_channel.strip())
            st.success(f"Added **{info['display_name'] or info['channel_id']}** — it'll be scanned on the next run.")
            st.rerun()
        except Exception as exc:
            st.error(f"Couldn't add that channel: {exc}")

    for c in creator_signals.list_creators():
        row = st.columns([5, 1])
        row[0].write(f"{'🟢' if c['active'] else '⚪'} **{c['display_name']}**"
                     + (f" · {c['handle']}" if c['handle'] else ""))
        if row[1].button("Disable" if c["active"] else "Enable", key=f"toggle_{c['channel_id']}"):
            creator_signals.set_creator_active(c["channel_id"], not c["active"])
            st.rerun()

signals = creator_signals.recent_signals()
if not signals:
    st.info(
        "No signals yet. The scanner checks the creators' channels for new videos every few hours and screens "
        "the stocks they discuss — check back after the next upload."
    )
    st.stop()

owned = {w["ticker"] for w in watchlist.list_watchlist()}

for sig in signals:
    st.divider()
    st.markdown(f"#### [{sig['title'] or sig['video_id']}]({sig['url']})")
    date_str = sig["published_at"].strftime("%b %d, %Y") if sig["published_at"] else ""
    st.caption(f"{sig['creator']}{' · ' + date_str if date_str else ''}")

    mentions = sig["mentions"]
    if not mentions:
        st.caption("No individual stocks identified in this video.")
        continue

    head = st.columns([1.2, 3, 1.7, 1.4, 1.7, 1.4])
    for col, label in zip(head, ["Ticker", "Company", "Creator's take", "Screener", "Rating", ""]):
        col.markdown(f"**{label}**")

    for m in mentions:
        c = st.columns([1.2, 3, 1.7, 1.4, 1.7, 1.4])
        c[0].markdown(f"**{m['ticker']}**")
        c[1].write(m["company_name"] or "—")
        c[2].write(_STANCE.get(m["stance"], "· —"))
        c[3].write(f"{m['screener_score']:.0f}/100" if m["screener_score"] is not None else "—")
        c[4].write(m["recommendation"] or "—")
        if m["ticker"] in owned:
            c[5].caption("✓ watchlist")
        elif c[5].button("➕ Add", key=f"add_{sig['video_id']}_{m['ticker']}"):
            watchlist.add_to_watchlist(m["ticker"])
            st.rerun()
