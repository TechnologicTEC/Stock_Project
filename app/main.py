"""Streamlit entry point + dashboard. Run with: streamlit run app/main.py"""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from collections import Counter
from datetime import date

import streamlit as st

from app import _cache
from app import _theme
from app._auth import gate
from db.session import current_user_id, init_db
from engine import creator_signals, currency, portfolio, screener_validation

_EARNINGS_HOUR = {"bmo": "before open", "amc": "after close"}

st.set_page_config(page_title="Investment Co-Pilot", page_icon="📊", layout="wide")
_theme.apply()
init_db()
gate("main")  # resolve the signed-in user and scope the DB to them (Phase B)


def _money(value) -> str:
    return f"${value:,.2f}" if value is not None else "—"


holdings = portfolio.list_holdings()

_theme.page_header(
    "Your portfolio at a glance",
    eyebrow="Dashboard",
    sub=(f"{len(holdings)} holding{'s' if len(holdings) != 1 else ''} tracked" if holdings
         else "No holdings yet — start on the Holdings page"),
)
_theme.advice(
    "<b>Personal, educational tool — not financial advice.</b> It runs on free-tier data "
    "that can be delayed, incomplete, or occasionally wrong — don't trade on it alone."
)

# --------------------------------------------------------------------------
# Getting started (no holdings yet)
# --------------------------------------------------------------------------
if not holdings:
    st.markdown(
        """
Welcome. Use the pages in the sidebar to get around:

- **Portfolio** — your holdings, valuation, allocation, and per-holding Screener ratings
- **Screener** — explainable weighted-factor stock scoring (Buy → Sell)
- **Health** — concentration, beta, Sharpe ratio, drawdown, and flags
- **News** — headline + earnings-release sentiment, and a cross-signal summary per ticker
- **Creator Signals** — stocks the creators you follow have been discussing
- **Screener Validation** — does the score actually predict returns? (information coefficient)
- **Backtest**, **Paper Trading**, and the **Assistant** chat

To begin, open the **Portfolio** page and add a holding (one at a time, or import a CSV).
        """
    )
    st.stop()

# --------------------------------------------------------------------------
# Dashboard (you have holdings) — light, cheap data only. No heavy screener/
# news runs here; those stay opt-in on their own pages.
# --------------------------------------------------------------------------
uid = current_user_id()
with st.spinner("Loading your dashboard…"):
    summary = _cache.portfolio_summary(uid)
    valuation = _cache.live_valuation(uid)

tickers = tuple(sorted(h["ticker"] for h in holdings))

# The Screener read is the one expensive card (screen_tickers runs FinBERT +
# per-ticker analyst calls), so it stays OPT-IN — the dashboard's contract is
# cheap reads only. Once run it's cached, so re-loads are instant.
ratings: dict = {}
if st.session_state.get("dash_rated"):
    with st.spinner("Screening your holdings…"):
        ratings = _cache.screener_ratings(tickers)

# --------------------------------------------------------------------------
# KPI row
# --------------------------------------------------------------------------
holdings_value = summary["invested_value"]
day_change = summary["total_day_change"]
prior = (holdings_value or 0) - (day_change or 0)
day_pct = (day_change / prior * 100) if prior else None

k1, k2, k3, k4 = st.columns(4)
k1.metric(
    "Holdings value", _money(holdings_value),
    f"{day_pct:+.2f}% today" if day_pct is not None else None,
)
with k2:
    # FX can fail (free-tier endpoint); fall back to a dash rather than crashing
    # the whole dashboard over a display conversion.
    try:
        nzd_rate = currency.get_rate("NZD")
        fx = currency.rate_info("NZD") or {}
    except Exception:
        nzd_rate, fx = None, {}
    st.metric("In NZD",
              currency.format_amount(holdings_value, "NZD", nzd_rate) if nzd_rate else "—",
              f"@ {fx['usd_per_nzd']:.4f} USD/NZD" if fx.get("usd_per_nzd") else None,
              delta_color="off")
    if fx.get("as_of"):
        st.caption(f"{fx.get('source', 'FX')} · {fx['as_of']}")
