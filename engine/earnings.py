"""
Earnings Analyzer (Section 6.5). Two free sources, combined:

- **Finnhub earnings calendar** → the beat/miss numbers (EPS actual vs. estimate,
  revenue actual vs. estimate) for recent quarters.
- **SEC EDGAR 8-K / EX-99.1** → the raw earnings press release text, run through
  the same FinBERT pipeline as the News Analyzer (Section 6.2) for an "AI
  summary" sentiment read.

Both go through engine/cache.py's generic TTL cache (they have no structured
table of their own), so a page reload doesn't re-hit Finnhub/SEC or re-score the
release. Everything degrades gracefully: no press release (not every company
files an EX-99.1), no earnings data, or no sentiment model each just leaves that
part of the report empty rather than erroring.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from engine import cache, news, sentiment
from engine.data_sources import edgar_client, finnhub_client

EARNINGS_TTL_SECONDS = 24 * 60 * 60      # earnings data only changes quarterly; a daily refresh is ample
_SURPRISE_LOOKBACK_DAYS = 450            # ~5 quarters, enough to show a short history


@dataclass
class EarningsAnalysis:
    ticker: str
    surprises: list[dict]           # newest first: EPS/revenue actual vs. estimate per quarter
    latest: dict | None             # the most recent reported quarter
    release: dict | None            # {filing_date, url, text, sentiment_score, sentiment_label}
    summary: str
    has_release: bool = False


# --------------------------------------------------------------------------
# Earnings surprises (Finnhub)
# --------------------------------------------------------------------------

def _fetch_surprises(ticker: str) -> list[dict]:
    to_date = date.today()
    from_date = to_date - timedelta(days=_SURPRISE_LOOKBACK_DAYS)
    raw = finnhub_client.get_earnings_calendar(ticker, from_date, to_date)

    rows = []
    for item in raw.get("earningsCalendar", []):
        eps_actual, eps_estimate = item.get("epsActual"), item.get("epsEstimate")
        if eps_actual is None:
            continue  # not reported yet — skip future/estimate-only rows
        surprise = eps_actual - eps_estimate if eps_estimate is not None else None
        surprise_pct = (surprise / abs(eps_estimate) * 100) if surprise is not None and eps_estimate else None
        rows.append({
            "period": item.get("date"),
            "eps_actual": eps_actual,
            "eps_estimate": eps_estimate,
            "eps_surprise": round(surprise, 4) if surprise is not None else None,
            "eps_surprise_pct": round(surprise_pct, 1) if surprise_pct is not None else None,
            "revenue_actual": item.get("revenueActual"),
            "revenue_estimate": item.get("revenueEstimate"),
            "beat": (eps_actual > eps_estimate) if eps_estimate is not None else None,
        })
    rows.sort(key=lambda r: r["period"] or "", reverse=True)
    return rows


UPCOMING_LOOKAHEAD_DAYS = 120       # how far ahead to look for the next report


def _fetch_upcoming(ticker: str) -> list[dict]:
    today = date.today()
    raw = finnhub_client.get_earnings_calendar(ticker, today, today + timedelta(days=UPCOMING_LOOKAHEAD_DAYS))
    rows = []
    for item in raw.get("earningsCalendar", []):
        day = item.get("date")
        if not day or item.get("epsActual") is not None:
            continue  # only future / not-yet-reported dates (free tier gives these even without actuals)
        rows.append({"date": day, "eps_estimate": item.get("epsEstimate"), "hour": item.get("hour")})
    rows.sort(key=lambda r: r["date"])
    return rows


def next_earnings(ticker: str) -> dict | None:
    """The soonest upcoming earnings report for `ticker`: {date, eps_estimate,
    hour, days_until}, or None. Finnhub's free tier withholds past *actuals* but
    still returns forward dates, so this works where beat/miss doesn't."""
    ticker = ticker.upper()
    try:
        rows = cache.get_or_fetch(f"earnings_upcoming:{ticker}", EARNINGS_TTL_SECONDS,
                                  lambda: _fetch_upcoming(ticker))
    except Exception:
        return None
    if not rows:
        return None
    nxt = rows[0]
    try:
        when = date.fromisoformat(nxt["date"])
    except (TypeError, ValueError):
        return None
    return {"date": nxt["date"], "eps_estimate": nxt.get("eps_estimate"),
            "hour": nxt.get("hour"), "days_until": (when - date.today()).days}


def get_surprises(ticker: str) -> list[dict]:
    ticker = ticker.upper()
    try:
        return cache.get_or_fetch(
            f"earnings_surprises:{ticker}", EARNINGS_TTL_SECONDS, lambda: _fetch_surprises(ticker)
        )
    except Exception:
        return []  # earnings calendar can 403 on some plans / fail — don't sink the page


# --------------------------------------------------------------------------
# Earnings press release (SEC EDGAR 8-K EX-99.1) + its sentiment
# --------------------------------------------------------------------------

