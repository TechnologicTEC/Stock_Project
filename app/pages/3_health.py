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
import plotly.graph_objects as go
import streamlit as st

from app._auth import gate
from db.session import init_db
from engine import health, news, portfolio, projections

st.set_page_config(page_title="Health — Investment Co-Pilot", page_icon="📊", layout="wide")
init_db()
gate("health")  # guest-accessible (Phase B) — sets the current user scope

st.title("Portfolio Health")
st.caption(
    "Personal, educational tool — not financial advice. These are simple, explainable checks over "
    "free-tier data, not a comprehensive risk assessment."
)

if not portfolio.list_holdings():
    st.info("You haven't added any holdings yet. Add some on the **Portfolio** page first, then come back here.")
    st.stop()

lookback_label = st.radio(
    "Lookback window", list(health.LOOKBACK_OPTIONS.keys()), index=2, horizontal=True, label_visibility="collapsed"
)
lookback_days = health.LOOKBACK_OPTIONS[lookback_label]

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
# Mid-window contribution warning - shown ABOVE the four metrics, since it
# affects how trustworthy every one of them is for this window. See
# engine/health.py's _detect_mid_window_contributions() docstring.
# --------------------------------------------------------------------------

if report.mid_window_contributions:
    added_list = ", ".join(
        f"**{c.ticker}** (added {c.purchase_date.isoformat()})" for c in report.mid_window_contributions
    )
    suggestion = ""
    if report.recommended_clean_lookback_days is not None:
        clean_label = next(
            label for label, days in health.LOOKBACK_OPTIONS.items() if days == report.recommended_clean_lookback_days
        )
        suggestion = f" Try the **{clean_label}** window instead — your other holdings have been stable that long."
    else:
        suggestion = " None of the available windows are fully clean yet — check back once your holdings have settled."

    st.warning(
        f"You added {added_list} partway through this {lookback_label} window. The four numbers below "
        f"can't tell the difference between new money arriving and the market actually moving, so they're "
        f"likely distorted by that contribution rather than reflecting real performance." + suggestion,
        icon="⚠️",
    )

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

st.divider()

# --------------------------------------------------------------------------
# Forward-Looking Projections (Section 6.11). A STATISTICAL PROJECTION, NOT A
# PREDICTION — the framing has to stay unmissable. Math lives in
# engine/projections.py; this is the subject/horizon picker, the fan chart,
# the band summary, and the template explanation.
# --------------------------------------------------------------------------

st.subheader("Forward-looking projection")
st.caption(
    "**A statistical projection, not a prediction.** This does *not* forecast where anything is headed. It "
    "takes how much this stock (or your portfolio) has moved day-to-day over the past year — its **volatility** "
    "— and spreads that forward with the lognormal/geometric-Brownian-motion math behind options pricing. The "
    "band's *width* is pure volatility. Its *centre* leans with the **Screener's rating** — up for highly-rated "
    "stocks, down for poorly-rated ones, flat for neutral (toggle below; capped and shrunk by the Screener's "
    "validation IC). Nothing drifts without the rating to back it, and the real future can still land anywhere, "
    "including outside the band."
)

holding_tickers = sorted({h["ticker"] for h in portfolio.list_holdings()})
PROJECTION_SUBJECTS = ["Your portfolio"] + holding_tickers

p1, p2 = st.columns([2, 1])
subject = p1.selectbox("Project", PROJECTION_SUBJECTS, index=0)
proj_horizon_label = p2.selectbox("Horizon", list(health.LOOKBACK_OPTIONS.keys()), index=2)  # default 1Y
proj_horizon_days = health.LOOKBACK_OPTIONS[proj_horizon_label]

apply_outlook = st.checkbox(
    "Tilt the median by the Screener's outlook (runs the Screener)",
    value=True,
    help="On: the median leans up for highly-rated stocks and down for poorly-rated ones — the Screener's "
         "fundamental score, capped at ±25%/yr, scaled by the score, and shrunk by the ticker's validation IC "
         "(run a validation to sharpen it). A neutrally-rated stock stays flat — nothing drifts without the "
         "score to back it. Off: a pure volatility cone with a flat median. Runs the Screener for the subject, "
         "so the first run can take a moment (cached afterwards).",
)

