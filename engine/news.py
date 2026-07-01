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

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from statistics import mean

from engine import cache, sentiment
from engine.data_sources import finnhub_client, rss_client
from engine.time_utils import utcnow

NEWS_TTL_SECONDS = 6 * 60 * 60      # news moves slower than quotes; refresh a few times a day
NEWS_DEFAULT_DAYS = 7               # Finnhub company-news lookback window
NEWS_DISPLAY_LIMIT = 40

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

    known_urls = {row["url"] for row in cache.get_cached_news(ticker, limit=1000)}
    fresh: dict[str, dict] = {}  # de-dupe within this fetch by URL
    for article in _fetch_all_sources(ticker, days):
        url = article.get("url")
        if url and url not in known_urls and url not in fresh:
            fresh[url] = article

    scored = _score_articles(list(fresh.values()))
    for article in scored:
        article["published_at"] = _to_datetime(article.get("published_at"))

    added = cache.save_news_articles(ticker, scored)
    cache.mark_news_fetched(ticker)  # marks *when we last asked*, even if nothing new came back
    return added


def _build_summary(ticker: str, total: int, scored: int, overall: int | None,
                   positive: int, neutral: int, negative: int) -> str:
    if total == 0:
        return f"No recent news found for {ticker}."
    if scored == 0:
        return (f"{total} recent headline(s) for {ticker}. Sentiment scoring is unavailable — "
                "the FinBERT model isn't installed (`pip install transformers torch`).")
    return (f"{total} recent headline(s) for {ticker} — overall sentiment {overall}/100 "
            f"({positive} positive, {neutral} neutral, {negative} negative).")


def analyze_ticker(ticker: str, days: int = NEWS_DEFAULT_DAYS,
                   limit: int = NEWS_DISPLAY_LIMIT, force: bool = False) -> NewsAnalysis:
    """Fetch-if-stale, then summarize the cached headlines for `ticker`."""
    ticker = ticker.upper()
    ensure_fresh(ticker, days=days, force=force)

    rows = cache.get_cached_news(ticker, limit=limit)
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
        summary=_build_summary(ticker, len(rows), len(scores), overall, positive, neutral, negative),
        has_sentiment=len(scores) > 0,
    )
