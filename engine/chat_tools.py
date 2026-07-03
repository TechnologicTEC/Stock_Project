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
