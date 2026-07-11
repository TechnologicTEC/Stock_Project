"""
Exercises app/pages/4_news.py via Streamlit's AppTest — catches UI-wiring
mistakes. External calls are mocked; the engine logic is covered separately in
test_news.py / test_earnings.py.
"""
from datetime import date
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from engine import portfolio

NEWS_PAGE_PATH = str(Path(__file__).resolve().parent.parent / "app" / "pages" / "4_news.py")


def _news_item(headline, url):
    return {"headline": headline, "source": "Reuters", "url": url,
            "published_at": "2026-06-30T00:00:00", "summary": None}


def test_news_page_prompts_when_no_ticker_available():
    at = AppTest.from_file(NEWS_PAGE_PATH)
    at.run(timeout=30)

    assert not at.exception
    assert any("type a ticker" in el.value for el in at.info)


def test_news_page_renders_news_view_with_sentiment():
    portfolio.add_holding("AAPL", 10, 150.0, date(2025, 6, 1))

    with patch("engine.news.finnhub_client.get_company_news", return_value=[_news_item("AAPL beats earnings", "http://x/1")]), \
         patch("engine.news.rss_client.get_google_news", return_value=[]), \
         patch("engine.news.sentiment.is_available", return_value=True), \
         patch("engine.news.sentiment.score_text", return_value=0.8):
        at = AppTest.from_file(NEWS_PAGE_PATH)
        at.run(timeout=30)

    assert not at.exception
    metrics = {m.label: m.value for m in at.metric}
    assert "Overall sentiment" in metrics
    assert metrics["Overall sentiment"] == "90/100"  # single headline @ 0.8 -> 0-100 scale, no sign


def test_news_page_earnings_view_shows_beat():
    portfolio.add_holding("AAPL", 10, 150.0, date(2025, 6, 1))
    raw = {"earningsCalendar": [
        {"date": "2026-05-01", "epsActual": 1.2, "epsEstimate": 1.1, "revenueActual": 1, "revenueEstimate": 1},
    ]}

    # The initial render runs the default News view, so those sources need mocking too.
    with patch("engine.news.finnhub_client.get_company_news", return_value=[]), \
         patch("engine.news.rss_client.get_google_news", return_value=[]), \
         patch("engine.news.sentiment.is_available", return_value=False), \
         patch("engine.earnings.finnhub_client.get_earnings_calendar", return_value=raw), \
         patch("engine.earnings.edgar_client.get_cik_for_ticker", return_value=None):
        at = AppTest.from_file(NEWS_PAGE_PATH)
        at.run(timeout=30)

        next(r for r in at.radio if r.label == "View").set_value("📈 Earnings")
        at.run(timeout=30)

    assert not at.exception
    labels = {m.label for m in at.metric}
    assert "Latest EPS (actual)" in labels
    assert "Result" in labels


def test_news_page_earnings_release_shows_highlights_not_a_raw_block():
    portfolio.add_holding("AAPL", 10, 150.0, date(2025, 6, 1))
    raw = {"earningsCalendar": [
        {"date": "2026-05-01", "epsActual": 1.2, "epsEstimate": 1.1, "revenueActual": 1, "revenueEstimate": 1},
    ]}
    release = {"filing_date": "2026-05-02", "url": "https://sec.gov/x",
               "text": ("Total revenue was $1.25 billion, up 18% year over year. "
                        "Net income was $210 million, or $0.85 per diluted share. "
                        "About the company: it builds widgets worldwide.")}

    with patch("engine.news.finnhub_client.get_company_news", return_value=[]), \
         patch("engine.news.rss_client.get_google_news", return_value=[]), \
         patch("engine.news.sentiment.is_available", return_value=False), \
         patch("engine.earnings.finnhub_client.get_earnings_calendar", return_value=raw), \
         patch("engine.earnings.get_press_release", return_value=release):
        at = AppTest.from_file(NEWS_PAGE_PATH)
        at.run(timeout=30)
        next(r for r in at.radio if r.label == "View").set_value("📈 Earnings")
        at.run(timeout=30)

    assert not at.exception
    # The highlights block is the markdown item right after the "Key figures" header.
    values = [m.value for m in at.markdown]
    header_i = next(i for i, v in enumerate(values) if "Key figures from the release" in v)
    highlights_md = values[header_i + 1]
    assert "1.25 billion" in highlights_md and "18%" in highlights_md   # a highlight rendered
    assert r"\$1.25 billion" in highlights_md                          # $ escaped, won't LaTeX-garble
    assert "About the company" not in highlights_md                    # boilerplate excluded from highlights
    # ...but the full text (with the boilerplate) is still available in the expander.
    assert any("About the company" in v for v in values)