wallet = summary["wallet_balance"]
book = (holdings_value or 0) + (wallet or 0)
k3.metric("Cash / wallet", _money(wallet),
          f"{(wallet / book * 100):.1f}% of book" if book else None, delta_color="off")
with k4:
    if ratings:
        counts = Counter(r["recommendation"] for r in ratings.values() if r.get("recommendation"))
        buys = counts["Strong Buy"] + counts["Buy"]
        sells = counts["Sell"] + counts["Strong Sell"]
        verdict = "Net Buy" if buys > sells else "Net Sell" if sells > buys else "Mixed"
        st.metric("Screener read", verdict,
                  f"{buys} buy · {counts['Hold']} hold · {sells} sell", delta_color="off")
    else:
        st.metric("Screener read", "—", "not run yet", delta_color="off")
        if st.button("Rate holdings", key="dash_rate_btn", use_container_width=True):
            st.session_state["dash_rated"] = True
            st.rerun()

# --------------------------------------------------------------------------
# Body: holdings | signal confidence + leaderboard
# --------------------------------------------------------------------------
left, right = st.columns([1.55, 1])

with left:
    # `default=0` matters: valuation is empty whenever every price fetch fails,
    # and a bare max() would take the whole dashboard down over missing prices.
    largest = max((v.get("market_value") or 0 for v in valuation), default=0) or 1
    rows = []
    for v in sorted(valuation, key=lambda x: -(x.get("market_value") or 0)):
        pct = v.get("day_change_pct")
        move = (f'<span class="{"up" if pct >= 0 else "down"}">{pct:+.2f}%</span>'
                if pct is not None else '<span class="co">—</span>')
        rec = (ratings.get(v["ticker"]) or {}).get("recommendation")
        badge = _theme.badge_html(rec) if rec else '<span class="co">—</span>'
        width = (v.get("market_value") or 0) / largest * 100
        rows.append(
            f'<tr><td><span class="tick">{v["ticker"]}</span></td>'
            f'<td class="val">{_money(v.get("market_value"))}</td>'
            f'<td><span class="cp-wbar"><i style="width:{width:.0f}%"></i></span></td>'
            f'<td class="val">{move}</td><td>{badge}</td></tr>'
        )
    if rows:
        _theme.panel(
            "Your holdings",
            '<table class="cp-table"><thead><tr><th>Ticker</th><th>Value</th>'
            '<th>Weight</th><th>Today</th><th>Screener</th></tr></thead>'
            f"<tbody>{''.join(rows)}</tbody></table>"
            + ("" if ratings else
               '<div class="cp-foot">Screener column is blank until you press '
               "<b>Rate holdings</b> — it runs the full six-factor screen, which is too "
               "slow to do on every dashboard load.</div>"),
            tag=f"{len(valuation)} positions",
        )
    else:
        _theme.panel(
            "Your holdings",
            '<p class="cp-note">Couldn\'t price your holdings right now — the market data '
            "source didn't return anything. Your positions are safe; try again shortly.</p>",
        )

