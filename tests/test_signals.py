"""
engine/signals.py — the cross-signal agreement summary. Every underlying engine
is mocked; this checks the stance mapping and the positive/neutral/negative tally
(and that 'n/a' reads are excluded from the count).
"""
from types import SimpleNamespace
from unittest.mock import patch

from engine import signals


def _screener(score, reco):
    return [SimpleNamespace(ticker="NVDA", overall_score=score, recommendation=reco)]


def _news(score):
    return SimpleNamespace(overall_score=score)


def _earn(beat, pct=None):
    return SimpleNamespace(latest=({"beat": beat, "eps_surprise_pct": pct} if beat is not None else None))


def _run(screener=None, news=None, earn=None, stance=None):
    with patch("engine.screener.screen_tickers", return_value=screener if screener is not None else []), \
         patch("engine.news.analyze_ticker", return_value=news if news is not None else _news(None)), \
         patch("engine.earnings.analyze_ticker", return_value=earn if earn is not None else _earn(None)), \
         patch("engine.creator_signals.ticker_stance", return_value=stance):
        return signals.aggregate_signals("nvda")


def _read(summary, name):
    return next(r for r in summary["reads"] if r.name == name)


def test_all_positive_signals_tally_and_map_correctly():
    out = _run(screener=_screener(80.0, "Strong Buy"), news=_news(70), earn=_earn(True, 12.0),
               stance={"mentions": 3, "counts": {"bullish": 3, "bearish": 0, "neutral": 0}, "stance": "bullish"})
    assert (out["positive"], out["neutral"], out["negative"], out["counted"]) == (4, 0, 0, 4)
    assert _read(out, "Screener").detail == "Strong Buy · 80/100"
    assert _read(out, "Latest earnings").detail == "Beat estimates by 12%"
    assert _read(out, "Creator mentions").stance == "positive"


def test_negative_and_mixed_mapping():
    out = _run(screener=_screener(20.0, "Sell"), news=_news(38), earn=_earn(False, 8.0),
               stance={"mentions": 2, "counts": {"bullish": 0, "bearish": 2, "neutral": 0}, "stance": "bearish"})
    assert (out["positive"], out["negative"], out["counted"]) == (0, 4, 4)
    assert _read(out, "News sentiment").stance == "negative"
    assert _read(out, "Latest earnings").detail == "Missed estimates by 8%"


def test_hold_and_neutral_news_are_neutral_not_counted_as_lean():
    out = _run(screener=_screener(50.0, "Hold"), news=_news(50))
    assert _read(out, "Screener").stance == "neutral"
    assert _read(out, "News sentiment").stance == "neutral"


def test_missing_data_is_na_and_excluded_from_the_count():
    # no screener result, no news score, no earnings, not mentioned by any creator
    out = _run(screener=[], news=_news(None), earn=_earn(None), stance=None)
    assert out["counted"] == 0
    assert all(r.stance == "n/a" for r in out["reads"])
    assert _read(out, "Creator mentions").detail == "not mentioned recently"


def test_partial_data_counts_only_what_exists():
    out = _run(screener=_screener(72.0, "Buy"), news=_news(None), earn=_earn(None), stance=None)
    assert out["counted"] == 1 and out["positive"] == 1
    assert _read(out, "News sentiment").stance == "n/a"


def test_a_failing_engine_degrades_to_na_not_a_crash():
    with patch("engine.screener.screen_tickers", side_effect=RuntimeError("boom")), \
         patch("engine.news.analyze_ticker", side_effect=RuntimeError("boom")), \
         patch("engine.earnings.analyze_ticker", side_effect=RuntimeError("boom")), \
         patch("engine.creator_signals.ticker_stance", return_value=None):
        out = signals.aggregate_signals("NVDA")
    assert out["counted"] == 0
    assert _read(out, "Screener").detail == "unavailable"
