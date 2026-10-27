"""
AI News Analyzer (Section 6.2). Pulls recent headlines per ticker from Finnhub
+ Google News RSS, scores each with FinBERT (engine/sentiment.py), and rolls
them up into an overall sentiment score + a template summary.

Caching (Section 5's rule — pages never hit an API directly): headlines live in
the `news_cache` table, deduped by URL, with a per-ticker staleness marker in
engine/cache.py. External sources are only called when the cache is stale, so a
page reload is free. Sentiment is scored once, at fetch time, and stored on the
row — so rendering never reloads the model.

Degrades gracefully: if a source is down it's skipped (the other still fills in),
and if the FinBERT deps aren't installed the headlines still show, just without
scores.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from statistics import mean

from engine import cache, sentiment
from engine.data_sources import finnhub_client, rss_client
from engine.time_utils import utcnow

NEWS_TTL_SECONDS = 6 * 60 * 60      # news moves slower than quotes; refresh a few times a day
NEWS_DEFAULT_DAYS = 7               # Finnhub company-news lookback window
NEWS_DISPLAY_LIMIT = 40
# Headlines older than this, outside a ticker's newest NEWS_DISPLAY_LIMIT, are
# pruned nightly (cache.prune_news via scripts/warm_cache.py) — and not stored in
# the first place (ensure_fresh), since Google News returns stories of any age.
NEWS_KEEP_DAYS = 10

# Relevance. A heavily covered name's feed is mostly general market stories (in
# Oct 2026 only 6 of NVDA's newest 40 named Nvidia), so the score reads the
# headlines that name the company — when at least MIN_RELEVANT do. Below that it
# reads them all, as it always did: a name whose headlines rarely spell it out
# keeps its sentiment factor rather than losing it.
MIN_RELEVANT = 5
_RELEVANCE_POOL = 400        # newest rows considered; NVDA had ~1,200 in ten days
# First words too common to identify a company on their own; for these the
# first two words must appear together ("Best Buy", not every "best").
_COMMON_FIRST_WORDS = frozenset("""
    advanced air american applied atlantic bank best capital central core digital
    eastern energy federal first general global great healthcare home insight
    international live match national new news northern old pacific public
    realty royal southern standard state stock target texas trade union united
    universal waste western world
""".split())
_NAME_SUFFIXES = re.compile(
    r"\b(inc|incorporated|corp|corporation|co|company|companies|holdings?|group|plc|ltd|"
    r"limited|llc|lp|n\.?v|s\.?a|se|ag|the|class [a-c]|common stock|& co)\b\.?", re.I)

# A headline counts as positive/negative only past a small band around zero, so
# faintly-signed neutral scores don't inflate the positive/negative tallies.
_POSITIVE_THRESHOLD = 0.15
_NEGATIVE_THRESHOLD = -0.15


@dataclass
class NewsAnalysis:
    ticker: str
    headlines: list[dict]                 # newest first; each has a "sentiment_label"
    overall_score: int | None             # 0-100 display score (50 = neutral); None if nothing scored
    positive: int = 0
    neutral: int = 0
    negative: int = 0
    scored_count: int = 0
    total_count: int = 0
    summary: str = ""
    has_sentiment: bool = False           # were any headlines actually scored?
    unrelated_count: int = 0              # headlines left out for not naming the company


def sentiment_label(score: float | None) -> str:
    if score is None:
        return "—"
    if score >= _POSITIVE_THRESHOLD:
        return "Positive"
    if score <= _NEGATIVE_THRESHOLD:
        return "Negative"
    return "Neutral"


def scale_to_100(mean_score: float) -> int:
    """Map a mean sentiment scalar in [-1, 1] to a friendlier 0-100 display
    score: 0 = extremely negative, 50 = neutral, 100 = extremely positive.

    FinBERT's underlying score is *signed* (P(pos) − P(neg), centred on 0), which
    is why raw values go negative. This only changes how the single *overall*
    number is presented — the per-headline Positive/Neutral/Negative labels are
    unaffected. Headline sentiment is usually mild, so most values land near 50."""
    return round((mean_score + 1) / 2 * 100)


def _fetch_all_sources(ticker: str, days: int) -> list[dict]:
    """Merge Finnhub + Google News headlines. One source failing (rate limit,
    outage) doesn't sink the other."""
    to_date = date.today()
    from_date = to_date - timedelta(days=days)

    articles: list[dict] = []
    try:
        articles += finnhub_client.get_company_news(ticker, from_date, to_date)
    except Exception:
        pass
    try:
        articles += rss_client.get_google_news(ticker)
    except Exception:
        pass
    return articles


