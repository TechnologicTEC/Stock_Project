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
import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from engine.data_sources import sec_tickers

logger = logging.getLogger(__name__)

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
    sponsored: bool = False           # set by engine/sponsorship after extraction


class TransientExtractionError(RuntimeError):
    """The LLM was configured but failed for a *transient* reason — quota, rate
    limit, or the service itself being down (5xx / UNAVAILABLE / timeout).
    Callers should retry later rather than accept the sparse dictionary result
    as final — see engine/creator_signals.py's retry flag."""


# Substrings that mark a retryable LLM failure.
#
# Quota/rate-limit was the original set, borrowed from the chat responder. That
# was too narrow, because the two have opposite consequences: chat falls back,
# shows a note, and the user can just ask again, while extraction runs ONCE per
# video and whatever it stores is permanent — `mentions_extracted_at` is set and
# the video is never revisited.
#
# Gemini returned **503 Service Unavailable** on 2026-09-14. A 503 is the
# textbook retryable error, but it matched nothing here, so the failure was
# treated as permanent: the run fell through to the dictionary, stored six
# mentions with `unknown` stance, and marked the video done. The stance is what
# creator_conviction counts — `unknown` contributes to `mentions` but not to
# `bullish` — so a bullish roundup of six names became six mentions the bot can
# never act on. It also mis-tickered two of them, because captions garble spoken
# symbols ("Fortinet ... FTNT" came through as FT, "Cloudflare ... NET" as NE)
# and both truncations happen to be real tickers of unrelated companies, so SEC
# validation passes them. Reading the NAME in context is exactly what the LLM
# path is for, which is why deferring beats accepting the fallback.
#
# 5xx, UNAVAILABLE, DEADLINE_EXCEEDED and timeouts all mean "the service failed,
# not the request" — retry in six hours and the next scan picks it up.
_TRANSIENT = (
    "429", "resource_exhausted", "quota", "rate limit", "rate-limit",
    "500", "502", "503", "504",
    "unavailable", "overloaded", "internal server error", "internal error",
    "deadline_exceeded", "deadline exceeded", "timeout", "timed out",
    "try again later", "temporarily",
)


def _is_transient(exc: Exception) -> bool:
    blob = f"{type(exc).__name__} {exc}".lower()
    return any(k in blob for k in _TRANSIENT)


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
# Does the ticker belong to the company the speaker named?
#
# SEC validation only proves a ticker is real, not that it is the RIGHT one.
# Gemini reads the company name correctly from context but sometimes gives a
# real ticker of a different company: an audit of all 440 stored mentions on
# 17 Oct 2026 found 7 (Kratos Defense as KOS = Kosmos Energy, Eos Energy as
# EOS = an Eaton Vance fund, Quantinuum as HQI = HireQuest, ...), 3 of them
# counted as bullish votes; captions add more ("Gold Resource ... G O" -> GO =
# Grocery Outlet). So the name is checked against the SEC's own name for the
# ticker, in two layers:
#
# 1. **Free:** if the spoken name doesn't fit the ticker but is EXACTLY another
#    listed company's name, use that company's ticker. Exact only — closest-
#    match lookup "corrected" Navitas to NXTT and Arteris to Carter's.
# 2. **Gemini:** whatever still doesn't fit is put to the model once per video
#    — renames (Raytheon/RTX, Instacart/Maplebear) look like mismatches to a
#    string comparison and are settled here, not dropped.
# --------------------------------------------------------------------------

# A squashed-name similarity this high is the same company spelt differently
# (Vivacore/Vivakor 0.80, Neutra Band/NutriBand 0.84); the wrong tickers in the
# audit topped out at 0.52.
NAME_MATCH_RATIO = 0.75

# Words too common to prove two names are the same company: "Iris Energy" and
# "Eos Energy" share one and nothing else.
_GENERIC = {"energy", "technologies", "technology", "tech", "gold", "mining", "resources",
            "resource", "pharmaceuticals", "pharma", "therapeutics", "systems", "international",
            "financial", "capital", "global", "solutions", "semiconductor", "industries",
            "enterprises", "medical", "health", "healthcare", "bio", "biosciences", "acquisition",
            "fund", "trust", "income", "bank", "motors", "networks", "software", "partners",
            "american", "first", "new", "power", "and", "of"}

_CHECK_PROMPT = (
    "A stock-market video transcript was read for the stocks discussed. For each item "
    "below, the speaker named a company ('spoken'), and the ticker given for it is "
    "registered to the company in 'listed_as'. 'said' holds what the speaker said "
    "around it; trust it over what you remember. Decide whether they are the same "
    "company - counting renames, former names, parent companies, share classes, and a "
    "SPAC the company is merging into, as the same. If they are not, give the correct "
    "US-listed ticker for the company the speaker named, or null if it has none. "
    "Respond with ONLY a JSON array like "
    '[{"ticker":"KOS","same":false,"correct":"KTOS"}] with one entry per item.'
)

# How much of the transcript goes with each doubtful name. Agility Robotics
# (CCXI) is the reason: from memory the model said it had no listing, but the
# creator said "going public through a SPAC, ticker symbol CCXI" two lines on.
_SAID_CHARS = 300
_SAID_HITS = 2


