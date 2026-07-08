"""
The Chat Assistant's optional LLM path (Section 6.6, stage 2), powered by
**Google Gemini** (free tier). When a GEMINI_API_KEY is set, free-form questions
are answered by Gemini, which calls the SAME underlying engine/chat_tools.py
functions as tools — so it reads only the app's own cached data and can't invent
numbers. Without a key (or if a call fails) engine/chat.py falls back to the
deterministic template responder.

Design notes:
- Uses the `google-genai` SDK's **automatic function calling**: the tool
  functions below are passed to the model, and the SDK builds their schemas
  (from signatures + docstrings), runs the call-tool-feed-result loop, and
  returns the final text. Far less to get wrong than a manual loop.
- The SDK is imported lazily (only in _client()), and contents/config are plain
  dicts, so this module imports and tests fine without `google-genai` installed
  (is_available() just returns False).
- Default model is gemini-2.5-flash; override with CHAT_LLM_MODEL in .env.
"""
from __future__ import annotations

import os

from engine import chat_tools, credentials

DEFAULT_MODEL = "gemini-2.5-flash"
MAX_HISTORY_MESSAGES = 10

SYSTEM_PROMPT = (
    "You are the assistant inside a personal, browser-based investment co-pilot. You answer the user's "
    "questions about THEIR OWN portfolio, watchlist, cash, and risk, using ONLY the provided tools, which "
    "read the app's cached data.\n\n"
    "Rules:\n"
    "- Only state numbers you obtained from a tool this turn. Never invent, estimate, or recall figures. "
    "If a tool returns no data, say so plainly.\n"
    "- This is a personal, educational tool — NOT financial advice. Don't give buy/sell recommendations or "
    "price predictions; if asked, say you can summarise the data but not advise.\n"
    "- When you explain a move using news, present the headlines as what's *around* the move, NOT a proven "
    "cause — say e.g. 'in the news around this' rather than 'X fell because of Y'. The Screener rating is an "
    "explainable score, not a recommendation; projections are a statistical range, not a forecast. Pass on "
    "each tool's disclaimer when you use it.\n"
    "- Be concise and direct: a sentence or two with the key numbers. Money is in USD.\n"
    "- If a question isn't about the user's own portfolio/watchlist/cash/risk, briefly say what you can help "
    "with instead."
)


# --------------------------------------------------------------------------
# Tool functions exposed to Gemini. Thin wrappers over chat_tools with
# model-facing docstrings — the SDK builds each tool's schema from the
# signature + docstring and executes it automatically.
# --------------------------------------------------------------------------

def get_portfolio_value() -> dict:
    """Total portfolio value (holdings plus cash) and the split into invested value and cash."""
    return chat_tools.get_portfolio_value()


def get_portfolio_performance() -> dict:
    """Overall gain or loss versus cost, and today's dollar change for the whole portfolio."""
    return chat_tools.get_portfolio_performance()


def get_holdings() -> list:
    """Every holding with its portfolio weight percent, today's percent change, and gain/loss percent."""
    return chat_tools.get_holdings()


def get_biggest_holding() -> dict:
    """The single largest holding by market value, with its portfolio weight."""
    return chat_tools.get_biggest_holding()


def get_holding_weight(ticker: str) -> dict:
    """Weight percent, market value and gain/loss versus cost for one specific ticker.

    Args:
        ticker: Stock ticker symbol, e.g. AAPL.
    """
    return chat_tools.get_holding_weight(ticker)


def get_todays_movers() -> dict:
    """The best and worst holdings by today's percent change (why the portfolio moved today)."""
    return chat_tools.get_todays_movers()


def get_cash_balance() -> float:
    """Uninvested cash: the wallet balance, in USD."""
    return chat_tools.get_cash_balance()


def get_watchlist() -> list:
    """The tickers on the user's watchlist."""
    return chat_tools.get_watchlist()


def get_health_summary() -> dict:
    """Portfolio risk read: beta versus the S&P 500, Sharpe ratio, max drawdown percent, and flags."""
    return chat_tools.get_health_summary()


def get_ticker_news(ticker: str) -> dict:
    """Recent news headlines and sentiment for one ticker (the app's cached news). Use to
    explain what's in the news around a stock.

    Args:
        ticker: Stock ticker symbol, e.g. ASML.
    """
    return chat_tools.get_ticker_news(ticker)