def _title_key(headline: str) -> str:
    """A normalized headline used to spot the *same story* arriving from different
    feeds under different URLs (e.g. Finnhub's 'Yahoo' vs Google RSS's 'Yahoo
    Finance'). Lowercased, punctuation stripped, whitespace collapsed."""
    return re.sub(r"[^a-z0-9]+", " ", (headline or "").lower()).strip()


def _to_datetime(value) -> datetime:
    """news_cache stores published_at as a datetime; the source clients hand us
    ISO strings. Convert, falling back to now() for anything unparseable."""
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return utcnow()


def _score_articles(articles: list[dict]) -> list[dict]:
    """Attach a sentiment_score (in [-1, 1], or None) to each article. Checks
    model availability once so a missing FinBERT install is a fast no-op rather
    than an exception per headline."""
    model_ready = sentiment.is_available()
    for article in articles:
        if not model_ready:
            article["sentiment_score"] = None
            continue
        try:
            article["sentiment_score"] = sentiment.score_text(article.get("headline") or "")
        except Exception:
            article["sentiment_score"] = None
    return articles


def ensure_fresh(ticker: str, days: int = NEWS_DEFAULT_DAYS, force: bool = False) -> int:
    """Refresh the cache for `ticker` if it's stale (or `force`d). Fetches from
    both sources, scores only headlines we haven't seen before, and stores them.
    Returns the number of new headlines added."""
    ticker = ticker.upper()
    if not force and not cache.is_news_stale(ticker, NEWS_TTL_SECONDS):
        return 0

    cached = cache.get_cached_news(ticker, limit=1000)
    known_urls = {row["url"] for row in cached}
    seen_titles = {_title_key(row.get("headline")) for row in cached}
    fresh: dict[str, dict] = {}  # de-dupe within this fetch by URL *and* title
    for article in _fetch_all_sources(ticker, days):
        url = article.get("url")
        title = _title_key(article.get("headline"))
        if not url or url in known_urls or url in fresh:
            continue
        if title and title in seen_titles:
            continue  # same story from another feed under a different URL
        seen_titles.add(title)
        fresh[url] = article

    fresh = _drop_prunable(fresh, cached)
    scored = _score_articles(list(fresh.values()))
    for article in scored:
        article["published_at"] = _to_datetime(article.get("published_at"))

    added = cache.save_news_articles(ticker, scored)
    cache.mark_news_fetched(ticker)  # marks *when we last asked*, even if nothing new came back
    return added


def _drop_prunable(fresh: dict[str, dict], cached: list[dict]) -> dict[str, dict]:
    """Leave out what tonight's prune would delete: older than NEWS_KEEP_DAYS AND
    older than the ticker's NEWS_DISPLAY_LIMIT-th newest stored headline.

    Google News RSS has no date window (stories from 2015 arrive), so without
    this a pruned story came straight back, was FinBERT-scored again, and was
    pruned again. Same rule as cache.prune_news, so nothing the sentiment score
    could read is ever skipped: with fewer than NEWS_DISPLAY_LIMIT stored, or a
    story newer than the limit-th, it is kept.
    """
    if len(cached) < NEWS_DISPLAY_LIMIT:
        return fresh
    floor = sorted((_naive_utc(r.get("published_at")) for r in cached),
                   reverse=True)[NEWS_DISPLAY_LIMIT - 1]
    cutoff = min(floor, utcnow() - timedelta(days=NEWS_KEEP_DAYS))
    return {url: a for url, a in fresh.items()
            if _naive_utc(a.get("published_at")) >= cutoff}


def _naive_utc(value) -> datetime:
    """A comparable instant. Google News dates arrive timezone-aware
    ('...+00:00') and stored ones come back naive UTC; comparing the two raises."""
    dt = _to_datetime(value)
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _name_phrase(company_name: str | None) -> str | None:
    """The part of a company name a headline would use: "Nvidia" from "NVIDIA
    Corp", "Best Buy" from "Best Buy Co Inc" (its first word is too common)."""
    words = _NAME_SUFFIXES.sub(" ", company_name or "").replace(",", " ").split()
    if not words:
        return None
    if len(words[0]) >= 4 and words[0].lower() not in _COMMON_FIRST_WORDS:
        return words[0]
    return " ".join(words[:2]) if len(words) >= 2 else None


