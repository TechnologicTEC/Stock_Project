"""
engine/bot/liquidity.py.

The module exists so a paper fill can't teach us something false, which makes
the load-bearing tests the ones about what it refuses to do: it must not let a
$95k-a-day stock through, and it must not sell a name just because the data went
missing. Both are asserted against the real thresholds rather than mocked ones,
so a threshold change has to be a deliberate edit to a failing test.
"""
from datetime import date

import pandas as pd
import pytest

from engine.bot import liquidity


def _frame(*, bars=60, close=50.0, volume=1_000_000, closes=None, volumes=None):
    """A price frame shaped like engine/price_history returns one."""
    n = bars if closes is None else len(closes)
    idx = [date(2026, 1, 1) for _ in range(n)]          # index value is unused
    return pd.DataFrame(
        {
            "close": closes if closes is not None else [close] * n,
            "volume": volumes if volumes is not None else [volume] * n,
        },
        index=idx,
    )


# --------------------------------------------------------------------------
# median_dollar_volume
# --------------------------------------------------------------------------

def test_a_single_huge_volume_day_cannot_certify_a_thin_stock():
    """The median, not the mean — these names print one 40x day on news."""
    volumes = [10_000] * 59 + [400_000_000]
    frame = _frame(close=1.0, volumes=volumes, closes=[1.0] * 60)
    assert liquidity.median_dollar_volume(frame) == 10_000
    mean = sum(volumes) / len(volumes)
    assert mean > liquidity.MIN_DOLLAR_VOLUME       # a mean would have passed it


def test_dollar_volume_is_none_without_data():
    assert liquidity.median_dollar_volume(None) is None
    assert liquidity.median_dollar_volume(pd.DataFrame()) is None


def test_dollar_volume_is_none_when_the_volume_column_is_missing():
    assert liquidity.median_dollar_volume(pd.DataFrame({"close": [1.0]})) is None


def test_only_the_recent_window_counts():
    """An old burst of liquidity doesn't describe how the name trades now."""
    closes = [1.0] * 120
    volumes = [50_000_000] * 60 + [10_000] * 60
    assert liquidity.median_dollar_volume(_frame(closes=closes, volumes=volumes)) == 10_000


# --------------------------------------------------------------------------
# assess — one gate at a time
# --------------------------------------------------------------------------

def test_no_frame_is_unmeasured_not_a_pass():
    verdict = liquidity.assess("IFNNY", None, 1_250)
    assert not verdict.ok
    assert verdict.code == liquidity.UNMEASURED


def test_an_empty_frame_is_unmeasured():
    assert liquidity.assess("X", pd.DataFrame(), 1_250).code == liquidity.UNMEASURED


def test_a_new_listing_has_no_liquidity_track_record():
    verdict = liquidity.assess("IPO", _frame(bars=liquidity.MIN_BARS - 1), 1_250)
    assert not verdict.ok
    assert verdict.code == liquidity.THIN_HISTORY
    assert verdict.bars == liquidity.MIN_BARS - 1


def test_exactly_min_bars_is_enough():
    assert liquidity.assess("OK", _frame(bars=liquidity.MIN_BARS), 1_250).ok


def test_a_sub_dollar_stock_is_rejected_on_price_alone():
    """FEED, from the live universe: $0.35, where one tick is ~3%."""
    frame = _frame(close=0.35, volume=100_000_000)     # deliberately liquid
    verdict = liquidity.assess("FEED", frame, 1_250)
    assert not verdict.ok
    assert verdict.code == liquidity.PENNY
    assert verdict.dollar_volume > liquidity.MIN_DOLLAR_VOLUME


def test_a_thin_market_is_rejected_even_at_a_healthy_price():
    """HQI, from the live universe: $15.96 but only $167k a day."""
    frame = _frame(close=15.96, volume=10_500)
    verdict = liquidity.assess("HQI", frame, 1_250)
    assert not verdict.ok
    assert verdict.code == liquidity.ILLIQUID


def test_a_liquid_name_passes_and_reports_what_it_measured():
    verdict = liquidity.assess("NVTS", _frame(close=12.44, volume=17_000_000), 1_250)
    assert verdict.ok and verdict.code == liquidity.OK
    assert verdict.close == pytest.approx(12.44)
    assert verdict.participation < 0.0001
    assert "median daily volume" in verdict.reason


def test_participation_blocks_an_order_that_would_move_the_stock():
    """Slack today, so it is forced with a size this account will never have."""
    frame = _frame(close=10.0, volume=1_000_000)       # $10M a day
    assert liquidity.assess("BIG", frame, 1_250).ok
    verdict = liquidity.assess("BIG", frame, 500_000)
    assert not verdict.ok
    assert verdict.code == liquidity.PARTICIPATION


def test_participation_is_slack_at_this_accounts_size():
    """Documents the measured finding: at $10k/8 slots the participation gate
    rejects nothing that the dollar-volume floor has not already rejected."""
    notional = 10_000 / 8
    at_the_floor = _frame(close=10.0, volume=int(liquidity.MIN_DOLLAR_VOLUME / 10))
    verdict = liquidity.assess("EDGE", at_the_floor, notional)
    assert verdict.ok
    assert verdict.participation < liquidity.MAX_PARTICIPATION / 10


