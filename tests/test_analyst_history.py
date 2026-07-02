from datetime import date
from unittest.mock import patch

from engine.data_sources import analyst_history as ah


# --------------------------------------------------------------------------
# grade_bucket — normalizing free-text analyst grades
# --------------------------------------------------------------------------

def test_grade_bucket_maps_common_grades():
    assert ah.grade_bucket("Buy") == "buy"
    assert ah.grade_bucket("Outperform") == "buy"
    assert ah.grade_bucket("Overweight") == "buy"
    assert ah.grade_bucket("Strong Buy") == "strongBuy"       # strong wins over plain 'buy'
    assert ah.grade_bucket("Hold") == "hold"
    assert ah.grade_bucket("Neutral") == "hold"
    assert ah.grade_bucket("Market Perform") == "hold"
    assert ah.grade_bucket("Equal-Weight") == "hold"
    assert ah.grade_bucket("Sell") == "sell"
    assert ah.grade_bucket("Underperform") == "sell"
    assert ah.grade_bucket("Underweight") == "sell"
    assert ah.grade_bucket("Strong Sell") == "strongSell"
    assert ah.grade_bucket("") is None
    assert ah.grade_bucket("Coverage Initiated") is None


# --------------------------------------------------------------------------
# reconstruct_recommendation — point-in-time consensus from change events
# --------------------------------------------------------------------------

def _events():
    return [
        {"date": "2022-01-10", "firm": "Alpha", "to_grade": "Buy"},
        {"date": "2022-06-15", "firm": "Alpha", "to_grade": "Hold"},        # Alpha's latest by mid-2022
        {"date": "2022-03-01", "firm": "Beta", "to_grade": "Outperform"},   # -> buy
        {"date": "2019-01-01", "firm": "Gamma", "to_grade": "Sell"},        # stale -> dropped
        {"date": "2022-12-01", "firm": "Delta", "to_grade": "Strong Buy"},  # future for a mid-2022 as-of
    ]


def test_reconstruct_uses_latest_rating_per_firm_and_respects_as_of():
    counts = ah.reconstruct_recommendation(_events(), date(2022, 7, 1))
    # Alpha's latest is Hold (its earlier Buy is superseded); Beta is a buy.
    # Gamma is stale (2019); Delta's Strong Buy is still in the future.
    assert counts == {"strongBuy": 0, "buy": 1, "hold": 1, "sell": 0, "strongSell": 0}


def test_reconstruct_includes_events_once_they_are_public():
    counts = ah.reconstruct_recommendation(_events(), date(2023, 1, 1))
    # Now Delta's Strong Buy counts; Alpha Hold + Beta buy still active; Gamma still stale.
    assert counts == {"strongBuy": 1, "buy": 1, "hold": 1, "sell": 0, "strongSell": 0}


def test_reconstruct_drops_stale_coverage():
    only_stale = [{"date": "2019-01-01", "firm": "Gamma", "to_grade": "Sell"}]
    assert ah.reconstruct_recommendation(only_stale, date(2023, 1, 1)) is None


def test_reconstruct_none_when_nothing_public_yet():
    assert ah.reconstruct_recommendation(_events(), date(2018, 1, 1)) is None


# --------------------------------------------------------------------------
# Fetch + cache
# --------------------------------------------------------------------------

def test_get_rating_events_is_cached():
    calls = {"n": 0}

    def fake(ticker):
        calls["n"] += 1
        return _events()

    with patch("engine.data_sources.analyst_history.yfinance_client.get_upgrades_downgrades", side_effect=fake):
        ah.get_rating_events("AAPL")
        ah.get_rating_events("AAPL")

    assert calls["n"] == 1


def test_recommendation_as_of_end_to_end():
    with patch("engine.data_sources.analyst_history.yfinance_client.get_upgrades_downgrades", return_value=_events()):
        counts = ah.recommendation_as_of("AAPL", date(2022, 7, 1))
    assert counts == {"strongBuy": 0, "buy": 1, "hold": 1, "sell": 0, "strongSell": 0}


def test_get_rating_events_survives_yfinance_failure():
    with patch("engine.data_sources.analyst_history.yfinance_client.get_upgrades_downgrades",
               side_effect=RuntimeError("yahoo down")):
        assert ah.get_rating_events("AAPL") == []
