from unittest.mock import patch

import pytest

from engine import currency


def test_get_rate_usd_is_identity_and_case_insensitive():
    assert currency.get_rate("USD") == 1.0
    assert currency.get_rate("usd") == 1.0
    assert currency.get_rate(None) == 1.0  # defaults to the base currency


def test_symbols():
    assert currency.symbol("USD") == "$"
    assert currency.symbol("NZD") == "NZ$"
    assert currency.symbol(None) == "$"  # falls back to the base symbol


def test_get_rate_nzd_uses_the_most_recent_fred_observation():
    # DEXUSNZ is USD-per-NZD, oldest first; the latest value is what we want.
    series = [
        {"date": "2026-06-29", "value": 0.60},
        {"date": "2026-06-30", "value": 0.625},  # most recent
    ]
    with patch("engine.currency.fred_client.get_series", return_value=series) as mock_get:
        rate = currency.get_rate("NZD")

    assert rate == pytest.approx(1 / 0.625)  # 1 USD buys 1/0.625 NZD
    mock_get.assert_called_once()


def test_get_rate_nzd_is_cached_and_not_refetched_within_ttl():
    series = [{"date": "2026-06-30", "value": 0.5}]
    with patch("engine.currency.fred_client.get_series", return_value=series) as mock_get:
        currency.get_rate("NZD")
        currency.get_rate("NZD")

    assert mock_get.call_count == 1  # second call served from the cache layer


def test_get_rate_nzd_raises_when_fred_returns_nothing():
    with patch("engine.currency.fred_client.get_series", return_value=[]):
        with pytest.raises(RuntimeError):
            currency.get_rate("NZD")


def test_get_rate_rejects_unsupported_currency():
    with pytest.raises(ValueError):
        currency.get_rate("EUR")


def test_format_amount_converts_and_labels_with_the_symbol():
    assert currency.format_amount(1000.0, "USD", 1.0) == "$1,000.00"
    assert currency.format_amount(1000.0, "NZD", 1 / 0.5) == "NZ$2,000.00"  # 1000 USD -> 2000 NZD
    assert currency.format_amount(None, "USD", 1.0) == "—"  # missing values stay a dash
