"""
Tools the AI Chat Assistant can call (Section 6.6). Each is a small, pure-ish
function that reads from the app's *own* already-cached data (portfolio, health,
watchlist) and returns structured dicts — never a free-form string. That shape
is deliberate: the template responder in engine/chat.py consumes these today,
and a future LLM layer can call the exact same functions as tools with no
changes here.

Nothing in this module hits an external API directly — it goes through the
engine layer (portfolio.py etc.), which already caches. So chat stays cheap.
"""
from __future__ import annotations

from engine import portfolio, watchlist


def get_portfolio_value() -> dict:
    """Total value (holdings + wallet cash), and the split."""
    s = portfolio.get_portfolio_summary()
    return {
        "total_value": s["total_value"],
        "invested_value": s["invested_value"],
        "wallet_balance": s["wallet_balance"],
    }


def get_portfolio_performance() -> dict:
    """Overall gain/loss vs cost, and today's dollar change."""
    s = portfolio.get_portfolio_summary()
    return {
        "total_gain_loss": s["total_gain_loss"],
        "total_gain_loss_pct": s["total_gain_loss_pct"],
        "total_day_change": s["total_day_change"],
    }


def _weighted_holdings() -> list[dict]:
    """Every valued holding with its portfolio weight, biggest first."""
    valuation = portfolio.get_live_valuation()
    valued = [v for v in valuation if v.get("market_value") is not None]
    total = sum(v["market_value"] for v in valued)
    out = [
        {
            "ticker": v["ticker"],
            "shares": v["shares"],
            "market_value": v["market_value"],
            "weight_pct": round(v["market_value"] / total * 100, 2) if total else None,
            "day_change_pct": v.get("day_change_pct"),
            "gain_loss_pct": v.get("gain_loss_pct"),
        }
        for v in valued
    ]
    return sorted(out, key=lambda h: -(h["market_value"] or 0))


def get_holdings() -> list[dict]:
    return _weighted_holdings()


def get_biggest_holding() -> dict | None:
    holdings = _weighted_holdings()
    return holdings[0] if holdings else None


def get_holding_weight(ticker: str) -> dict | None:
    ticker = ticker.strip().upper()
    for h in _weighted_holdings():
        if h["ticker"] == ticker:
            return h
    return None


def get_todays_movers() -> dict:
    """Best and worst holdings by today's % change (plus the full ranked list)."""
    movers = [h for h in _weighted_holdings() if h.get("day_change_pct") is not None]
    ranked = sorted(movers, key=lambda h: h["day_change_pct"])  # worst -> best
    return {
        "best": ranked[-1] if ranked else None,
        "worst": ranked[0] if ranked else None,
        "ranked_desc": list(reversed(ranked)),
    }


def get_cash_balance() -> float:
    return portfolio.get_wallet_balance()


def get_watchlist() -> list[str]:
    return [w["ticker"] for w in watchlist.list_watchlist()]


def get_health_summary() -> dict:
    """Beta / Sharpe / max drawdown / flags from the Health page's report.
    Heavier than the other tools (it fetches SPY + a risk-free rate), so the
    responder only calls it for explicit risk/health questions."""
    from engine import health  # local: keeps the heavier health deps off import

    report = health.get_health_report()
    return {
        "beta": report.beta,
        "sharpe_ratio": report.sharpe_ratio,
        "max_drawdown_pct": report.max_drawdown_pct,
        "flags": [f.message for f in report.flags],
    }


def known_tickers() -> set[str]:
    """Tickers the user actually has (holdings + watchlist) — used to spot a
    ticker mentioned in a question."""
    return {h["ticker"] for h in portfolio.list_holdings()} | set(get_watchlist())


# --------------------------------------------------------------------------
# Richer tools (Section 6.6 follow-on). Heavy engine deps (news/FinBERT,
# screener, projections, health) are imported *inside* each function so this
# module stays cheap to import and the template responder never pays for them.
# --------------------------------------------------------------------------

_HORIZON_DAYS = {"3M": 90, "6M": 182, "1Y": 365, "2Y": 730}
_PERIOD_DAYS = {"1W": 7, "1M": 30, "3M": 90, "6M": 182, "1Y": 365}


def _r(x, n: int = 2):
    # Cast to a plain Python float so tool outputs JSON-serialize cleanly for the
    # LLM (numpy floats from pandas otherwise leak through).
    return round(float(x), n) if x is not None else None


def _pick(d: dict, *keys):
    for k in keys:
        if d.get(k) is not None:
            return d[k]
    return None


def get_ticker_news(ticker: str) -> dict:
    """Recent headlines + FinBERT sentiment for one ticker (the app's own cached
    news). Use to explain what's in the news around a stock."""
    from engine import news

    a = news.analyze_ticker(ticker.strip().upper())
    return {
        "ticker": a.ticker,
        "overall_sentiment_0_100": a.overall_score,  # 50 = neutral
        "counts": {"positive": a.positive, "neutral": a.neutral, "negative": a.negative},
        "headlines": [
            {"headline": h["headline"], "sentiment": h.get("sentiment_label"),
             "published_at": str(h.get("published_at")), "source": h.get("source")}
            for h in a.headlines[:8]
        ],
        "summary": a.summary,
    }


