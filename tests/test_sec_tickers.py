"""
engine/data_sources/sec_tickers.py — the SEC name↔ticker master. The network
fetch is mocked; the in-process memo is cleared around each test.
"""
from unittest.mock import patch

import pytest

from engine.data_sources import sec_tickers

_RAW = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
    "2": {"cik_str": 1321655, "ticker": "PLTR", "title": "Palantir Technologies Inc."},
}


@pytest.fixture(autouse=True)
def _fresh_memo():
    sec_tickers.refresh()
    yield
    sec_tickers.refresh()


def test_builds_ticker_set_and_name_map():
    with patch("engine.data_sources.sec_tickers._fetch_raw", return_value=_RAW):
        assert sec_tickers.is_real_ticker("aapl") and sec_tickers.is_real_ticker("NVDA")
        assert not sec_tickers.is_real_ticker("ZZZZ")
        names = sec_tickers.name_to_ticker()
    assert names["nvidia"] == "NVDA" and names["palantir technologies"] == "PLTR"


def test_normalize_name_strips_suffixes_and_punctuation():
    assert sec_tickers.normalize_name("Apple Inc.") == "apple"
    assert sec_tickers.normalize_name("NVIDIA CORP") == "nvidia"
    assert sec_tickers.normalize_name("Palantir Technologies, Inc.") == "palantir technologies"
