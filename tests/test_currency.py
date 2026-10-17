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


def test_get_rate_nzd_keeps_the_last_rate_when_both_sources_are_down():
    """A day-old rate beats showing the whole page in USD."""
    from datetime import timedelta

    from engine import cache

    ecb = {"value": 0.5, "date": "2026-07-10", "source": "ECB (frankfurter)"}
    with patch("engine.currency.frankfurter_client.usd_per_nzd", return_value=ecb):
        currency.get_rate("NZD")
    later = cache._utcnow() + timedelta(seconds=currency._FX_TTL_SECONDS + 60)
    with patch("engine.cache._utcnow", return_value=later), \
         patch("engine.currency.frankfurter_client.usd_per_nzd", side_effect=RuntimeError("down")), \
         patch("engine.currency.fred_client.get_series", side_effect=RuntimeError("down")):
        assert currency.get_rate("NZD") == 2.0


# --------------------------------------------------------------------------
# Historical rates: a failure must never be cached. The cache keeps these a
# year, so one timeout used to blank that quarter's valuation until next year.
# --------------------------------------------------------------------------

def test_a_failed_historical_rate_is_not_cached_and_is_retried():
    from datetime import date

    day = date(2025, 12, 31)
    with patch("engine.currency.frankfurter_client.usd_per",
               side_effect=RuntimeError("Read timed out")):
        assert currency.historical_usd_rate("EUR", day) is None
    with patch("engine.currency.frankfurter_client.usd_per", return_value=1.175) as fetch:
        assert currency.historical_usd_rate("EUR", day) == 1.175
    assert fetch.call_count == 1                 # asked again, not served the failure


def test_an_unpublished_currency_is_a_real_answer_and_is_cached():
    from datetime import date

    day = date(2025, 12, 31)
    with patch("engine.currency.frankfurter_client.usd_per", return_value=None) as fetch:
        assert currency.historical_usd_rate("TWD", day) is None
        assert currency.historical_usd_rate("TWD", day) is None
    assert fetch.call_count == 1


# --------------------------------------------------------------------------
# The client: what counts as an answer, and what counts as a failure
# --------------------------------------------------------------------------

class _Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        import requests
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Server Error")


def test_client_calls_the_new_address():
    """api.frankfurter.app answers every call with a 301 to frankfurter.dev/v1."""
    from engine.data_sources import frankfurter_client as fc

    with patch("engine.data_sources.frankfurter_client.requests.get",
               return_value=_Resp(200, {"rates": {"USD": 0.566}, "date": "2026-10-16"})) as get:
        assert fc.usd_per_nzd()["value"] == 0.566
    assert get.call_args.args[0] == "https://api.frankfurter.dev/v1/latest"


def test_client_reads_a_404_as_not_published():
    from datetime import date

    from engine.data_sources import frankfurter_client as fc

    with patch("engine.data_sources.frankfurter_client.requests.get",
               return_value=_Resp(404, {"message": "not found"})) as get:
        assert fc.usd_per("TWD", date(2025, 12, 31)) is None
    assert get.call_args.args[0] == "https://api.frankfurter.dev/v1/2025-12-31"


def test_client_raises_on_a_server_error():
    from datetime import date

    import requests

    from engine.data_sources import frankfurter_client as fc

    with patch("engine.data_sources.frankfurter_client.requests.get", return_value=_Resp(503)):
        with pytest.raises(requests.HTTPError):
            fc.usd_per("EUR", date(2025, 12, 31))


def test_client_tries_once_more_after_a_timeout_then_gives_up():
    from datetime import date

    import requests

    from engine.data_sources import frankfurter_client as fc

    ok = _Resp(200, {"rates": {"USD": 1.175}})
    with patch("engine.data_sources.frankfurter_client.requests.get",
               side_effect=[requests.Timeout("slow"), ok]):
        assert fc.usd_per("EUR", date(2025, 12, 31)) == 1.175
    with patch("engine.data_sources.frankfurter_client.requests.get",
               side_effect=requests.Timeout("slow")) as get:
        with pytest.raises(requests.Timeout):
            fc.usd_per("EUR", date(2025, 12, 31))
    assert get.call_count == fc._ATTEMPTS