def names_company(headline: str, ticker: str, company_name: str | None) -> bool:
    """Does `headline` name this company — by its symbol or its name?

    The symbol must appear as an upper-case word ("NVDA", "$NVDA") and be at
    least two letters: "A" (Agilent) is a word in half of all headlines. A
    class share also matches with a dot or without its class ("BRK.B", "BRK").
    """
    text = headline or ""
    symbols = {ticker, ticker.replace("-", "."), ticker.split("-")[0]}
    if any(len(sym) >= 2 and re.search(rf"(?<![A-Za-z]){re.escape(sym)}(?![A-Za-z])", text)
           for sym in symbols):
        return True
    phrase = _name_phrase(company_name)
    return bool(phrase and re.search(rf"\b{re.escape(phrase)}\b", text, re.I))


def _company_name(ticker: str) -> str | None:
    """The company name from the cached profile the screener already fetched —
    a database read, never a network call, so every page agrees with the
    screener about which headlines count."""
    try:
        return (cache.get_value(f"profile:{ticker}") or {}).get("name")
    except Exception:                      # noqa: BLE001 — relevance is a refinement
        return None


def _scored_rows(ticker: str, limit: int) -> tuple[list[dict], int]:
    """(the headlines the score reads, how many were left out as unrelated).

    Drawn only from rows the nightly prune keeps (each ticker's newest
    NEWS_DISPLAY_LIMIT, plus anything under NEWS_KEEP_DAYS old — see
    cache.prune_news), so the prune can never change a score by deleting a row
    it reads.
    """
    rows = cache.get_cached_news(ticker, limit=max(limit, _RELEVANCE_POOL))
    cutoff = utcnow() - timedelta(days=NEWS_KEEP_DAYS)
    pool = [r for i, r in enumerate(rows)
            if i < NEWS_DISPLAY_LIMIT or (r.get("published_at") and r["published_at"] >= cutoff)]
    name = _company_name(ticker)
    relevant = [r for r in pool if names_company(r.get("headline"), ticker, name)]
    if len(relevant) < MIN_RELEVANT:
        return pool[:limit], 0
    return relevant[:limit], len(pool) - len(relevant)


def _build_summary(ticker: str, total: int, scored: int, overall: int | None,
                   positive: int, neutral: int, negative: int, unrelated: int = 0) -> str:
    if total == 0:
        return f"No recent news found for {ticker}."
    if scored == 0:
        return (f"{total} recent headline(s) for {ticker}. Sentiment scoring is unavailable — "
                "the FinBERT model isn't installed (`pip install transformers torch`).")
    left_out = (f" {unrelated} other headline(s) in the feed didn't name the company and "
                "were left out." if unrelated else "")
    return (f"{total} recent headline(s) for {ticker} — overall sentiment {overall}/100 "
            f"({positive} positive, {neutral} neutral, {negative} negative).{left_out}")


def analyze_ticker(ticker: str, days: int = NEWS_DEFAULT_DAYS,
                   limit: int = NEWS_DISPLAY_LIMIT, force: bool = False) -> NewsAnalysis:
    """Fetch-if-stale, then summarize the cached headlines for `ticker` — the
    newest `limit` that name the company, where enough do (see _scored_rows)."""
    ticker = ticker.upper()
    ensure_fresh(ticker, days=days, force=force)

    rows, unrelated = _scored_rows(ticker, limit)
    for row in rows:
        row["sentiment_label"] = sentiment_label(row.get("sentiment_score"))

    scores = [r["sentiment_score"] for r in rows if r.get("sentiment_score") is not None]
    overall = scale_to_100(mean(scores)) if scores else None
    positive = sum(1 for s in scores if s >= _POSITIVE_THRESHOLD)
    negative = sum(1 for s in scores if s <= _NEGATIVE_THRESHOLD)
    neutral = len(scores) - positive - negative

    return NewsAnalysis(
        ticker=ticker,
        headlines=rows,
        overall_score=overall,
        positive=positive,
        neutral=neutral,
        negative=negative,
        scored_count=len(scores),
        total_count=len(rows),
        summary=_build_summary(ticker, len(rows), len(scores), overall, positive, neutral,
                               negative, unrelated),
        has_sentiment=len(scores) > 0,
        unrelated_count=unrelated,
    )
