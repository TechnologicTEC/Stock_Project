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


def _horizon_from_text(ql: str) -> str:
    """A projection horizon (3M/6M/1Y/2Y) parsed from the question; 1Y default."""
    if any(k in ql for k in ("2 year", "two year", "2y", "2-year")):
        return "2Y"
    if any(k in ql for k in ("6 month", "six month", "6-month", "half year")):
        return "6M"
    if any(k in ql for k in ("3 month", "three month", "3-month", "quarter")):
        return "3M"
    return "1Y"


def _period_from_text(ql: str) -> str:
    """A look-back period (1W/1M/3M/6M/1Y/YTD) parsed from the question; 1M default."""
    if any(k in ql for k in ("ytd", "year to date", "this year")):
        return "YTD"
    if "week" in ql:
        return "1W"
    if any(k in ql for k in ("6 month", "six month", "6-month", "half year")):
        return "6M"
    if any(k in ql for k in ("3 month", "three month", "3-month", "quarter")):
        return "3M"
    if any(k in ql for k in ("1 year", "one year", "past year", "12 month", "1-year")):
        return "1Y"
    return "1M"


HELP_TEXT = (
    "I can answer questions about **your own portfolio** from the app's cached data. Try:\n"
    "- *What's my portfolio worth?* · *How am I doing overall?*\n"
    "- *What's my biggest holding?* · *How much of my portfolio is in AAPL?*\n"
    "- *Why is my portfolio down today?* · *Any news on ASML?*\n"
    "- *Is the whole market down today?* · *How does the screener rate PLTR?*\n"
    "- *How did NVDA's last earnings go?*\n"
    "- *Am I beating the S&P this month?* · *What's my 1-year projected range?*\n"
    "- *How much cash do I have?* · *What's on my watchlist?* · *How risky is my portfolio?*\n\n"
    "I answer these from the app's own data (no AI key needed) and never make up numbers."
)


def _help(intent: str = "help") -> ChatResponse:
    return ChatResponse(HELP_TEXT, intent)


def _llm_unavailable_note(exc: Exception) -> str | None:
    """A user-facing heads-up when the LLM path failed for a *known transient*
    reason — so a quota/rate-limit problem isn't silently disguised as a
    (wrong-looking) deterministic answer. Returns None for unknown errors, which
    still degrade quietly to the template."""
    msg = str(exc).lower()
    if any(k in msg for k in ("429", "resource_exhausted", "quota", "rate limit", "rate-limit")):
        return ("⏳ *The AI assistant has hit its Gemini free-tier limit for now (it resets daily) — "
                "here's a quick answer from the built-in responder:*")
    return None


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


def _answer_news(ticker: str) -> ChatResponse:
    n = chat_tools.get_ticker_news(ticker)
    heads = n.get("headlines") or []
    if not heads:
        return ChatResponse(f"I don't have any recent news cached for **{ticker}** right now.", "news", n)
    score = n.get("overall_sentiment_0_100")
    lead = f"Recent news for **{ticker}**" + (f" (overall sentiment {score}/100)" if score is not None else "") + ":"
    lines = [lead]
    for h in heads[:5]:
        label = h.get("sentiment")
        lines.append(f"- {h['headline']}" + (f" · _{label}_" if label else ""))
    lines.append("\n*Headlines are what's in the news **around** this stock — context, not a proven cause of any move.*")
    return ChatResponse("\n".join(lines), "news", n)


def _answer_why() -> ChatResponse:
    data = chat_tools.whats_moving_and_why(limit=3)
    movers = data.get("movers") or []
    if not movers:
        return ChatResponse(data.get("note") or "I don't have today's price changes for your holdings right now.", "why")
    lines = ["Here's what's moving your portfolio today, with the news around each move:"]
    for m in movers:
        heads = m.get("recent_headlines") or []
        lines.append(f"- **{m['ticker']}** {_pct(m['day_change_pct'])}" + (f" — _{heads[0]}_" if heads else ""))
    if data.get("disclaimer"):
        lines.append(f"\n*{data['disclaimer']}*")
    return ChatResponse("\n".join(lines), "why", data)


def _answer_screener(ticker: str) -> ChatResponse:
    r = chat_tools.get_screener_rating(ticker)
    score = r.get("overall_score_0_100")
    if score is None:
        return ChatResponse(f"I couldn't get a screener rating for **{ticker}** right now.", "screener", r)
    return ChatResponse(
        f"The screener scores **{ticker}** at **{score}/100** — **{r.get('recommendation', 'n/a')}**. "
        "*It's an explainable score from public data, not advice.*",
        "screener", r)


