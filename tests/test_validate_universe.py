"""
scripts/validate_universe.py — the cross-sectional validation, for either list.

Drives the real main(), summary and cache; only the network-bound history
reconstruction (`pooled_walk_forward`) is replaced, returning points shaped like
the real ones (tests/test_screener_validation.py builds the same shape).
"""
import importlib.util
import pathlib
from datetime import date, timedelta

import pytest

from engine import screener_validation as sv, universe

_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "validate_universe.py"
_spec = importlib.util.spec_from_file_location("validate_universe_mod", _PATH)
validate_universe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validate_universe)


def _points(tickers):
    days = [date(2022, 1, 1) + timedelta(days=91 * i) for i in range(8)]
    return [{"date": d, "ticker": t, "score": float(k), "recommendation": "Hold",
             "forward_return_pct": float(k), "factors": {"momentum": float(k)}}
            for d in days for k, t in enumerate(tickers)]


@pytest.fixture
def walked(monkeypatch):
    seen = []

    def fake_walk(tickers, *a, **k):
        seen.extend(tickers)
        return _points(tickers)

    monkeypatch.setattr(validate_universe, "configure", lambda: None)
    monkeypatch.setattr(validate_universe.validation, "pooled_walk_forward", fake_walk)
    for var in ("VALIDATION_UNIVERSE", "UNIVERSE_EVERY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("UNIVERSE_SIZE", "25")
    return seen


def test_the_default_run_is_still_the_sp500(walked):
    validate_universe.main()
    assert walked == list(universe.sp500()[:25])
    assert sv.load_universe_result()["universe"] == "sp500"


def test_an_sp400_run_is_labelled_and_stored_as_its_own(walked, monkeypatch):
    monkeypatch.setenv("VALIDATION_UNIVERSE", "sp400")
    validate_universe.main()
    assert walked == list(universe.sp400()[:25])
    result = sv.load_universe_result("sp400")
    assert result["universe"] == "sp400" and result["n_requested"] == 25
    assert sv.load_universe_result() is None            # the S&P 500 result is untouched


def test_an_unknown_universe_stops_the_run(walked, monkeypatch):
    monkeypatch.setenv("VALIDATION_UNIVERSE", "ftse100")
    with pytest.raises(ValueError):
        validate_universe.main()
    assert walked == []
