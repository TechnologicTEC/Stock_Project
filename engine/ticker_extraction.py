"""
Extract the stocks discussed in a video transcript (docs/creator-signals-plan.md).

Two paths, mirroring the chat assistant's LLM-with-fallback design:
- **LLM primary (Gemini):** best at spoken names ("the chip maker Nvidia") and
  reading the speaker's stance. Runs once per new video, so the free-tier quota
  that limits interactive chat is a non-issue here.
- **Deterministic fallback (free):** SEC name↔ticker dictionary + $cashtags +
  bare uppercase symbols, used when there's no key / the LLM errors.

Every candidate is validated against the SEC ticker list, so only real,
US-listed symbols survive — the main false-positive killer.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from engine.data_sources import sec_tickers

_STANCES = {"bullish", "bearish", "neutral", "unknown"}

# Uppercase tokens that look like tickers but are common words/abbreviations —
# only the deterministic path needs this (the LLM uses context).
_STOPWORDS = {
    "A", "I", "AI", "IT", "CEO", "CFO", "COO", "IPO", "ETF", "USA", "US", "USD", "DD", "YOLO",
    "EPS", "PE", "GDP", "FED", "SEC", "NYSE", "EV", "CPI", "ATH", "YOY", "Q1", "Q2", "Q3", "Q4",
    "OK", "TV", "PR", "IMO", "FYI", "AKA", "FAQ", "AH", "PM", "AM", "EU", "UK", "NOW", "HR",
    "IQ", "ID", "SO", "GO", "UP", "AT", "ON", "IN", "BE", "DO", "MY", "AN", "AS", "OR", "IF",
    "BY", "HE", "WE", "ME", "NO", "VS", "PS", "OG", "DV",
}


@dataclass
class Mention:
    ticker: str
    company_name: str | None = None
    stance: str = "unknown"           # bullish | bearish | neutral | unknown
    confidence: float | None = None


# --------------------------------------------------------------------------
# LLM path
# --------------------------------------------------------------------------
_PROMPT = (
    "You are given the transcript of a stock-market YouTube video. Identify every "
    "publicly-traded company or stock the speaker actually discusses (ignore market "
    "indexes and the speaker's own channel/promos). For each, give its US ticker "
    "symbol, the company name, and the speaker's stance toward it: bullish, bearish, "
    "or neutral. Only include real, tradeable US-listed tickers. Respond with ONLY a "
    'JSON array like [{"ticker":"NVDA","company":"NVIDIA","stance":"bullish"}] and '
    "nothing else. If none, respond with []."
)


def _llm_available() -> bool:
    from engine import chat_llm
    return chat_llm.is_available()


def _extract_llm(text: str) -> list[Mention]:
    from engine import chat_llm

    client = chat_llm._client()
    resp = client.models.generate_content(
        model=chat_llm._model(),
        contents=[{"role": "user", "parts": [{"text": _PROMPT + "\n\nTRANSCRIPT:\n" + text}]}],
        config={"response_mime_type": "application/json", "temperature": 0},
    )
    data = json.loads((getattr(resp, "text", None) or "[]").strip())
    if isinstance(data, dict):  # be lenient if the model wraps the array
        data = next((v for v in data.values() if isinstance(v, list)), [])
    out = []
    for d in data if isinstance(data, list) else []:
        ticker = str(d.get("ticker", "")).upper().strip()
        if not ticker:
            continue
        stance = str(d.get("stance", "unknown")).lower().strip()
        out.append(Mention(ticker=ticker, company_name=d.get("company") or None,
                           stance=stance if stance in _STANCES else "unknown", confidence=0.9))
    return out


# --------------------------------------------------------------------------
# Deterministic fallback
# --------------------------------------------------------------------------
def _extract_dictionary(text: str) -> list[Mention]:
    tickers = sec_tickers.ticker_set()
    name_map = sec_tickers.name_to_ticker()
    found: dict[str, Mention] = {}

    for sym in re.findall(r"\$([A-Za-z]{1,5})\b", text):        # $cashtags
        t = sym.upper()
        if t in tickers:
            found.setdefault(t, Mention(ticker=t, confidence=0.6))

    for sym in re.findall(r"\b([A-Z]{2,5})\b", text):           # bare uppercase symbols
        if sym in tickers and sym not in _STOPWORDS:
            found.setdefault(sym, Mention(ticker=sym, confidence=0.5))

    # Multi-word company names only (2–3 word grams). Single-word matching is
    # deliberately skipped: thousands of firms are named after common words
    # ("Bullish", "Honest", "People", "Pattern"), so 1-grams flood the results
    # with false positives. The LLM path handles single-word names in context;
    # this fallback stays high-precision.
    words = re.findall(r"[a-z0-9]+", text.lower())
    for n in (3, 2):
        for i in range(len(words) - n + 1):
            norm = sec_tickers.normalize_name(" ".join(words[i:i + n]))
            if len(norm) < 5:
                continue
            t = name_map.get(norm)
            if t:
                found.setdefault(t, Mention(ticker=t, company_name=norm.title(), confidence=0.55))
    return list(found.values())


# --------------------------------------------------------------------------
# Public
# --------------------------------------------------------------------------
def _validate(mentions: list[Mention]) -> list[Mention]:
    """Keep only real, listed tickers; dedupe (first mention wins)."""
    real = sec_tickers.ticker_set()
    out, seen = [], set()
    for m in mentions:
        t = m.ticker.upper().strip()
        if t in real and t not in seen:
            seen.add(t)
            m.ticker = t
            out.append(m)
    return out


def extract_mentions(text: str, *, use_llm: bool = True) -> list[Mention]:
    """Tickers discussed in `text`, validated against the SEC list. LLM primary
    (if configured), deterministic dictionary otherwise or on LLM failure."""
    if not text or not text.strip():
        return []
    mentions: list[Mention] = []
    if use_llm and _llm_available():
        try:
            mentions = _extract_llm(text)
        except Exception:
            mentions = []
    if not mentions:
        mentions = _extract_dictionary(text)
    return _validate(mentions)