def whats_moving_and_why() -> dict:
    """The portfolio's biggest movers today, each paired with its recent headlines — the
    news AROUND each move (context, not a proven cause). Use to answer 'why is my
    portfolio up/down today'."""
    return chat_tools.whats_moving_and_why()


def get_market_context() -> dict:
    """Today's move in the S&P 500 (SPY) — tells you whether a portfolio move is
    market-wide or stock-specific."""
    return chat_tools.get_market_context()


def get_screener_rating(ticker: str) -> dict:
    """The Investment Screener's 0-100 score and Strong Buy..Strong Sell rating for one
    ticker, with the per-factor breakdown. Educational only, not advice.

    Args:
        ticker: Stock ticker symbol, e.g. PLTR.
    """
    return chat_tools.get_screener_rating(ticker)


def get_recent_earnings(ticker: str) -> dict:
    """The most recent earnings for a ticker: beat/miss versus estimates and the latest
    release summary.

    Args:
        ticker: Stock ticker symbol, e.g. NVDA.
    """
    return chat_tools.get_recent_earnings(ticker)


def get_projection(subject: str = "portfolio", horizon: str = "1Y") -> dict:
    """A statistical RANGE of outcomes (NOT a prediction) for the whole portfolio or a
    single ticker, over a horizon.

    Args:
        subject: "portfolio" for the whole portfolio, or a ticker symbol.
        horizon: one of "3M", "6M", "1Y", "2Y".
    """
    return chat_tools.get_projection(subject, horizon)


def get_period_performance(period: str = "1M") -> dict:
    """The portfolio's return over a period and the S&P 500's over the same window, so you
    can see if it's beating the benchmark.

    Args:
        period: one of "1W", "1M", "3M", "6M", "1Y", "YTD".
    """
    return chat_tools.get_period_performance(period)


def get_concentration_risk() -> dict:
    """The portfolio's biggest concentrations (single stock, sector, asset type, country,
    market cap) and which ones cross their risk thresholds."""
    return chat_tools.get_concentration_risk()


_TOOLS = [
    get_portfolio_value, get_portfolio_performance, get_holdings, get_biggest_holding,
    get_holding_weight, get_todays_movers, get_cash_balance, get_watchlist, get_health_summary,
    get_ticker_news, whats_moving_and_why, get_market_context, get_screener_rating,
    get_recent_earnings, get_projection, get_period_performance, get_concentration_risk,
]


# --------------------------------------------------------------------------
# Availability + client
# --------------------------------------------------------------------------

def _model() -> str:
    return os.environ.get("CHAT_LLM_MODEL") or DEFAULT_MODEL


def _api_key() -> str | None:
    return credentials.get("GEMINI_API_KEY") or credentials.get("GOOGLE_API_KEY")


def _genai_installed() -> bool:
    try:
        from google import genai  # noqa: F401
        return True
    except Exception:
        return False


def is_available() -> bool:
    """True only when the LLM path is usable: a key is set, the SDK is installed,
    and it hasn't been explicitly disabled. engine/chat.py checks this before
    trying the LLM and falls back to the template responder otherwise."""
    if os.environ.get("CHAT_LLM_DISABLED"):
        return False
    return bool(_api_key()) and _genai_installed()


def _client():
    from google import genai
    return genai.Client(api_key=_api_key())


# --------------------------------------------------------------------------
# Answering
# --------------------------------------------------------------------------

def _history_contents(history: list[dict] | None) -> list[dict]:
    """Turn the page's chat history into Gemini `contents` (dict form), mapping
    the assistant role to Gemini's 'model' and trimming to start on a user turn."""
    contents = []
    for m in (history or [])[-MAX_HISTORY_MESSAGES:]:
        role, text = m.get("role"), m.get("content")
        if role in ("user", "assistant") and text:
            contents.append({"role": "model" if role == "assistant" else "user",
                             "parts": [{"text": text}]})
    while contents and contents[0]["role"] != "user":
        contents.pop(0)
    return contents


def answer(question: str, history: list[dict] | None = None) -> str:
    """Answer `question` with Gemini (automatic function calling over the tools).
    Raises on an empty result so engine/chat.py falls back to the template."""
    client = _client()
    contents = _history_contents(history) + [{"role": "user", "parts": [{"text": question}]}]

    response = client.models.generate_content(
        model=_model(),
        contents=contents,
        config={"system_instruction": SYSTEM_PROMPT, "tools": _TOOLS, "temperature": 0},
    )

    text = (getattr(response, "text", None) or "").strip()
    if not text:
        raise RuntimeError("empty response from the assistant")
    return text