with right:
    # Signal confidence — cheap cache read of the S&P 500 validation.
    uni = screener_validation.load_universe_result()
    if uni and (uni.get("overall") or {}).get("mean_ic") is not None:
        o = uni["overall"]
        ic = o["mean_ic"]
        fill = min(abs(ic) * 100 * 2.2, 100)          # a faint IC reads as a small fill
        sig = "distinguishable from zero" if o.get("significant") else "not statistically significant"
        hit = f" · hit {o['hit_rate']:.0%}" if o.get("hit_rate") is not None else ""
        _theme.panel(
            "Signal confidence",
            f'<div class="ic"><b>{ic:+.3f}</b><span class="t">IC · t={o.get("t_stat")}{hit}</span></div>'
            f'<div class="cp-meter"><i style="width:{fill:.0f}%"></i></div>'
            f'<div class="cp-verdict">◆ Faint tilt — {sig}</div>'
            '<p class="cp-note">How well this screener has ranked stocks against each other. '
            'The top of any list is where it is <b>most positive right now</b>, not a forecast.</p>',
            tag=f"{uni.get('n_tickers', '—')} names",
            extra_class="cp-conf",
        )
    else:
        _theme.panel("Signal confidence",
                     '<p class="cp-note">No validation run stored yet — run the S&P 500 validation '
                     "to see how predictive this screener's ranking has actually been.</p>")

    # S&P 500 leaderboard — cheap cache read.
    from engine import screener as _screener  # lazy: keeps the screener stack off page import

    lb = _screener.load_leaderboard()
    if lb and lb.get("rows"):
        lb_rows = "".join(
            f'<tr><td class="co">{r["rank"]}</td>'
            f'<td><span class="tick">{r["ticker"]}</span></td>'
            f'<td class="val">{r["score"]:.1f}</td>'
            f'<td>{_theme.badge_html(r["recommendation"])}</td></tr>'
            for r in lb["rows"][:5]
        )
        _theme.panel(
            "S&P 500 leaderboard",
            '<table class="cp-table"><thead><tr><th>#</th><th>Name</th>'
            '<th>Score</th><th>Rating</th></tr></thead>'
            f"<tbody>{lb_rows}</tbody></table>"
            '<div class="cp-foot"><b>Ranking, not a buy list.</b> Highest-scoring right now.</div>',
            tag=lb.get("generated_at", ""),
        )
    else:
        _theme.panel("S&P 500 leaderboard",
                     '<p class="cp-note">No leaderboard yet — run the S&P 500 leaderboard job and the '
                     "highest-scoring names appear here.</p>")

# --------------------------------------------------------------------------
# Creator signals + what's reporting
# --------------------------------------------------------------------------
c_left, c_right = st.columns(2)

with c_left:
    board = creator_signals.mention_leaderboard()   # cheap DB read; ≥2 mentions, last 3 months
    if board:
        rows = "".join(
            f'<tr><td><span class="tick">{e["ticker"]}</span></td>'
            f'<td class="val">{e["mentions"]}×</td>'
            f'<td class="co">{e["last_seen"].strftime("%b %d") if e["last_seen"] else "—"}</td></tr>'
            for e in board[:5]
        )
        _theme.panel(
            "Creator signals",
            '<table class="cp-table"><thead><tr><th>Ticker</th><th>Mentions</th>'
            '<th>Last seen</th></tr></thead>'
            f"<tbody>{rows}</tbody></table>"
            '<div class="cp-foot">Repetition is <b>attention, not endorsement</b>.</div>',
        )
    else:
        _theme.panel("Creator signals",
                     '<p class="cp-note">Nothing a creator has repeated yet — add channels on the '
                     "Creator Signals page.</p>")

with c_right:
    upcoming = _cache.upcoming_earnings(tickers)
    if upcoming:
        rows = "".join(
            f'<tr><td><span class="tick">{e["ticker"]}</span></td>'
            f'<td class="val">{e["days_until"]}d</td>'
            f'<td class="co">{date.fromisoformat(e["date"]).strftime("%b %d")}'
            f'{" · " + _EARNINGS_HOUR[e["hour"]] if e.get("hour") in _EARNINGS_HOUR else ""}</td>'
            f'<td class="val">{("$%.2f" % e["eps_estimate"]) if e.get("eps_estimate") is not None else "—"}</td>'
            f"</tr>"
            for e in upcoming[:6]
        )
        _theme.panel(
            "Reporting soon",
            '<table class="cp-table"><thead><tr><th>Ticker</th><th>In</th><th>Date</th>'
            '<th>Est. EPS</th></tr></thead>'
            f"<tbody>{rows}</tbody></table>"
            '<div class="cp-foot">Estimates only — Finnhub\'s free tier withholds the actual '
            "beat/miss.</div>",
            tag="next 3 weeks",
        )
    else:
        _theme.panel("Reporting soon",
                     '<p class="cp-note">No holdings report in the next three weeks.</p>')
