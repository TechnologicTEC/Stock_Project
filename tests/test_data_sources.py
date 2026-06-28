from unittest.mock import MagicMock, patch

import pytest

from engine.data_sources import edgar_client, finnhub_client


# --------------------------------------------------------------------------
# Finnhub client — verifies field mapping and the "missing API key" guard
# --------------------------------------------------------------------------

def test_get_quote_maps_finnhub_fields_to_our_shape(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "fake-key-for-tests")
    finnhub_client._client.cache_clear()

    mock_sdk_client = MagicMock()
    mock_sdk_client.quote.return_value = {
        "c": 150.0, "d": 1.2, "dp": 0.8, "h": 151.0, "l": 148.0, "o": 149.0, "pc": 148.8,
    }

    with patch("engine.data_sources.finnhub_client.finnhub.Client", return_value=mock_sdk_client):
        result = finnhub_client.get_quote("aapl")

    assert result["ticker"] == "AAPL"  # normalized to uppercase
    assert result["current_price"] == 150.0
    assert result["previous_close"] == 148.8
    mock_sdk_client.quote.assert_called_once_with("AAPL")


def test_get_company_news_drops_items_missing_url_or_timestamp(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "fake-key-for-tests")
    finnhub_client._client.cache_clear()

    mock_sdk_client = MagicMock()
    mock_sdk_client.company_news.return_value = [
        {"headline": "Good item", "source": "Reuters", "url": "http://x/1", "datetime": 1700000000},
        {"headline": "Missing URL", "source": "Reuters", "url": None, "datetime": 1700000000},
    ]

    with patch("engine.data_sources.finnhub_client.finnhub.Client", return_value=mock_sdk_client):
        from datetime import date
        result = finnhub_client.get_company_news("AAPL", date(2026, 1, 1), date(2026, 1, 31))

    assert len(result) == 1
    assert result[0]["headline"] == "Good item"


def test_finnhub_client_raises_clear_error_without_api_key(monkeypatch):
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    finnhub_client._client.cache_clear()

    with pytest.raises(finnhub_client.FinnhubConfigError):
        finnhub_client.get_quote("AAPL")


# --------------------------------------------------------------------------
# EDGAR client — config guard + rate-limit headers, no real network call
# --------------------------------------------------------------------------

def test_edgar_client_raises_clear_error_without_user_agent(monkeypatch):
    monkeypatch.delenv("EDGAR_USER_AGENT", raising=False)

    with pytest.raises(edgar_client.EdgarConfigError):
        edgar_client._headers()


def test_edgar_client_sends_identifying_user_agent(monkeypatch):
    monkeypatch.setenv("EDGAR_USER_AGENT", "Test User test@example.com")

    headers = edgar_client._headers()
    assert headers["User-Agent"] == "Test User test@example.com"


def test_get_cik_for_ticker_finds_match(monkeypatch):
    monkeypatch.setenv("EDGAR_USER_AGENT", "Test User test@example.com")

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
        "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corp"},
    }
    mock_response.raise_for_status.return_value = None

    with patch("engine.data_sources.edgar_client.requests.get", return_value=mock_response):
        cik = edgar_client.get_cik_for_ticker("aapl")

    assert cik == "0000320193"  # zero-padded to 10 digits


def test_get_cik_for_ticker_returns_none_when_not_found(monkeypatch):
    monkeypatch.setenv("EDGAR_USER_AGENT", "Test User test@example.com")

    mock_response = MagicMock()
    mock_response.json.return_value = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}
    mock_response.raise_for_status.return_value = None

    with patch("engine.data_sources.edgar_client.requests.get", return_value=mock_response):
        cik = edgar_client.get_cik_for_ticker("ZZZZNOTREAL")

    assert cik is None
