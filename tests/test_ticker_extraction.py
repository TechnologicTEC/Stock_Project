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


# --------------------------------------------------------------------------
# Server-side failures are retryable too.
#
# Extraction runs ONCE per video and what it stores is permanent — the video is
# stamped `mentions_extracted_at` and never revisited. So the set of errors that
# defer has to cover more than quota. Gemini returned 503 on 2026-09-14, matched
# nothing, and the run stored six dictionary mentions with `unknown` stance for
# a video that was bullish on all six — mentions creator_conviction can never
# act on, because it counts BULLISH ones.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("message", [
    "503 Service Unavailable",                      # the one that actually happened
    "ServerError: 500 INTERNAL",
    "502 Bad Gateway",
    "504 Gateway Timeout",
    "UNAVAILABLE: connection refused",
    "model is overloaded, try again later",
    "DEADLINE_EXCEEDED",
    "request timed out",
])
def test_server_side_failures_defer_instead_of_storing_the_fallback(message):
    with patch("engine.ticker_extraction._llm_available", return_value=True), \
         patch("engine.ticker_extraction._extract_llm", side_effect=RuntimeError(message)):
        with pytest.raises(ticker_extraction.TransientExtractionError):
            ticker_extraction.extract_mentions("some transcript text")


def test_a_genuine_bug_still_falls_back_rather_than_blocking_forever():
    """Not everything is retryable. A broken SDK would otherwise mean a video
    is deferred on every scan and never extracted at all."""
    with patch("engine.ticker_extraction._llm_available", return_value=True), \
         patch("engine.ticker_extraction._extract_llm",
               side_effect=TypeError("got an unexpected keyword argument")):
        got = {m.ticker for m in ticker_extraction.extract_mentions("I like AAPL and $TSLA")}
    assert got == {"AAPL", "TSLA"}
