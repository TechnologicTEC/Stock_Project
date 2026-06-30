"""
Portfolio Health Evaluation (Section 6.4). Streamlit only — all the actual
math lives in engine/health.py; this file is metrics, flags, and a table.
"""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pandas as pd
import streamlit as st

from db.session import init_db
from engine import health, portfolio

st.set_page_config(page_title="Health — Investment Co-Pilot", page_icon="📊", layout="wide")
init_db()

st.title("Portfolio Health")
st.caption(
    "Personal, educational tool — not financial advice. These are simple, explainable checks over "
    "free-tier data, not a comprehensive risk assessment."
)

if not portfolio.list_holdings():
    st.info("You haven't added any holdings yet. Add some on the **Portfolio** page first, then come back here.")
    st.stop()

LOOKBACK_OPTIONS = {"3M": 90, "6M": 182, "1Y": 365, "2Y": 730}
lookback_label = st.radio(
    "Lookback window", list(LOOKBACK_OPTIONS.keys()), index=2, horizontal=True, label_visibility="collapsed"
)
lookback_days = LOOKBACK_OPTIONS[lookback_label]

with st.spinner("Computing health metrics..."):
    report = health.get_health_report(lookback_days=lookback_days)

st.caption(
    f"Based on {lookback_label} of history (as of {report.as_of.isoformat()}). Risk-free rate used for "
    f"Sharpe: {report.risk_free_rate_annual:.2%} — source: {report.risk_free_rate_source}."
)

if report.errors:
    with st.expander("⚠️ Some metrics had data issues"):
        for err in report.errors:
            st.caption(f"- {err}")

# --------------------------------------------------------------------------
# Headline metrics
# --------------------------------------------------------------------------

MIN_DATA_NOTE = f"Needs at least {health.MIN_DATA_POINTS} trading days of overlapping history; not enough yet."

m1, m2, m3, m4 = st.columns(4)

with m1:
    st.metric("Beta vs. S&P 500", f"{report.beta:.2f}" if report.beta is not None else "—")
    st.caption(f"{report.beta_data_points} trading days" if report.beta is not None else MIN_DATA_NOTE)

with m2:
    st.metric("Sharpe ratio", f"{report.sharpe_ratio:.2f}" if report.sharpe_ratio is not None else "—")
    st.caption(f"{report.sharpe_data_points} trading days" if report.sharpe_ratio is not None else MIN_DATA_NOTE)

with m3:
    st.metric(
        "Trailing annualized return",
        f"{report.expected_return_annualized_pct:+.1f}%" if report.expected_return_annualized_pct is not None else "—",
    )
    st.caption("Historical average, not a forecast" if report.expected_return_annualized_pct is not None else MIN_DATA_NOTE)

with m4:
    st.metric("Max drawdown", f"{report.max_drawdown_pct:.1f}%" if report.max_drawdown_pct is not None else "—")
    st.caption(f"{report.max_drawdown_data_points} trading days" if report.max_drawdown_pct is not None else MIN_DATA_NOTE)

st.caption(
    "These four numbers come from your portfolio's day-to-day value changes and don't account for when "
    "you bought or sold — adding or removing a holding partway through the lookback window will show up "
    "as a price swing in these calculations, not just as your own contribution. They're most accurate "
    "over a window where your holdings didn't change."
)

st.divider()

# --------------------------------------------------------------------------
# Flags
# --------------------------------------------------------------------------

st.subheader("Flags")

_SEVERITY_RENDER = {"warning": st.warning, "info": st.info, "good": st.success}
for flag in report.flags:
    _SEVERITY_RENDER.get(flag.severity, st.info)(flag.message)

st.divider()

# --------------------------------------------------------------------------
# Concentration table
# --------------------------------------------------------------------------

st.subheader("Concentration")

if report.concentration:
    breakdown_display = {
        "ticker": "Single holding", "sector": "Sector", "asset_type": "Asset type",
        "country": "Country", "market_cap": "Market cap",
    }
    rows = [
        {
            "Breakdown": breakdown_display.get(c.breakdown, c.breakdown),
            "Largest": c.top_label,
            "% of portfolio": c.top_pct,
            "Threshold": c.threshold,
            "Flagged": "🚩" if c.flagged else "",
        }
        for c in report.concentration
    ]
    df = pd.DataFrame(rows)
    st.dataframe(
        df.style.format({"% of portfolio": "{:.1f}%", "Threshold": "{:.0f}%"}),
        width="stretch", hide_index=True,
    )
else:
    st.caption("Not enough data to compute concentration yet.")

st.caption(
    "“Flagged” means the largest item in that breakdown exceeds the threshold shown — these are simple, "
    "fixed cutoffs (documented in `engine/health.py`), not a judgment that concentration is necessarily bad. "
    "A row showing **Unknown** as the largest item means sector/country/market-cap data couldn't be looked "
    "up for those holdings (e.g. a Finnhub access issue) — that's a data gap, never flagged as concentration."
)