with st.spinner(f"Projecting {subject} {proj_horizon_label} forward…"):
    if subject == "Your portfolio":
        projection = projections.project_portfolio(proj_horizon_days, apply_outlook=apply_outlook)
    else:
        projection = projections.project_ticker(subject, proj_horizon_days, apply_outlook=apply_outlook)

if projection is None:
    st.info(
        f"Couldn't get enough price history for **{subject}** to project a range. For a single ticker it may "
        "not be available on the free data source; for the portfolio, add a holding first."
    )
elif projection.insufficient_data:
    st.info(
        f"**{subject}** doesn't have enough recent trading history yet to estimate a range "
        f"(needs at least {projections.MIN_RETURN_POINTS} days; have {projection.n_return_days})."
    )
else:
    r = projection.horizon_returns_pct
    v = projection.horizon_values
    unit = "value" if subject == "Your portfolio" else "price"

    b1, b2, b3 = st.columns(3)
    b1.metric(
        f"90% range ({proj_horizon_label})",
        f"{r[5]:+.1f}% … {r[95]:+.1f}%",
        help="The model's 5th-to-95th-percentile band of outcomes — it expects the actual result to fall "
             "outside this range about 1 time in 10. Not a floor or a ceiling.",
    )
    b2.metric(
        "Middle-half range",
        f"{r[25]:+.1f}% … {r[75]:+.1f}%",
        help="The interquartile band (25th–75th percentile): the model's 'middle half' of outcomes.",
    )
    b3.metric(
        "Annualized volatility",
        f"{projection.annualized_volatility_pct:.0f}%",
        help="How much this stock/portfolio has swung day-to-day over the past year, annualized. This is what "
             "sets the width of the band above — more volatility, wider range.",
    )

    # Fan chart: a light 90% band (p5–p95), a darker middle-half band (p25–p75),
    # and the median line. Bands are drawn with 'tonexty' fills, so each shaded
    # trace must immediately follow the upper edge it fills down to.
    fan = pd.DataFrame(projection.fan)
    dates = fan["date"]
    band_color = "70, 130, 180"  # steel blue
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=dates, y=fan["p95"], line=dict(width=0), name="95th pct", hoverinfo="skip", showlegend=False))
    fig.add_trace(go.Scatter(
        x=dates, y=fan["p5"], line=dict(width=0), fill="tonexty", fillcolor=f"rgba({band_color},0.12)",
        name="90% range (5th–95th)", hoverinfo="skip",
    ))
    fig.add_trace(go.Scatter(x=dates, y=fan["p75"], line=dict(width=0), name="75th pct", hoverinfo="skip", showlegend=False))
    fig.add_trace(go.Scatter(
        x=dates, y=fan["p25"], line=dict(width=0), fill="tonexty", fillcolor=f"rgba({band_color},0.28)",
        name="Middle half (25th–75th)", hoverinfo="skip",
    ))
    fig.add_trace(go.Scatter(
        x=dates, y=fan["p50"], line=dict(color=f"rgba({band_color},1)", width=2), name="Median",
        hovertemplate="%{x|%b %d, %Y}<br>median: %{y:$,.2f}<extra></extra>",
    ))
    fig.update_layout(
        margin=dict(l=0, r=0, t=10, b=0),
        yaxis_title=f"Projected {unit} (USD)",
        legend=dict(orientation="h", yanchor="top", y=-0.12, x=0),
        hovermode="x unified",
    )
    st.plotly_chart(fig, width="stretch", key="projection_fan")

    # ---- Outlook tilt explanation (only when the Screener outlook is applied).
    if projection.outlook_applied:
        if projection.outlook_score is None:
            st.caption("The Screener couldn't score this subject (no usable fundamentals), so the median is left flat.")
        else:
            if projection.outlook_ic is not None:
                conf_txt = (f"validation IC {projection.outlook_ic:+.2f} → the tilt is scaled to "
                            f"{projection.outlook_confidence:.0%} confidence")
            else:
                conf_txt = (f"no validation run yet → a cautious {projection.outlook_confidence:.0%} confidence "
                            "(run a validation on the Screener Validation page to sharpen this)")
            reco = f" ({projection.outlook_recommendation})" if projection.outlook_recommendation else ""
            blend = f", {projection.outlook_detail}" if projection.outlook_detail else ""
            st.info(
                f"📈 **Screener outlook applied.** Score **{projection.outlook_score:.0f}/100**{reco}{blend}; "
                f"{conf_txt}, tilting the median by **{projection.applied_annual_tilt_pct:+.1f}%/yr** (capped at "
                f"±25%). Only the centre line moves — the band's width still comes purely from volatility, so the "
                f"outcome can easily land the other way."
            )

    st.markdown(projections.describe(projection, proj_horizon_label, "year"))
    tilted = projection.outlook_applied and abs(projection.applied_annual_tilt_pct) > 0.05
    median_note = (
        f"The median is tilted **{projection.applied_annual_tilt_pct:+.1f}%/yr** by the Screener's outlook (above)."
        if tilted else
        "The median line is **flat at today's value** — no directional trend assumed."
    )
    st.caption(
        f"Estimated from {projection.n_return_days} trading days of history. {median_note} For context (not "
        f"projected): {subject} actually returned **{projection.observed_annual_return_pct:+.0f}%** over the past "
        "year. "
        + ("The portfolio projection values your current holdings held constant across the year, so it isn't "
           "distorted by money you added or withdrew — but it does assume today's mix, not your past one."
           if subject == "Your portfolio" else "")
    )

    # ---- Step 2: real supporting context — recent news sentiment, presented
    # alongside the band (never replacing it). Per-ticker only; cached, so a
    # reload is free. Failures here must never take down the projection.
    if subject != "Your portfolio":
        try:
            analysis = news.analyze_ticker(subject)
            note = projections.sentiment_context_note(analysis.overall_score, analysis.positive, analysis.negative)
            if note:
                st.info("📰 " + note)
        except Exception:
            pass

    # ---- Step 3: historical calibration — how often the actual return has
    # landed inside the band this model would have drawn on past dates. Opt-in
    # (it walks several years of history) and per-ticker (a clean price series,
    # unlike the portfolio value series).
    if subject != "Your portfolio":
        if st.checkbox(
            "Show historical calibration — how often the actual return landed inside the range",
            value=False,
            help="Replays this exact model on past dates (no look-ahead: it only ever uses data knowable then) "
                 "and checks how often the real subsequent return fell inside the range it would have drawn. "
                 "A well-calibrated 90% band should contain the outcome about 90% of the time.",
        ):
            with st.spinner(f"Replaying the projection for {subject} across past windows…"):
                calib = projections.validate_coverage(subject, proj_horizon_days)

            if calib is None or calib.insufficient_data:
                st.info(
                    f"Not enough price history for **{subject}** to test calibration over a "
                    f"{proj_horizon_label} horizon yet — needs several years of trading history."
                )
            else:
                c1, c2, c3 = st.columns(3)
                c1.metric(
                    "90% band coverage", f"{calib.coverage_90_pct:.0f}%",
                    help="Share of past windows where the actual return landed inside the 90% (5th–95th) band. "
                         "The nominal target is 90%.",
                )
                c2.metric(
                    "Middle-half coverage", f"{calib.coverage_50_pct:.0f}%",
                    help="Share inside the 25th–75th band. Nominal target is 50%.",
                )
                c3.metric("Past windows tested", calib.n_windows)
                st.markdown(projections.calibration_verdict(calib.coverage_90_pct, calib.n_windows))
                st.caption(
                    "Each window estimates volatility from the year before an anchor date, projects the band "
                    f"{proj_horizon_label} forward, then compares it to what the stock *actually* did next — "
                    "using only information knowable on that date, so there's no look-ahead."
                )

