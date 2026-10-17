"""
engine/spinoffs.py — the spin-off adjustment Alpaca doesn't make.

The numbers are CTVA's real ones: 77.67 on 30 Sep 2026, 12.57 on 1 Oct when
holders also got one VYLR each, at 67.21. The database is the in-memory one
conftest sets up; Alpaca is stubbed.
"""
from datetime import date, timedelta
from unittest.mock import patch

import pytest

from engine import cache, price_history, spinoffs

EX = date(2026, 10, 1)
CTVA = {"source_symbol": "CTVA", "new_symbol": "VYLR", "new_rate": 1, "source_rate": 1,
        "ex_date": "2026-10-01"}
RAW = {("CTVA", EX): 12.57, ("VYLR", EX): 67.21}


def _raw_close(ticker, day):
    return RAW.get((ticker, day))


def _bar(day, close):
    return {"date": day, "open": close, "high": close, "low": close, "close": close,
            "volume": 1000}


def _seed(prev=77.67, on_ex=12.57):
    """Five sessions before the ex-date at `prev`, then the ex-date and one after."""
    before = [_bar(EX - timedelta(days=n), prev) for n in (7, 6, 3, 2, 1)]
    cache.save_price_bars("CTVA", spinoffs.SOURCE,
                          before + [_bar(EX, on_ex), _bar(EX + timedelta(days=1), 11.92)])


def _closes():
    return {r["date"]: r["close"] for r in
            cache.get_price_history("CTVA", spinoffs.SOURCE, EX - timedelta(days=10),
                                    EX + timedelta(days=5))}


def test_the_factor_is_what_holders_kept_in_the_parent():
    assert spinoffs.factor(12.57, 1.0, 67.21) == pytest.approx(0.1576, abs=1e-4)
    assert spinoffs.factor(34.73, 0.5, 97.03) == pytest.approx(0.4172, abs=1e-4)   # DD / Qnity


def test_apply_scales_every_row_before_the_ex_date_and_nothing_after():
    _seed()
    items = spinoffs.plan([CTVA], raw_close=_raw_close)
    assert items[0]["status"] == "apply" and items[0]["rows"] == 5

    spinoffs.apply(items)
    closes = _closes()
    assert closes[EX - timedelta(days=1)] == pytest.approx(77.67 * items[0]["factor"])
    assert closes[EX] == 12.57 and closes[EX + timedelta(days=1)] == 11.92
    # The fake -84% is now the real day: +2.7%.
    assert closes[EX] / closes[EX - timedelta(days=1)] - 1 == pytest.approx(0.027, abs=0.001)


def test_running_it_again_does_not_scale_the_same_rows_twice():
    _seed()
    spinoffs.apply(spinoffs.plan([CTVA], raw_close=_raw_close))
    once = _closes()

    again = spinoffs.plan([CTVA], raw_close=_raw_close)
    assert again[0]["status"] == "already applied"
    spinoffs.apply(again)
    # Even an item still marked "apply" from a plan made before the first run.
    spinoffs.apply([{**again[0], "status": "apply", "factor": 0.5}])
    assert _closes() == once


def test_a_spin_off_listed_twice_is_planned_once():
    """Alpaca returns DD/Q and BDX/WAT twice."""
    _seed()
    items = spinoffs.plan([CTVA, dict(CTVA)], raw_close=_raw_close)
    assert len(items) == 1


def test_a_refetch_saves_prices_that_match_the_adjusted_rows():
    """The nightly job refetches 400 days and upserts them. Without the hook
    it would write the unadjusted 77.67s straight back over the fix — this goes
    through price_history.refresh, the path the job takes."""
    _seed()
    spinoffs.apply(spinoffs.plan([CTVA], raw_close=_raw_close))
    fixed = _closes()

    unadjusted = [_bar(EX - timedelta(days=n), 77.67) for n in (7, 6, 3, 2, 1)] + [
        _bar(EX, 12.57), _bar(EX + timedelta(days=1), 11.92)]
    with patch("engine.price_history._alpaca_bars", return_value=unadjusted):
        price_history.refresh("CTVA", EX - timedelta(days=10), EX + timedelta(days=5),
                              source="alpaca")
    assert _closes() == pytest.approx(fixed)