def _said(text: str, m: Mention) -> str:
    """The transcript around the spoken name (or, failing that, the ticker)."""
    if not text:
        return ""
    name = sec_tickers.normalize_name(m.company_name or "").split()
    patterns = ([re.escape(name[0])] if name else []) + [rf"\b{re.escape(m.ticker)}\b"]
    for pattern in patterns:
        hits = list(re.finditer(pattern, text, flags=re.I))[:_SAID_HITS]
        if hits:
            return " ... ".join(text[max(0, h.start() - _SAID_CHARS): h.end() + _SAID_CHARS]
                                for h in hits)
    return ""


def names_agree(spoken: str | None, official: str | None) -> bool:
    """Could `spoken` be the company registered as `official`? Generous on
    purpose: a False here only sends the pair on for a closer look.

    The ticker itself is no evidence: "EOS Energy Enterprises" contains EOS,
    and EOS is an Eaton Vance fund."""
    a, b = sec_tickers.normalize_name(spoken or ""), sec_tickers.normalize_name(official or "")
    if not a or not b:
        return True                      # nothing to compare
    if (set(a.split()) - _GENERIC) & (set(b.split()) - _GENERIC):
        return True
    sa, sb = a.replace(" ", ""), b.replace(" ", "")
    if sa == "".join(w[0] for w in b.split()):
        return True                      # IBM, AMD
    return sa in sb or sb in sa or SequenceMatcher(None, sa, sb).ratio() >= NAME_MATCH_RATIO


def _ask_model_to_check(pairs: list[dict]) -> dict[str, dict]:
    from engine import chat_llm

    # Held in a name for the length of the call — see engine/sponsorship.
    client = chat_llm._client()
    resp = client.models.generate_content(
        model=chat_llm._model(),
        contents=[{"role": "user", "parts": [{"text": (
            f"{_CHECK_PROMPT}\n\nITEMS:\n{json.dumps(pairs)}")}]}],
        config={"response_mime_type": "application/json", "temperature": 0},
    )
    data = json.loads((getattr(resp, "text", None) or "[]").strip())
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), [])
    return {str(d.get("ticker", "")).upper().strip(): d
            for d in (data if isinstance(data, list) else []) if isinstance(d, dict)}


def check_names(mentions: list[Mention], text: str = "") -> list[Mention]:
    """Correct mentions whose ticker belongs to a different company. Never
    drops one. `text` is the transcript, quoted to the model around each
    doubtful name.

    Raises TransientExtractionError if the model check fails for a retryable
    reason, so the video is deferred rather than stored with a known-doubtful
    ticker. Any other model failure keeps the mentions as they are, loudly —
    which is what happened before this check existed.
    """
    real, titles, names = (sec_tickers.ticker_set(), sec_tickers.ticker_to_title(),
                           sec_tickers.name_to_ticker())
    doubtful: list[Mention] = []
    for m in mentions:
        if names_agree(m.company_name, titles.get(m.ticker)):
            continue
        exact = names.get(sec_tickers.normalize_name(m.company_name or ""))
        if exact and exact != m.ticker and exact in real:
            logger.info("Ticker check: %r is %s, not %s (%s)",
                        m.company_name, exact, m.ticker, titles.get(m.ticker))
            m.ticker = exact
        else:
            doubtful.append(m)

    if doubtful and _llm_available():
        try:
            verdicts = _ask_model_to_check([
                {"ticker": m.ticker, "spoken": m.company_name,
                 "listed_as": titles.get(m.ticker), "said": _said(text, m)}
                for m in doubtful])
        except Exception as exc:
            if _is_transient(exc):
                raise TransientExtractionError(str(exc)) from exc
            logger.warning("Ticker check: model failed (%s: %s); keeping %s unchecked",
                           type(exc).__name__, exc, [m.ticker for m in doubtful])
            verdicts = {}
        for m in doubtful:
            v = verdicts.get(m.ticker)
            if v is None or v.get("same") is True:
                continue                 # confirmed, or no answer to act on
            correct = str(v.get("correct") or "").upper().strip()
            # A correction is taken only when the SEC agrees the new ticker is
            # the company the speaker named. The model's memory is older than
            # the listings: replayed over the stored mentions it said SpaceX
            # (SPCX) and Agility Robotics (CCXI, a SPAC) had no listing, and
            # both do. So "not the same" alone never drops or moves a mention —
            # it stays as it was, which is how every mention was kept before.
            if correct and correct != m.ticker and correct in real and names_agree(
                    m.company_name, titles.get(correct)):
                logger.info("Ticker check: %r is %s, not %s (model, SEC name %r)",
                            m.company_name, correct, m.ticker, titles.get(correct))
                m.ticker = correct
            else:
                logger.warning("Ticker check: %r kept as %s (%s); the model doubted it "
                               "but offered no ticker the SEC confirms (%r)",
                               m.company_name, m.ticker, titles.get(m.ticker), correct or None)

    out, seen = [], set()
    for m in mentions:
        if m.ticker not in seen:
            seen.add(m.ticker)
            out.append(m)
    return out


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
    (if configured), deterministic dictionary otherwise. Raises
    TransientExtractionError if the LLM failed for a retryable reason (quota),
    so the caller can defer rather than store the sparse fallback as final."""
    if not text or not text.strip():
        return []
    if use_llm and _llm_available():
        try:
            mentions = _extract_llm(text)
            if mentions:
                return check_names(_validate(mentions), text)
            # LLM ran and found nothing — accept it, but still let the dictionary
            # catch an obvious $cashtag it might have skipped.
        except Exception as exc:
            if _is_transient(exc):
                raise TransientExtractionError(str(exc)) from exc
            # non-transient (e.g. bad SDK) → fall through to the dictionary
    return _validate(_extract_dictionary(text))
