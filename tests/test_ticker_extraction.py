"""
engine/ticker_extraction.py — LLM-primary / dictionary-fallback extraction, all
gated by SEC-list validation. The SEC maps and the LLM call are mocked.
"""
from unittest.mock import patch

import pytest

from engine import ticker_extraction
from engine.ticker_extraction import Mention

_TICKERS = frozenset({"AAPL", "NVDA", "PLTR", "TSLA"})
_NAMES = {"apple": "AAPL", "nvidia": "NVDA", "palantir technologies": "PLTR", "tesla": "TSLA"}


@pytest.fixture(autouse=True)
def _fake_sec():
    with patch("engine.ticker_extraction.sec_tickers.ticker_set", return_value=_TICKERS), \
         patch("engine.ticker_extraction.sec_tickers.name_to_ticker", return_value=_NAMES):
        yield


def test_dictionary_path_finds_cashtags_names_and_symbols():
    text = "I love $NVDA and apple. Palantir Technologies looks great. Also watching AAPL."
    with patch("engine.ticker_extraction._llm_available", return_value=False):
        got = {m.ticker for m in ticker_extraction.extract_mentions(text)}
    assert got == {"NVDA", "AAPL", "PLTR"}


def test_validation_drops_unlisted_symbols():
    with patch("engine.ticker_extraction._llm_available", return_value=False):
        got = {m.ticker for m in ticker_extraction.extract_mentions("Buy $ZZZZ, better than $NVDA")}
    assert got == {"NVDA"}                       # ZZZZ isn't a real ticker


def test_llm_path_used_when_available_and_validated():
    llm = [Mention("TSLA", "Tesla", "bullish", 0.9), Mention("ZZZZ", "Fake Co", "bearish", 0.9)]
    with patch("engine.ticker_extraction._llm_available", return_value=True), \
         patch("engine.ticker_extraction._extract_llm", return_value=llm):
        got = ticker_extraction.extract_mentions("anything")
    assert [m.ticker for m in got] == ["TSLA"]   # ZZZZ validated out
    assert got[0].stance == "bullish"


def test_llm_failure_falls_back_to_dictionary():
    with patch("engine.ticker_extraction._llm_available", return_value=True), \
         patch("engine.ticker_extraction._extract_llm", side_effect=RuntimeError("boom")):
        got = {m.ticker for m in ticker_extraction.extract_mentions("I like AAPL and $TSLA")}
    assert got == {"AAPL", "TSLA"}


def test_transient_llm_error_reraises_for_retry():
    # A quota / rate-limit failure must propagate so the caller retries later
    # instead of silently accepting the sparse dictionary result.
    with patch("engine.ticker_extraction._llm_available", return_value=True), \
         patch("engine.ticker_extraction._extract_llm", side_effect=RuntimeError("429 RESOURCE_EXHAUSTED quota")):
        with pytest.raises(ticker_extraction.TransientExtractionError):
            ticker_extraction.extract_mentions("some transcript text")


def test_dictionary_path_ignores_single_word_common_names():
    # "apple"/"tesla" as lone lowercase words are NOT matched (too noisy); only
    # multi-word names, $cashtags and explicit uppercase symbols are.
    with patch("engine.ticker_extraction._llm_available", return_value=False):
        got = {m.ticker for m in ticker_extraction.extract_mentions("i feel bullish, apple and tesla look fine")}
    assert got == set()


def test_empty_text_returns_nothing():
    assert ticker_extraction.extract_mentions("   ") == []
