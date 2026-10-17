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
_TITLES = {"AAPL": "Apple Inc.", "NVDA": "NVIDIA CORP", "PLTR": "Palantir Technologies Inc.",
           "TSLA": "Tesla, Inc."}


@pytest.fixture(autouse=True)
def _fake_sec():
    with patch("engine.ticker_extraction.sec_tickers.ticker_set", return_value=_TICKERS), \
         patch("engine.ticker_extraction.sec_tickers.name_to_ticker", return_value=_NAMES), \
         patch("engine.ticker_extraction.sec_tickers.ticker_to_title", return_value=_TITLES):
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


# --------------------------------------------------------------------------
# check_names — does the ticker belong to the company the speaker named?
#
# The cases are the real ones from the 17 Oct 2026 audit of all 440 stored
# mentions: 7 had a real ticker of the wrong company, and a string comparison
# also flags renames and SPACs that are right.
# --------------------------------------------------------------------------

_REAL = frozenset({"MOD", "MODD", "KOS", "KTOS", "CCXI", "SPCX", "RTX", "EOS", "EOSE", "GO"})
_REAL_TITLES = {
    "MOD": "MODINE MANUFACTURING CO", "MODD": "Modular Medical, Inc.",
    "KOS": "Kosmos Energy Ltd.", "KTOS": "KRATOS DEFENSE & SECURITY SOLUTIONS, INC.",
    "CCXI": "Churchill Capital Corp XI", "SPCX": "SPACE EXPLORATION TECHNOLOGIES CORP",
    "RTX": "RTX Corp", "EOS": "Eaton Vance Enhanced Equity Income Fund II",
    "EOSE": "Eos Energy Enterprises, Inc.", "GO": "Grocery Outlet Holding Corp.",
}
_REAL_NAMES = {"modular medical": "MODD", "eos energy enterprises": "EOSE",
               "kratos defense & security solutions": "KTOS"}

_CCXI_SAID = ("Agility Robotics makes Digit a warehouse robot. Now, this company is going "
              "public through a SPAC, ticker symbol CCXI, and the deal is expected to close.")


@pytest.fixture
def real_sec():
    with patch("engine.ticker_extraction.sec_tickers.ticker_set", return_value=_REAL), \
         patch("engine.ticker_extraction.sec_tickers.name_to_ticker", return_value=_REAL_NAMES), \
         patch("engine.ticker_extraction.sec_tickers.ticker_to_title", return_value=_REAL_TITLES), \
         patch("engine.ticker_extraction._llm_available", return_value=True):
        yield


def _check(mentions, *, verdicts=None, text="", error=None):
    """Run check_names with the model stubbed; returns (result, items sent)."""
    sent = []

    def ask(pairs):
        sent.extend(pairs)
        if error:
            raise error
        return verdicts or {}

    with patch("engine.ticker_extraction._ask_model_to_check", side_effect=ask):
        return ticker_extraction.check_names(mentions, text), sent


@pytest.mark.parametrize("spoken, official, agree", [
    ("Vivacore", "Vivakor, Inc.", True),                 # same company, misheard
    ("Neutra Band, Inc.", "NutriBand Inc.", True),
    ("AMD", "ADVANCED MICRO DEVICES INC", True),          # initials
    ("Palantir", "Palantir Technologies Inc.", True),
    ("Iris Energy", "EOS Energy Enterprises", False),     # sharing only a common word
    ("EOS Energy Enterprises", "Eaton Vance Enhanced Equity Income Fund II", False),
    ("Kratos Defense", "Kosmos Energy Ltd.", False),
    ("Gold Resource Corporation", "Grocery Outlet Holding Corp.", False),
    (None, "Kosmos Energy Ltd.", True),                   # nothing to compare
])
def test_names_agree(spoken, official, agree):
    assert ticker_extraction.names_agree(spoken, official) is agree


