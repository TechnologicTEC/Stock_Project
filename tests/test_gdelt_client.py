from datetime import date
from unittest.mock import patch

import pytest

from engine.data_sources import gdelt_client as gd


# --------------------------------------------------------------------------
# org query term — strip legal suffixes/punctuation so the GDELT LIKE matches
# --------------------------------------------------------------------------

def test_org_query_term_strips_suffixes_and_punctuation():
    # GDELT stores "apple", not "apple inc." — the suffix made the match miss.
    assert gd._org_query_term("Apple Inc.") == "apple"
    assert gd._org_query_term("Advanced Micro Devices, Inc.") == "advanced micro devices"
    assert gd._org_query_term("NVIDIA Corporation") == "nvidia"
    assert gd._org_query_term("The Coca-Cola Company") == "coca cola"
    assert gd._org_query_term("") == ""


# --------------------------------------------------------------------------
# tone -> 0-100 sentiment mapping
# --------------------------------------------------------------------------

def test_tone_to_sentiment_maps_and_clamps():
    assert gd.tone_to_sentiment(0.0) == 50.0       # neutral
    assert gd.tone_to_sentiment(5.0) == 100.0      # strongly positive
    assert gd.tone_to_sentiment(-5.0) == 0.0       # strongly negative
    assert gd.tone_to_sentiment(2.0) == 70.0
    assert gd.tone_to_sentiment(50.0) == 100.0     # clamped
    assert gd.tone_to_sentiment(-50.0) == 0.0      # clamped
    assert gd.tone_to_sentiment(None) is None


# --------------------------------------------------------------------------
# sentiment_as_of — article-count-weighted average over the window
# --------------------------------------------------------------------------

def test_sentiment_as_of_weights_by_article_count():
    daily = [
        {"day": "2023-05-05", "avg_tone": 5.0, "n": 1},    # +5 on 1 article
        {"day": "2023-05-20", "avg_tone": 0.0, "n": 9},    # neutral on 9 articles
    ]
    # weighted tone = (5*1 + 0*9) / 10 = 0.5 -> 50 + 5 = 55
    with patch("engine.data_sources.gdelt_client.get_daily_tone", return_value=daily):
        assert gd.sentiment_as_of("Apple Inc", date(2023, 6, 1)) == 55.0


def test_sentiment_as_of_none_when_no_coverage():
    with patch("engine.data_sources.gdelt_client.get_daily_tone", return_value=[]):
        assert gd.sentiment_as_of("Nobody Corp", date(2023, 6, 1)) is None


# --------------------------------------------------------------------------
# get_daily_tone — caching + graceful failure (BigQuery mocked)
# --------------------------------------------------------------------------

def test_get_daily_tone_is_cached():
    calls = {"n": 0}

    def fake_query(company, start, end):
        calls["n"] += 1
        return [{"day": "2023-05-05", "avg_tone": 1.0, "n": 3}]

    with patch("engine.data_sources.gdelt_client._run_daily_tone_query", side_effect=fake_query):
        gd.get_daily_tone("Apple Inc", date(2023, 5, 1), date(2023, 6, 1))
        gd.get_daily_tone("Apple Inc", date(2023, 5, 1), date(2023, 6, 1))

    assert calls["n"] == 1


def test_get_daily_tone_returns_empty_on_failure():
    with patch("engine.data_sources.gdelt_client._run_daily_tone_query", side_effect=RuntimeError("no bigquery")):
        assert gd.get_daily_tone("Apple Inc", date(2023, 5, 1), date(2023, 6, 1)) == []


def test_get_daily_tone_empty_for_blank_company():
    assert gd.get_daily_tone("", date(2023, 5, 1), date(2023, 6, 1)) == []
