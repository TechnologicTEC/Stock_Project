"""
AI Chat Assistant (Section 6.6) — the responder.

Per the blueprint, this is a **tool-calling layer, not a freeform chatbot**. The
MVP here is a deterministic, template-based intent router over engine/chat_tools.py:
it recognizes a handful of questions ("what's my portfolio worth?", "why is it
down today?", "how much of my portfolio is TSLA?", "how risky is it?") and
answers them from the app's own cached data — zero cost, no API key, fully
testable.

The blueprint's stage 2 is to let an LLM field free-form questions by calling
those *same* chat_tools functions. That slots in here as an alternative
`answer()` path behind an API key, without touching the tools — see the note at
the bottom of this file. Until then, unrecognized questions get a helpful list
of what the assistant *can* answer, rather than a fake confident reply.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from engine import chat_tools


@dataclass
class ChatResponse:
    text: str
    intent: str
    data: dict | None = None


def _money(value) -> str:
    return f"${value:,.2f}" if value is not None else "n/a"


def _pct(value) -> str:
    return f"{value:+.2f}%" if value is not None else "n/a"


def _updown(value) -> str:
    if value is None:
        return "flat"
    return "up" if value > 0 else "down" if value < 0 else "flat"


def _find_ticker(question: str, known: set[str]) -> str | None:
    for token in re.findall(r"[A-Za-z]{1,5}", question.upper()):
        if token in known:
            return token
    return None


HELP_TEXT = (
    "I can answer questions about **your own portfolio** from the app's cached data. Try:\n"
    "- *What's my portfolio worth?*\n"
    "- *How am I doing overall?* / *Why is my portfolio down today?*\n"
    "- *What's my biggest holding?*\n"
    "- *How much of my portfolio is in AAPL?*\n"
    "- *What are today's movers?*\n"
    "- *How much cash do I have?* · *What's on my watchlist?*\n"
    "- *How risky is my portfolio?*\n\n"
    "I'm a deterministic assistant (not an LLM), so I stick to these and never make up numbers."
)


def _help(intent: str = "help") -> ChatResponse:
    return ChatResponse(HELP_TEXT, intent)


def _no_holdings() -> ChatResponse:
    return ChatResponse(
        "You don't have any holdings yet — add some on the **Portfolio** page and I'll have something to talk "
        "about. You can still ask *what's on my watchlist?* or *how much cash do I have?*",
        "no_holdings",
    )


# --------------------------------------------------------------------------
# Intent handlers — each returns a ChatResponse from the tools
# --------------------------------------------------------------------------

def _answer_value() -> ChatResponse:
    v = chat_tools.get_portfolio_value()
    text = (
        f"Your portfolio is worth **{_money(v['total_value'])}** — {_money(v['invested_value'])} in holdings "
        f"and {_money(v['wallet_balance'])} in cash."
    )
    return ChatResponse(text, "portfolio_value", v)


def _answer_performance() -> ChatResponse:
    p = chat_tools.get_portfolio_performance()
    text = (
        f"Overall you're **{_updown(p['total_gain_loss'])} {_money(abs(p['total_gain_loss'])) }** "
        f"({_pct(p['total_gain_loss_pct'])}) versus cost. Today your holdings are "
        f"**{_updown(p['total_day_change'])} {_money(abs(p['total_day_change']))}**."
    )
    return ChatResponse(text, "performance", p)


def _answer_today() -> ChatResponse:
    p = chat_tools.get_portfolio_performance()
    movers = chat_tools.get_todays_movers()
    if not movers["ranked_desc"]:
        return ChatResponse("I don't have today's price changes for your holdings right now.", "today")
    lead = (
        f"Your holdings are **{_updown(p['total_day_change'])} {_money(abs(p['total_day_change']))}** today."
    )
    best, worst = movers["best"], movers["worst"]
    parts = [lead]
    if worst and worst["day_change_pct"] is not None and worst["day_change_pct"] < 0:
        parts.append(f"Biggest drag: **{worst['ticker']}** ({_pct(worst['day_change_pct'])}).")
    if best and best["day_change_pct"] is not None and best["day_change_pct"] > 0:
        parts.append(f"Biggest lift: **{best['ticker']}** ({_pct(best['day_change_pct'])}).")
    return ChatResponse(" ".join(parts), "today", movers)


def _answer_biggest() -> ChatResponse:
    h = chat_tools.get_biggest_holding()
    if not h:
        return _no_holdings()
    text = (
        f"Your biggest holding is **{h['ticker']}** at {_money(h['market_value'])} — "
        f"**{_pct(h['weight_pct']).lstrip('+')}** of the portfolio."
    )
    return ChatResponse(text, "biggest_holding", h)


def _answer_weight(ticker: str) -> ChatResponse:
    h = chat_tools.get_holding_weight(ticker)
    if not h:
        return ChatResponse(f"You don't hold **{ticker}** right now (nothing with a current value).", "holding_weight")
    text = (
        f"**{ticker}** is **{_pct(h['weight_pct']).lstrip('+')}** of your portfolio "
        f"({_money(h['market_value'])}), and it's {_updown(h['gain_loss_pct'])} {_pct(h['gain_loss_pct'])} "
        f"versus your cost."
    )
    return ChatResponse(text, "holding_weight", h)


def _answer_cash() -> ChatResponse:
    cash = chat_tools.get_cash_balance()
    return ChatResponse(f"You have **{_money(cash)}** in cash (the wallet).", "cash", {"cash": cash})


def _answer_watchlist() -> ChatResponse:
    tickers = chat_tools.get_watchlist()
    if not tickers:
        return ChatResponse("Your watchlist is empty. Add tickers on the **Screener** page.", "watchlist")
    return ChatResponse("On your watchlist: **" + "**, **".join(tickers) + "**.", "watchlist", {"tickers": tickers})


def _answer_risk() -> ChatResponse:
    h = chat_tools.get_health_summary()
    bits = []
    if h["beta"] is not None:
        bits.append(f"beta **{h['beta']:.2f}** vs the S&P 500")
    if h["sharpe_ratio"] is not None:
        bits.append(f"Sharpe **{h['sharpe_ratio']:.2f}**")
    if h["max_drawdown_pct"] is not None:
        bits.append(f"max drawdown **{h['max_drawdown_pct']:.1f}%**")
    lead = "Here's the risk read: " + ("; ".join(bits) if bits else "not enough history yet for the metrics") + "."
    flags = h.get("flags") or []
    if flags:
        lead += " Flags: " + " ".join(flags[:3])
    lead += "\n\n*These are simple, explainable checks — see the Health page for the full picture.*"
    return ChatResponse(lead, "risk", h)


# --------------------------------------------------------------------------
# Router — most specific intents first
# --------------------------------------------------------------------------

def answer(question: str, history: list[dict] | None = None) -> ChatResponse:
    """Answer a question. If the optional LLM path is configured (a GEMINI_API_KEY
    is set), route free-form questions through Gemini — which calls the same
    chat_tools functions — and fall back to the deterministic template responder
    on any failure or when no key is set."""
    q = (question or "").strip()
    if not q:
        return _help()

    from engine import chat_llm
    if chat_llm.is_available():
        try:
            text = chat_llm.answer(q, history=history)
            if text:
                return ChatResponse(text, "llm")
        except Exception:
            pass  # network/rate-limit/refusal/old-SDK → deterministic fallback below

    return _template_answer(q)


def _template_answer(question: str) -> ChatResponse:
    """The deterministic, no-key responder: route a question to a tool-backed
    template answer by keyword intent."""
    q = question.strip()
    ql = q.lower()

    known = chat_tools.known_tickers()
    ticker = _find_ticker(q, known)

    # A ticker + a "how much / weight / position" phrasing -> weight of that ticker.
    if ticker and any(k in ql for k in ("weight", "percent", "% ", "how much of", "position", "allocation")):
        return _answer_weight(ticker)

    if any(k in ql for k in ("today", "mover", "moving", "why", "gainer", "loser", "drag", "lift")):
        return _answer_today()

    if any(k in ql for k in ("biggest", "largest", "top holding", "top position", "concentrated most")):
        return _answer_biggest()

    if any(k in ql for k in ("cash", "wallet")):
        return _answer_cash()

    if "watchlist" in ql or "watching" in ql:
        return _answer_watchlist()

    if any(k in ql for k in ("risk", "risky", "health", "beta", "sharpe", "drawdown", "diversif", "concentrat", "volatil")):
        return _answer_risk()

    if any(k in ql for k in ("worth", "value", "how much is my", "total", "balance", "net worth")):
        return _answer_value()

    if any(k in ql for k in ("gain", "loss", "return", "performance", "profit", "doing", "up or down")):
        return _answer_performance()

    # A bare ticker mention -> that holding's weight/detail.
    if ticker:
        return _answer_weight(ticker)

    return _help("fallback")


# --------------------------------------------------------------------------
# Stage 2 (built): when a GEMINI_API_KEY is configured, answer() routes
# free-form questions through engine/chat_llm.py, where Gemini calls the SAME
# chat_tools functions as tools. This template path stays as the free, no-key
# (and failure) fallback.
# --------------------------------------------------------------------------