def _fetch_press_release(ticker: str) -> dict | None:
    cik = edgar_client.get_cik_for_ticker(ticker)
    if cik is None:
        return None  # not a US filer (Section 2: international is paid-only)
    release = edgar_client.get_8k_press_release(cik)
    if release is None:
        return None
    score = None
    if sentiment.is_available():
        try:
            score = sentiment.score_text(release.get("text") or "")
        except Exception:
            score = None
    release["sentiment_score"] = score
    return release


def get_press_release(ticker: str) -> dict | None:
    ticker = ticker.upper()
    try:
        return cache.get_or_fetch(
            f"earnings_release:{ticker}", EARNINGS_TTL_SECONDS, lambda: _fetch_press_release(ticker)
        )
    except Exception:
        return None


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def _describe_surprise(latest: dict | None) -> str:
    if not latest or latest.get("beat") is None:
        return ""
    verb = "beat" if latest["beat"] else ("met" if latest["eps_surprise"] == 0 else "missed")
    pct = latest.get("eps_surprise_pct")
    by = f" by {abs(pct):.1f}%" if pct else ""
    return (f"Latest quarter ({latest['period']}): EPS ${latest['eps_actual']:.2f} vs "
            f"${latest['eps_estimate']:.2f} estimate — {verb}{by}.")


# --------------------------------------------------------------------------
# Press-release presentation: turn the raw 8-K text blob into (a) a few
# key-figure highlight sentences and (b) readable paragraphs. Deterministic —
# it only re-uses sentences the company actually wrote, never invents numbers.
# --------------------------------------------------------------------------

_FIN_KEYWORDS = (
    "revenue", "net income", "net loss", "earnings", "eps", "per share", "operating income",
    "operating margin", "gross margin", "guidance", "outlook", "cash flow", "free cash",
    "dividend", "full year", "full-year", "quarter", "grew", "growth", "increased",
    "decreased", "declined", "up ", "down ", "raised", "lowered", "record", "backlog",
)
# A dollar amount, a percentage, or a scaled figure — the mark of a substantive line.
_FIGURE_RE = re.compile(r"\$\s?\d|\b\d[\d,]*\.?\d*\s?(?:%|percent|million|billion|bps)\b", re.I)
# Split on sentence boundaries but NOT on the '.' inside "$1.25" (period + digit).
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"'])")


def press_release_highlights(text: str | None, limit: int = 6) -> list[str]:
    """The most informative sentences from a press release — those pairing a real
    figure ($ / % / million-billion) with financial context. Fluff and boilerplate
    ("About the Company", CEO platitudes with no numbers) are dropped. Sentences
    are returned in their original order, best-scoring first up to `limit`."""
    flat = re.sub(r"\s+", " ", text or "").strip()
    if not flat:
        return []
    scored, seen = [], set()
    for i, raw in enumerate(_SENTENCE_SPLIT.split(flat)):
        sentence = raw.strip()
        if not (40 <= len(sentence) <= 320) or not _FIGURE_RE.search(sentence):
            continue
        keyword_hits = sum(k in sentence.lower() for k in _FIN_KEYWORDS)
        if not keyword_hits:
            continue
        dedupe_key = sentence.lower()[:64]
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        scored.append((i, keyword_hits, sentence))
    best = sorted(scored, key=lambda t: -t[1])[:limit]
    return [s for _, _, s in sorted(best, key=lambda t: t[0])]  # back to reading order


def format_release_body(text: str | None) -> str:
    """Raw press-release text as wrapping markdown paragraphs (not a monospace
    wall). `$` is escaped so figures like `$1.2 billion` don't trip Streamlit's
    `$…$` LaTeX math rendering."""
    if not text or not text.strip():
        return ""
    paragraphs = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", text)]
    paragraphs = [p for p in paragraphs if p] or [re.sub(r"\s+", " ", text).strip()]
    return "\n\n".join(p.replace("$", r"\$") for p in paragraphs)


def _describe_release(release: dict | None) -> str:
    if not release:
        return ""
    score = release.get("sentiment_score")
    if score is None:
        return " An 8-K press release is available (sentiment scoring unavailable)."
    return f" Press-release sentiment: {news.sentiment_label(score)} ({round(score * 100):+d}/100)."


def analyze_ticker(ticker: str) -> EarningsAnalysis:
    ticker = ticker.upper()
    surprises = get_surprises(ticker)
    release = get_press_release(ticker)
    if release is not None:
        release["sentiment_label"] = news.sentiment_label(release.get("sentiment_score"))
        release["highlights"] = press_release_highlights(release.get("text"))
        # markdown-safe copies for the page ($ escaped so figures don't LaTeX-render)
        release["highlights_md"] = [h.replace("$", r"\$") for h in release["highlights"]]
        release["body_md"] = format_release_body(release.get("text"))

    latest = surprises[0] if surprises else None

    if not surprises and not release:
        summary = f"No earnings data or 8-K press release found for {ticker}."
    else:
        summary = (_describe_surprise(latest) + _describe_release(release)).strip() or \
            f"Earnings data for {ticker}."

    return EarningsAnalysis(
        ticker=ticker,
        surprises=surprises,
        latest=latest,
        release=release,
        summary=summary,
        has_release=release is not None,
    )
