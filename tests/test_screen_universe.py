"""
scripts/screen_universe.py — the weekly screen, now for either universe.

The S&P 400 run must land in its own leaderboard and never touch the S&P 500
one the bots trade from. These drive the real main() and the real cache; only
the network-bound screen itself (`screener.screen_tickers`) is replaced, with
results shaped exactly like the real ones.
"""
import importlib.util
import pathlib

import pytest

from engine import screener, universe
from engine.screener import FactorResult, ScreenerResult

_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "screen_universe.py"
_spec = importlib.util.spec_from_file_location("screen_universe_mod", _PATH)
screen_universe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(screen_universe)


def _results(tickers):
    return [ScreenerResult(ticker=t, overall_score=60.0 + i, recommendation="Buy",
                           data_errors=[],
                           factors={n: FactorResult(score=60.0, reasons=[])
                                    for n in screener.FACTOR_WEIGHTS})
            for i, t in enumerate(tickers)]


@pytest.fixture
def screened(monkeypatch):
    """Record which names were screened; skip configure() (the test DB is set)."""
    seen = []

    def fake_screen(batch):
        seen.extend(batch)
        return _results(batch)

    monkeypatch.setattr(screen_universe, "configure", lambda: None)
    monkeypatch.setattr(screen_universe.screener, "screen_tickers", fake_screen)
    for var in ("SCREEN_TICKERS", "SCREEN_UNIVERSE", "UNIVERSE_EVERY", "UNIVERSE_SIZE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("UNIVERSE_SIZE", "3")
    return seen


def test_the_default_run_is_still_the_sp500(screened):
    screen_universe.main()
    assert screened == list(universe.sp500()[:3])
    assert screener.load_leaderboard()["universe"] == "sp500"
    assert screener.load_leaderboard("sp400") is None


def test_an_sp400_run_lands_in_its_own_leaderboard(screened, monkeypatch):
    screener.save_leaderboard(screener.build_leaderboard(_results(["AAPL"])))   # the bots' ranking
    monkeypatch.setenv("SCREEN_UNIVERSE", "sp400")
    screen_universe.main()
    assert screened == list(universe.sp400()[:3])
    mid = screener.load_leaderboard("sp400")
    assert mid["universe"] == "sp400"
    assert {r["ticker"] for r in mid["rows"]} == set(universe.sp400()[:3])
    assert [r["ticker"] for r in screener.load_leaderboard()["rows"]] == ["AAPL"]   # untouched


def test_auto_repair_looks_for_gaps_in_its_own_universe(screened, monkeypatch):
    """Repair mode compares the saved S&P 400 ranking with the S&P 400 list — not
    the S&P 500's, which would rescreen 500 names into the wrong leaderboard."""
    have = list(universe.sp400())[1:]
    screener.save_leaderboard(screener.build_leaderboard(_results(have), universe="sp400"))
    monkeypatch.setenv("SCREEN_UNIVERSE", "sp400")
    monkeypatch.setenv("SCREEN_TICKERS", "auto")
    screen_universe.main()
    assert screened == [universe.sp400()[0]]
    assert screener.load_leaderboard("sp400")["n_scored"] == len(universe.sp400())
    assert screener.load_leaderboard() is None


def test_a_screen_that_dies_partway_leaves_last_weeks_ranking_live(screened, monkeypatch):
    """Progress is saved after every chunk, but into its own slot. A run that
    crashes after the first chunk must leave the previous COMPLETE ranking in
    the live slot — not a partial one dated today, which the bots would trade."""
    screener.save_leaderboard(screener.build_leaderboard(_results(["AAPL", "MSFT"])))
    monkeypatch.setenv("SCREEN_CHUNK", "1")
    calls = {"n": 0}

    def dies_on_the_second_chunk(batch):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("Finnhub outage")
        return _results(batch)

    monkeypatch.setattr(screen_universe.screener, "screen_tickers", dies_on_the_second_chunk)
    with pytest.raises(RuntimeError):
        screen_universe.main()
    assert [r["ticker"] for r in screener.load_leaderboard()["rows"]] == ["MSFT", "AAPL"]


def test_a_finished_screen_replaces_the_live_ranking(screened, monkeypatch):
    screener.save_leaderboard(screener.build_leaderboard(_results(["OLD"])))
    monkeypatch.setenv("SCREEN_CHUNK", "1")
    screen_universe.main()
    assert {r["ticker"] for r in screener.load_leaderboard()["rows"]} == set(universe.sp500()[:3])


def test_an_unknown_universe_stops_the_run(screened, monkeypatch):
    monkeypatch.setenv("SCREEN_UNIVERSE", "ftse100")
    with pytest.raises(ValueError):
        screen_universe.main()
    assert screened == []
