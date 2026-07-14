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

def test_sentiment_as_of_weights_by_article_count_within_the_window():
    year = [
        {"day": "2023-05-05", "avg_tone": 5.0, "n": 1},    # +5 on 1 article  (in window)
        {"day": "2023-05-20", "avg_tone": 0.0, "n": 9},    # neutral, 9 articles (in window)
        {"day": "2023-01-04", "avg_tone": -9.0, "n": 99},  # OUTSIDE the 30-day window -> ignored
    ]
    # weighted tone = (5*1 + 0*9) / 10 = 0.5 -> 50 + 5 = 55
    with patch("engine.data_sources.gdelt_client.daily_tone_for_year", return_value=year):
        assert gd.sentiment_as_of("Apple Inc", date(2023, 6, 1)) == 55.0


def test_sentiment_as_of_reads_one_cached_year_not_a_query_per_date():
    # The whole point of the batching: many as-of dates, one fetch per company-year.
    calls = {"n": 0}

    def fake_year(company, year):
        calls["n"] += 1
        return [{"day": "2023-05-20", "avg_tone": 2.0, "n": 5}]

    with patch("engine.data_sources.gdelt_client.daily_tone_for_year", side_effect=fake_year):
        for day in (date(2023, 6, 1), date(2023, 6, 2), date(2023, 6, 3)):
            gd.sentiment_as_of("Apple Inc", day)
    assert calls["n"] == 3          # one lookup per as-of, but each hits the SAME cached year
    # (daily_tone_for_year itself is cache.get_or_fetch-backed -> one BigQuery job per year)


def test_sentiment_as_of_spans_a_year_boundary():
    def fake_year(company, year):
        return {2022: [{"day": "2022-12-20", "avg_tone": 5.0, "n": 1}],
                2023: [{"day": "2023-01-03", "avg_tone": 0.0, "n": 1}]}[year]

    with patch("engine.data_sources.gdelt_client.daily_tone_for_year", side_effect=fake_year):
        # 30-day window before 2023-01-10 straddles 2022 -> both years must be read
        assert gd.sentiment_as_of("Apple Inc", date(2023, 1, 10)) == 75.0   # tone (5+0)/2 = 2.5 -> 75


def test_sentiment_as_of_none_when_no_coverage():
    with patch("engine.data_sources.gdelt_client.daily_tone_for_year", return_value=[]):
        assert gd.sentiment_as_of("Nobody Corp", date(2023, 6, 1)) is None


def test_daily_tone_for_year_queries_once_and_caches():
    calls = {"n": 0}

    def fake_query(company, start, end):
        calls["n"] += 1
        assert (start, end) == (date(2023, 1, 1), date(2024, 1, 1))   # a full calendar year
        return [{"day": "2023-05-05", "avg_tone": 1.0, "n": 3}]

    with patch("engine.data_sources.gdelt_client._run_daily_tone_query", side_effect=fake_query):
        gd.daily_tone_for_year("Apple Inc", 2023)
        gd.daily_tone_for_year("Apple Inc", 2023)
    assert calls["n"] == 1          # second call served from the cache


def test_daily_tone_for_year_empty_on_failure_or_blank_company():
    with patch("engine.data_sources.gdelt_client._run_daily_tone_query", side_effect=RuntimeError("no bq")):
        assert gd.daily_tone_for_year("Apple Inc", 2023) == []
    assert gd.daily_tone_for_year("", 2023) == []


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