def _answer_earnings(ticker: str) -> ChatResponse:
    e = chat_tools.get_recent_earnings(ticker)
    if not e.get("has_release"):
        return ChatResponse(f"I don't have a recent earnings release cached for **{ticker}**.", "earnings", e)
    return ChatResponse(f"**{ticker}** — {e.get('summary') or 'no summary available.'}", "earnings", e)


def _answer_market() -> ChatResponse:
    m = chat_tools.get_market_context()
    pct = m.get("today_pct")
    if pct is None:
        return ChatResponse("I can't read the S&P 500's move right now.", "market", m)
    return ChatResponse(f"The broad market (**S&P 500**) is **{_updown(pct)}** today ({_pct(pct)}).", "market", m)


def _answer_projection(subject: str, horizon: str) -> ChatResponse:
    p = chat_tools.get_projection(subject, horizon)
    if p.get("median") is None:
        return ChatResponse(p.get("note") or f"I don't have enough history to project {subject}.", "projection", p)
    text = (
        f"Over ~{p.get('horizon', horizon)}, {p.get('subject', subject)} has a modelled range of "
        f"**{_money(p['range_low'])} to {_money(p['range_high'])}** (median {_money(p['median'])}). "
        f"*{p.get('disclaimer', '')}*"
    )
    return ChatResponse(text, "projection", p)


def _answer_period(period: str) -> ChatResponse:
    d = chat_tools.get_period_performance(period)
    if d.get("portfolio_return_pct") is None:
        return ChatResponse(d.get("note") or "I don't have enough history for that window.", "period_performance", d)
    lead = f"Over **{period}**, your portfolio is **{_pct(d['portfolio_return_pct'])}**"
    spy = d.get("sp500_return_pct")
    if spy is not None:
        beat = d.get("beating_benchmark")
        verdict = "ahead of" if beat else "behind" if beat is False else "level with"
        lead += f" versus the S&P 500's {_pct(spy)} — you're **{verdict}** the benchmark."
    else:
        lead += " (no S&P comparison available for this window)."
    return ChatResponse(lead, "period_performance", d)


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
        except Exception as exc:  # network/quota/refusal/old-SDK → deterministic fallback below
            base = _template_answer(q)
            note = _llm_unavailable_note(exc)  # tell the user when it's a known transient (e.g. quota)
            if note:
                return ChatResponse(f"{note}\n\n{base.text}", base.intent)
            return base

    return _template_answer(q)


def _template_answer(question: str) -> ChatResponse:
    """The deterministic, no-key responder: route a question to a tool-backed
    template answer by keyword intent."""
    q = question.strip()
    ql = q.lower()

    known = chat_tools.known_tickers()
    ticker = _find_ticker(q, known)

    news_kw = "news" in ql or "headline" in ql
    earnings_kw = any(k in ql for k in ("earnings", "eps", "beat estimate", "beat expectation"))
    screener_kw = any(k in ql for k in ("screener", "rating", "screen")) or bool(re.search(r"\brate\b", ql))
    projection_kw = any(k in ql for k in ("projection", "projected", "project my", "range of outcome",
                                          "expected range", "forecast", "monte carlo"))
    benchmark_kw = any(k in ql for k in ("beating", "benchmark", "outperform", "underperform", "keeping up",
                                         "vs the market", "versus the market", "vs the s&p", "versus the s&p",
                                         "beat the market", "beat the s&p"))

    # A ticker + a "how much / weight / position" phrasing -> weight of that ticker.
    if ticker and any(k in ql for k in ("weight", "percent", "% ", "how much of", "position", "allocation")):
        return _answer_weight(ticker)

    # Ticker-specific reads: "any news on X" / "why is X down" -> X's news;
    # "X earnings" -> X's earnings; "rate X" -> X's screener score.
    if ticker and (news_kw or "why" in ql):
        return _answer_news(ticker)
    if ticker and earnings_kw:
        return _answer_earnings(ticker)
    if ticker and screener_kw:
        return _answer_screener(ticker)

    # Portfolio-wide "why is it moving" / "any news" -> the movers + their headlines.
    if "why" in ql or news_kw:
        return _answer_why()

    # Projected range (Monte-Carlo band) for a ticker or the whole portfolio.
    if projection_kw:
        return _answer_projection(ticker or "portfolio", _horizon_from_text(ql))

    # "Am I beating the S&P over <period>?" -> portfolio return vs the benchmark.
    if benchmark_kw:
        return _answer_period(_period_from_text(ql))

    # Is the broad market up/down? (guarded so "beating the S&P"/"projected" — handled
    # just above — don't get answered here with today's index move instead.)
    if any(k in ql for k in ("s&p", "sp500", "sp 500", "the market", "whole market", "overall market",
                             "broad market", "market down", "market up", "market doing")) \
            and not (benchmark_kw or projection_kw):
        return _answer_market()

    if any(k in ql for k in ("today", "mover", "moving", "gainer", "loser", "drag", "lift")):
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
