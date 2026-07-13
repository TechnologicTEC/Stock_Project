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


def test_get_rate_nzd_uses_the_fresh_ecb_rate():
    ecb = {"value": 0.625, "date": "2026-07-10", "source": "ECB (frankfurter.app)"}
    with patch("engine.currency.frankfurter_client.usd_per_nzd", return_value=ecb) as mock_ecb, \
         patch("engine.currency.fred_client.get_series") as mock_fred:
        rate = currency.get_rate("NZD")

    assert rate == pytest.approx(1 / 0.625)  # 1 USD buys 1/0.625 NZD
    mock_ecb.assert_called_once()
    mock_fred.assert_not_called()            # ECB was fresh, FRED not needed


def test_get_rate_nzd_falls_back_to_fred_when_ecb_is_down():
    series = [{"date": "2026-06-29", "value": 0.60}, {"date": "2026-06-30", "value": 0.55}]  # latest last
    with patch("engine.currency.frankfurter_client.usd_per_nzd", side_effect=RuntimeError("ecb down")), \
         patch("engine.currency.fred_client.get_series", return_value=series):
        rate = currency.get_rate("NZD")
    assert rate == pytest.approx(1 / 0.55)   # FRED's most recent observation


def test_rate_info_reports_the_source_and_date():
    ecb = {"value": 0.5756, "date": "2026-07-10", "source": "ECB (frankfurter.app)"}
    with patch("engine.currency.frankfurter_client.usd_per_nzd", return_value=ecb):
        info = currency.rate_info("NZD")
    assert info["usd_per_nzd"] == 0.5756
    assert info["nzd_per_usd"] == pytest.approx(1 / 0.5756)
    assert info["as_of"] == "2026-07-10" and "ECB" in info["source"]
    assert currency.rate_info("USD") is None


def test_get_rate_nzd_is_cached_and_not_refetched_within_ttl():
    ecb = {"value": 0.5, "date": "2026-07-10", "source": "ECB (frankfurter.app)"}
    with patch("engine.currency.frankfurter_client.usd_per_nzd", return_value=ecb) as mock_ecb:
        currency.get_rate("NZD")
        currency.get_rate("NZD")

    assert mock_ecb.call_count == 1  # second call served from the cache layer


def test_get_rate_nzd_raises_when_no_source_has_data():
    with patch("engine.currency.frankfurter_client.usd_per_nzd", side_effect=RuntimeError("down")), \
         patch("engine.currency.fred_client.get_series", return_value=[]):
        with pytest.raises(RuntimeError):
            currency.get_rate("NZD")


def test_get_rate_rejects_unsupported_currency():
    with pytest.raises(ValueError):
        currency.get_rate("EUR")


def test_format_amount_converts_and_labels_with_the_symbol():
    assert currency.format_amount(1000.0, "USD", 1.0) == "$1,000.00"
    assert currency.format_amount(1000.0, "NZD", 1 / 0.5) == "NZ$2,000.00"  # 1000 USD -> 2000 NZD
    assert currency.format_amount(None, "USD", 1.0) == "—"  # missing values stay a dash