def test_the_price_floor_is_checked_before_the_volume_floor():
    """A name failing both should be reported as the penny stock it is — the
    reason line is what a human reads back later."""
    verdict = liquidity.assess("OBAI", _frame(close=0.32, volume=800_000), 1_250)
    assert verdict.code == liquidity.PENNY


# --------------------------------------------------------------------------
# screen — the entry-gate-only asymmetry
# --------------------------------------------------------------------------

def _frames():
    return {
        "GOOD": _frame(close=50.0, volume=1_000_000),
        "THIN": _frame(close=15.96, volume=10_500),
        "GONE": None,
    }


def test_screen_splits_tradable_from_excluded():
    tradable, excluded = liquidity.screen(["GOOD", "THIN", "GONE"], _frames(), 1_250)
    assert [a.ticker for a in tradable] == ["GOOD"]
    assert sorted(a.ticker for a in excluded) == ["GONE", "THIN"]


def test_a_held_name_is_never_excluded_by_the_liquidity_screen():
    """The asymmetry the module is built around: this filter gates entries. If
    it could force an exit, a cold cache would become a sell order."""
    tradable, excluded = liquidity.screen(
        ["GOOD", "THIN", "GONE"], _frames(), 1_250, held=["THIN", "GONE"],
    )
    assert sorted(a.ticker for a in tradable) == ["GONE", "GOOD", "THIN"]
    assert excluded == []


def test_screen_still_measures_a_held_name_it_lets_through():
    tradable, _ = liquidity.screen(["THIN"], _frames(), 1_250, held=["THIN"])
    assert tradable[0].code == liquidity.ILLIQUID      # kept, but the record is honest
    assert not tradable[0].ok


def test_screen_is_case_insensitive_about_held_names():
    tradable, excluded = liquidity.screen(["THIN"], _frames(), 1_250, held=["thin"])
    assert [a.ticker for a in tradable] == ["THIN"]
    assert excluded == []


def test_as_note_carries_the_measurement_not_just_the_verdict():
    note = liquidity.assess("HQI", _frame(close=15.96, volume=10_500), 1_250).as_note()
    assert note["code"] == liquidity.ILLIQUID
    assert note["dollar_volume"] is not None and note["close"] is not None
    assert "HQI" in note["reason"]


# --------------------------------------------------------------------------
# fetch_frames — the I/O half
# --------------------------------------------------------------------------

def test_fetch_frames_survives_one_bad_ticker(monkeypatch):
    """One name failing to fetch must not fail the run — it becomes UNMEASURED,
    which is a decision about that name only."""
    def fake(ticker, start, end, source=None):
        if ticker == "BOOM":
            raise RuntimeError("upstream 500")
        return _frame()

    import engine.price_history as ph
    monkeypatch.setattr(ph, "get_history_df", fake)
    frames = liquidity.fetch_frames(["GOOD", "BOOM"], date(2026, 9, 1))
    assert frames["BOOM"] is None
    assert frames["GOOD"] is not None
    assert liquidity.assess("BOOM", frames["BOOM"], 1_250).code == liquidity.UNMEASURED


def test_fetch_frames_normalises_and_dedupes_tickers(monkeypatch):
    calls = []

    def fake(ticker, start, end, source=None):
        calls.append(ticker)
        return _frame()

    import engine.price_history as ph
    monkeypatch.setattr(ph, "get_history_df", fake)
    frames = liquidity.fetch_frames(["msft", "MSFT", "", None], date(2026, 9, 1))
    assert calls == ["MSFT"]
    assert set(frames) == {"MSFT"}


def test_fetch_frames_maps_an_empty_result_to_none(monkeypatch):
    import engine.price_history as ph
    monkeypatch.setattr(ph, "get_history_df", lambda *a, **k: pd.DataFrame())
    assert liquidity.fetch_frames(["X"], date(2026, 9, 1))["X"] is None


# --------------------------------------------------------------------------
# format_participation — the measurement has to survive being formatted
# --------------------------------------------------------------------------

def test_a_tiny_participation_does_not_render_as_zero():
    """0.0006% is the normal case for this account. Rendering it as "0.00%"
    would read as an unmeasured field rather than a very small number."""
    assert liquidity.format_participation(0.000006) == "under 0.01%"
    assert "0.00%" != liquidity.format_participation(0.000006)


def test_participation_formats_keep_useful_precision():
    assert liquidity.format_participation(0.0015) == "0.150%"
    assert liquidity.format_participation(0.25) == "25.00%"


def test_participation_formatting_handles_missing_and_zero():
    assert liquidity.format_participation(None) == "an unknown share"
    assert liquidity.format_participation(0) == "0%"


def test_a_real_reason_line_shows_the_number_it_measured():
    verdict = liquidity.assess("NVTS", _frame(close=11.59, volume=18_000_000), 1_250)
    assert verdict.ok
    assert "under 0.01%" in verdict.reason