def test_a_cache_that_does_not_show_the_drop_is_left_for_a_person():
    """Alpaca's records aren't always right: it carries a reverse split for HON
    that never happened to the price. If the cached series doesn't fall the way
    an unadjusted spin-off would, nothing is scaled."""
    _seed(prev=12.40, on_ex=12.57)
    items = spinoffs.plan([CTVA], raw_close=_raw_close)
    assert items[0]["status"] == "cache doesn't show the drop"
    assert spinoffs.apply(items) == []
    assert spinoffs.recorded() == {}


def test_a_tiny_spin_off_is_not_worth_adjusting():
    _seed(prev=12.70, on_ex=12.57)
    items = spinoffs.plan([CTVA], raw_close=lambda t, d: {"CTVA": 12.57, "VYLR": 0.10}[t])
    assert items[0]["status"] == "too small"


def test_missing_prices_are_reported_not_guessed():
    assert spinoffs.plan([CTVA], raw_close=_raw_close)[0]["status"] == "no cached prices"
    _seed()
    items = spinoffs.plan([CTVA], raw_close=lambda t, d: None if t == "VYLR" else 12.57)
    assert items[0]["status"] == "no price for VYLR"


def test_adjust_bars_only_touches_tickers_and_dates_on_record():
    cache.set_value(spinoffs.ADJUSTMENTS_KEY,
                    {"CTVA": [{"ex_date": "2026-10-01", "factor": 0.5}]})
    bars = [_bar(EX - timedelta(days=1), 80.0), _bar(EX, 12.0)]
    assert [b["close"] for b in spinoffs.adjust_bars("CTVA", bars)] == [40.0, 12.0]
    assert spinoffs.adjust_bars("DD", bars) == bars


# --------------------------------------------------------------------------
# The nightly step
# --------------------------------------------------------------------------

def test_the_nightly_step_adjusts_and_never_fails_the_job(capsys):
    from scripts import warm_cache

    _seed()
    with patch("engine.data_sources.alpaca_client.get_spin_offs", return_value=[CTVA]), \
         patch("engine.data_sources.alpaca_client.get_raw_close", side_effect=_raw_close):
        assert warm_cache.adjust_spin_offs(EX - timedelta(days=400), EX) == 1
    assert "scaled 5 earlier rows" in capsys.readouterr().out

    with patch("engine.data_sources.alpaca_client.get_spin_offs",
               side_effect=RuntimeError("503")):
        assert warm_cache.adjust_spin_offs(EX - timedelta(days=400), EX) == 0
    assert "spin-offs FAILED" in capsys.readouterr().out


# --------------------------------------------------------------------------
# The Alpaca lookup
# --------------------------------------------------------------------------

class _Resp:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    def json(self):
        return self._body

    def raise_for_status(self):
        import requests
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


def test_get_spin_offs_pages_and_speaks_our_ticker_form():
    from engine.data_sources import alpaca_client

    pages = [_Resp({"corporate_actions": {"spin_offs": [{**CTVA, "source_symbol": "BRK.B"}]},
                    "next_page_token": "p2"}),
             _Resp({"corporate_actions": {"spin_offs": [CTVA]}, "next_page_token": None})]
    with patch.object(alpaca_client, "_require_keys", return_value=("k", "s")), \
         patch("requests.get", side_effect=pages) as get:
        got = alpaca_client.get_spin_offs(["BRK-B", "CTVA"], EX, EX)
    assert [s["source_symbol"] for s in got] == ["BRK-B", "CTVA"]
    first, second = get.call_args_list
    assert first.kwargs["params"]["symbols"] == "BRK.B,CTVA"
    assert first.kwargs["params"]["types"] == "spin_off"
    assert second.kwargs["params"]["page_token"] == "p2"


def test_get_spin_offs_raises_rather_than_reporting_none():
    import requests

    from engine.data_sources import alpaca_client

    with patch.object(alpaca_client, "_require_keys", return_value=("k", "s")), \
         patch("requests.get", return_value=_Resp({}, status=500)):
        with pytest.raises(requests.HTTPError):
            alpaca_client.get_spin_offs(["CTVA"], EX, EX)