def test_an_exact_name_moves_the_mention_without_asking_the_model(real_sec):
    """Modular Medical as MOD (Modine). EOS is the trap for a lazy rule: the
    name CONTAINS the ticker, and EOS is still an Eaton Vance fund."""
    got, sent = _check([Mention("MOD", "Modular Medical", "neutral"),
                        Mention("EOS", "EOS Energy Enterprises", "bullish")])
    assert [(m.ticker, m.stance) for m in got] == [("MODD", "neutral"), ("EOSE", "bullish")]
    assert sent == []


def test_names_that_fit_never_reach_the_model(real_sec):
    got, sent = _check([Mention("KTOS", "Kratos Defense", "bullish")])
    assert [m.ticker for m in got] == ["KTOS"] and sent == []


def test_the_model_corrects_a_ticker_the_sec_confirms(real_sec):
    got, sent = _check([Mention("KOS", "Kratos Defense", "bullish")],
                       verdicts={"KOS": {"ticker": "KOS", "same": False, "correct": "KTOS"}})
    assert [m.ticker for m in got] == ["KTOS"]
    assert sent[0]["listed_as"] == "Kosmos Energy Ltd."


def test_a_correction_the_sec_does_not_confirm_is_not_taken(real_sec):
    """The original caption case: 'Gold Resource ... G O' stored as GO. A model
    answer whose SEC name isn't the company named is its memory, not evidence."""
    got, _ = _check([Mention("GO", "Gold Resource Corporation", "bullish")],
                    verdicts={"GO": {"ticker": "GO", "same": False, "correct": "MOD"}})
    assert [m.ticker for m in got] == ["GO"]


def test_doubt_alone_never_drops_a_mention(real_sec):
    """Replayed over the stored mentions, the model said SpaceX and Agility
    Robotics (a SPAC, CCXI) had no listing. Both do."""
    got, sent = _check([Mention("CCXI", "Agility Robotics", "bullish"),
                        Mention("SPCX", "SpaceX", "neutral")],
                       verdicts={"CCXI": {"ticker": "CCXI", "same": False, "correct": None},
                                 "SPCX": {"ticker": "SPCX", "same": False, "correct": None}},
                       text=_CCXI_SAID)
    assert [m.ticker for m in got] == ["CCXI", "SPCX"]
    assert "ticker symbol CCXI" in sent[0]["said"]        # the model is shown what was said


def test_a_rename_the_model_confirms_is_kept(real_sec):
    got, _ = _check([Mention("RTX", "Raytheon", "neutral")],
                    verdicts={"RTX": {"ticker": "RTX", "same": True, "correct": None}})
    assert [m.ticker for m in got] == ["RTX"]


def test_a_busy_model_defers_the_video(real_sec):
    with pytest.raises(ticker_extraction.TransientExtractionError):
        _check([Mention("KOS", "Kratos Defense", "bullish")],
               error=RuntimeError("503 UNAVAILABLE"))


def test_a_broken_model_keeps_the_mentions_as_they_were(real_sec):
    got, _ = _check([Mention("KOS", "Kratos Defense", "bullish")],
                    error=ValueError("bad JSON"))
    assert [m.ticker for m in got] == ["KOS"]


def test_a_correction_onto_a_ticker_already_mentioned_keeps_the_first(real_sec):
    got, _ = _check([Mention("MODD", "Modular Medical", "bullish"),
                     Mention("MOD", "Modular Medical", "neutral")])
    assert [(m.ticker, m.stance) for m in got] == [("MODD", "bullish")]


def test_extraction_runs_the_check_with_the_transcript(real_sec):
    """Through extract_mentions, the path the scan takes — not check_names alone."""
    llm = [Mention("KOS", "Kratos Defense", "bullish")]
    with patch("engine.ticker_extraction._extract_llm", return_value=llm):
        got, sent = None, []

        def ask(pairs):
            sent.extend(pairs)
            return {"KOS": {"ticker": "KOS", "same": False, "correct": "KTOS"}}

        with patch("engine.ticker_extraction._ask_model_to_check", side_effect=ask):
            got = ticker_extraction.extract_mentions("Kratos Defense is a drone maker.")
    assert [m.ticker for m in got] == ["KTOS"]
    assert "drone maker" in sent[0]["said"]
