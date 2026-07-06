from unittest.mock import patch

from engine import news


def _article(headline, url, source="Reuters", when="2026-06-30T00:00:00"):
    return {"headline": headline, "source": source, "url": url, "published_at": when, "summary": None}


def _no_model():
    return patch("engine.news.sentiment.is_available", return_value=False)


# --------------------------------------------------------------------------
# sentiment_label thresholds
# --------------------------------------------------------------------------

def test_sentiment_label_thresholds():
    assert news.sentiment_label(0.5) == "Positive"
    assert news.sentiment_label(0.15) == "Positive"   # boundary is inclusive
    assert news.sentiment_label(0.0) == "Neutral"
    assert news.sentiment_label(-0.15) == "Negative"
    assert news.sentiment_label(None) == "—"


# --------------------------------------------------------------------------
# scale_to_100 — signed [-1, 1] mean -> friendly 0-100 (50 = neutral)
# --------------------------------------------------------------------------

def test_scale_to_100_maps_signed_score_onto_zero_to_hundred():
    assert news.scale_to_100(-1.0) == 0     # extremely negative
    assert news.scale_to_100(0.0) == 50     # neutral
    assert news.scale_to_100(1.0) == 100    # extremely positive
    assert news.scale_to_100(-0.08) == 46   # a slightly-negative day reads just below neutral
    assert news.scale_to_100(0.8) == 90


# --------------------------------------------------------------------------
# analyze_ticker — fetch, score, aggregate, summarize
# --------------------------------------------------------------------------

def test_analyze_ticker_scores_and_summarizes():
    finnhub_items = [_article("AAPL beats earnings", "http://x/1")]
    rss_items = [_article("AAPL faces lawsuit", "http://x/2", source="Bloomberg")]

    with patch("engine.news.finnhub_client.get_company_news", return_value=finnhub_items), \
         patch("engine.news.rss_client.get_google_news", return_value=rss_items), \
         patch("engine.news.sentiment.is_available", return_value=True), \
         patch("engine.news.sentiment.score_text", side_effect=lambda t: 0.8 if "beats" in t else -0.4):
        analysis = news.analyze_ticker("aapl")

    assert analysis.total_count == 2
    assert analysis.positive == 1 and analysis.negative == 1 and analysis.neutral == 0
    # mean(0.8, -0.4) = 0.2 -> 0-100 scale (50 = neutral): (0.2 + 1) / 2 * 100 = 60
    assert analysis.overall_score == 60
    assert "overall sentiment 60/100" in analysis.summary
    labels = {h["headline"]: h["sentiment_label"] for h in analysis.headlines}
    assert labels["AAPL beats earnings"] == "Positive"
    assert labels["AAPL faces lawsuit"] == "Negative"


def test_analyze_ticker_merges_both_sources_and_dedupes_by_url():
    shared = _article("Same story", "http://x/DUP")
    with patch("engine.news.finnhub_client.get_company_news", return_value=[shared]), \
         patch("engine.news.rss_client.get_google_news", return_value=[shared]), \
         _no_model():
        analysis = news.analyze_ticker("AAPL", force=True)

    assert analysis.total_count == 1  # the duplicate URL is collapsed


def test_analyze_ticker_dedupes_same_story_across_feeds_by_title():
    # The same story from two feeds has *different* URLs and source labels
    # ("Yahoo" vs "Yahoo Finance") — URL dedup misses it, title dedup collapses it
    # (even across whitespace/punctuation differences).
    finnhub = [_article("ASML A Top AI Stock to Buy", "http://finnhub/1", source="Yahoo")]
    rss = [_article("ASML  a Top AI Stock to Buy!", "http://google/rss/2", source="Yahoo Finance")]
    with patch("engine.news.finnhub_client.get_company_news", return_value=finnhub), \
         patch("engine.news.rss_client.get_google_news", return_value=rss), \
         _no_model():
        analysis = news.analyze_ticker("ASML", force=True)

    assert analysis.total_count == 1


def test_analyze_ticker_survives_one_source_failing():
    with patch("engine.news.finnhub_client.get_company_news", side_effect=RuntimeError("rate limited")), \
         patch("engine.news.rss_client.get_google_news", return_value=[_article("From RSS", "http://x/9")]), \
         _no_model():
        analysis = news.analyze_ticker("AAPL")

    assert analysis.total_count == 1
    assert analysis.headlines[0]["headline"] == "From RSS"


def test_analyze_ticker_without_model_shows_headlines_but_no_scores():
    with patch("engine.news.finnhub_client.get_company_news", return_value=[_article("h", "http://x/1")]), \
         patch("engine.news.rss_client.get_google_news", return_value=[]), \
         _no_model():
        analysis = news.analyze_ticker("AAPL")

    assert analysis.total_count == 1
    assert analysis.scored_count == 0
    assert analysis.has_sentiment is False
    assert analysis.overall_score is None
    assert "unavailable" in analysis.summary.lower()


def test_analyze_ticker_no_news():
    with patch("engine.news.finnhub_client.get_company_news", return_value=[]), \
         patch("engine.news.rss_client.get_google_news", return_value=[]), \
         _no_model():
        analysis = news.analyze_ticker("ZZZZ")

    assert analysis.total_count == 0
    assert "No recent news" in analysis.summary


# --------------------------------------------------------------------------
# Caching — only hit the sources when stale
# --------------------------------------------------------------------------

def test_ensure_fresh_only_fetches_when_stale():
    calls = {"n": 0}

    def counting_fetch(ticker, from_date, to_date):
        calls["n"] += 1
        return [_article("h", "http://x/1")]

    with patch("engine.news.finnhub_client.get_company_news", side_effect=counting_fetch), \
         patch("engine.news.rss_client.get_google_news", return_value=[]), \
         _no_model():
        news.ensure_fresh("AAPL")
        news.ensure_fresh("AAPL")  # cache is warm now

    assert calls["n"] == 1