def whats_moving_and_why(limit: int = 3) -> dict:
    """The portfolio's biggest movers today, each paired with its recent
    headlines — i.e. the news *around* each move. Answers 'why is my portfolio
    up/down today'."""
    from engine import news

    movers = [m for m in get_todays_movers()["ranked_desc"] if m.get("day_change_pct") is not None]
    if not movers:
        return {"movers": [], "note": "No holdings have a live price change today (market closed or no data)."}
    picks = sorted(movers, key=lambda m: -abs(m["day_change_pct"]))[:max(1, limit)]
    out = []
    for m in picks:
        try:
            heads = [h["headline"] for h in news.analyze_ticker(m["ticker"]).headlines[:3]]
        except Exception:
            heads = []
        out.append({
            "ticker": m["ticker"], "day_change_pct": m["day_change_pct"],
            "weight_pct": m.get("weight_pct"), "recent_headlines": heads,
        })
    return {
        "movers": out,
        "disclaimer": "The headlines are what's in the news *around* each move, not a proven cause of it.",
    }


def get_market_context() -> dict:
    """Is the broad market up or down today? Today's move in the S&P 500 (SPY) —
    use to tell whether a portfolio move is stock-specific or market-wide."""
    from engine.data_sources import finnhub_client

    try:
        q = finnhub_client.get_quote("SPY")
        return {"index": "S&P 500 (SPY proxy)", "today_pct": _r(q.get("percent_change")),
                "current_price": _r(q.get("current_price"))}
    except Exception as exc:
        return {"index": "S&P 500 (SPY proxy)", "today_pct": None, "error": str(exc)}


def get_screener_rating(ticker: str) -> dict:
    """The Investment Screener's 0-100 score and Strong Buy..Strong Sell rating
    for one ticker, with the per-factor breakdown. Educational, NOT advice."""
    from engine import screener

    results = screener.screen_tickers([ticker.strip().upper()])
    if not results:
        return {"ticker": ticker.strip().upper(), "score": None, "note": "no screener result"}
    r = results[0]
    return {
        "ticker": r.ticker,
        "overall_score_0_100": _r(r.overall_score),
        "recommendation": r.recommendation,
        "factor_scores": {name: _r(f.score) for name, f in r.factors.items()},
        "data_errors": r.data_errors,
    }


def get_recent_earnings(ticker: str) -> dict:
    """The most recent earnings for a ticker: beat/miss vs estimates and the
    latest release summary."""
    from engine import earnings

    a = earnings.analyze_ticker(ticker.strip().upper())
    return {"ticker": a.ticker, "latest_quarter": a.latest, "has_release": a.has_release, "summary": a.summary}


def get_projection(subject: str = "portfolio", horizon: str = "1Y") -> dict:
    """A **statistical range of outcomes — NOT a prediction** for the whole
    'portfolio' or a single ticker over a horizon (3M/6M/1Y/2Y)."""
    from engine import projections

    days = _HORIZON_DAYS.get(horizon.strip().upper(), 365)
    subject = subject.strip()
    if subject.lower() in ("", "portfolio", "the portfolio", "my portfolio"):
        p = projections.project_portfolio(days, apply_outlook=True)
    else:
        p = projections.project_ticker(subject.upper(), days, apply_outlook=True)
    if p is None or p.insufficient_data:
        return {"subject": subject or "portfolio", "horizon": horizon, "note": "not enough history to project"}
    return {
        "subject": p.label, "horizon": horizon, "start_value": _r(p.start_value),
        "median": _r(_pick(p.horizon_values, 50)),
        "range_low": _r(_pick(p.horizon_values, 10, 5)),
        "range_high": _r(_pick(p.horizon_values, 90, 95)),
        "median_return_pct": _r(_pick(p.horizon_returns_pct, 50)),
        "disclaimer": "A statistical range implied by past volatility — NOT a forecast of the actual outcome.",
    }


def get_period_performance(period: str = "1M") -> dict:
    """Portfolio return over a period (1W/1M/3M/6M/1Y/YTD) and the S&P 500's over
    the same window — so you can see if you're beating the benchmark."""
    from datetime import date, timedelta

    from engine import price_history

    today = date.today()
    period = period.strip().upper()
    start = date(today.year, 1, 1) if period == "YTD" else today - timedelta(days=_PERIOD_DAYS.get(period, 30))

    hist = portfolio.get_value_history(start, today)
    values = [h["value"] for h in hist if h.get("value")]
    if len(values) < 2:
        return {"period": period, "note": "not enough portfolio history in this window"}
    port_ret = _r((values[-1] / values[0] - 1) * 100)

    spy_ret = None
    try:
        closes = price_history.get_history_df("SPY", start, today).get("close")
        if closes is not None and closes.dropna().shape[0] >= 2:
            c = closes.dropna()
            spy_ret = _r((c.iloc[-1] / c.iloc[0] - 1) * 100)
    except Exception:
        pass
    return {
        "period": period, "portfolio_return_pct": port_ret, "sp500_return_pct": spy_ret,
        "beating_benchmark": None if spy_ret is None else bool(port_ret > spy_ret),
    }


def get_concentration_risk() -> dict:
    """The portfolio's biggest concentrations (single stock, sector, asset type,
    country, market cap) and which cross their risk thresholds."""
    from engine import health

    report = health.get_health_report()
    return {
        "concentrations": [
            {"breakdown": c.breakdown, "top": c.top_label, "top_pct": _r(c.top_pct),
             "threshold_pct": _r(c.threshold), "flagged": c.flagged}
            for c in report.concentration
        ],
        "flags": [f.message for f in report.flags],
    }
