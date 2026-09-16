"""engine/bot/corporate_actions.py — spotting a split the broker ignored.

Alpaca's paper accounts do not process corporate actions (confirmed with their
support, 2026-09-16). APH went 2-for-1 on 2026-09-03 and the position stayed at
1.268 shares @ $157.70 against a $77.66 quote, reading $98 instead of $197.
These pin the detection against those real numbers.
"""
from datetime import date

import pytest

from engine.bot import corporate_actions as ca

APH_SPLIT = {"ex_date": date(2026, 9, 3), "old_rate": 1, "new_rate": 2}


def _position(ticker="APH", qty=1.268199573, entry=157.696, price=77.66):
    return {"ticker": ticker, "qty": qty, "avg_entry_price": entry, "current_price": price}


def test_it_catches_the_real_aph_case():
    [row] = ca.unapplied([_position()], {"APH": [APH_SPLIT]})
    assert row["ratio"] == 2.0
    assert row["reported_qty"] == pytest.approx(1.268199573)
    assert row["expected_qty"] == pytest.approx(2.536399146)
    assert row["reported_value"] == pytest.approx(98.48, abs=0.05)
    assert row["expected_value"] == pytest.approx(196.97, abs=0.05)
    assert row["shortfall"] == pytest.approx(98.48, abs=0.05)
    assert row["adjusted_entry"] == pytest.approx(78.848)


def test_a_split_the_broker_DID_apply_is_not_flagged():
    """Adjusted, entry and price sit side by side rather than a ratio apart."""
    applied = _position(qty=2.536399146, entry=78.848, price=77.66)
    assert ca.unapplied([applied], {"APH": [APH_SPLIT]}) == []


def test_a_split_from_before_you_owned_it_is_ignored():
    """It is already in the price you paid."""
    assert ca.unapplied([_position()], {"APH": [APH_SPLIT]},
                        opened={"APH": date(2026, 9, 10)}) == []
    # ...but one after the holding opened still counts.
    assert len(ca.unapplied([_position()], {"APH": [APH_SPLIT]},
                            opened={"APH": date(2026, 9, 1)})) == 1


def test_a_reverse_split_is_caught_and_reads_as_an_overstatement():
    """A 1-for-10 reverse split unapplied makes the position look 10x too big,
    so the shortfall is negative — the account is flattered, not docked."""
    reverse = {"ex_date": date(2026, 6, 1), "old_rate": 10, "new_rate": 1}
    [row] = ca.unapplied([_position("VNRX", qty=100.0, entry=0.4, price=4.0)],
                         {"VNRX": [reverse]})
    assert row["ratio"] == pytest.approx(0.1)
    assert row["expected_qty"] == pytest.approx(10.0)
    assert row["shortfall"] < 0


def test_two_splits_since_entry_compound():
    splits = [{"ex_date": date(2026, 3, 1), "old_rate": 1, "new_rate": 2},
              {"ex_date": date(2026, 6, 1), "old_rate": 1, "new_rate": 2}]
    [row] = ca.unapplied([_position(qty=1.0, entry=400.0, price=100.0)], {"APH": splits})
    assert row["ratio"] == 4.0
    assert row["expected_qty"] == 4.0


def test_an_ordinary_loss_is_not_mistaken_for_a_split():
    """Down 20% with a split on record, but entry/price is nowhere near 2."""
    assert ca.unapplied([_position(entry=97.0, price=77.66)], {"APH": [APH_SPLIT]}) == []


def test_no_split_on_record_means_nothing_to_report():
    assert ca.unapplied([_position()], {}) == []


def test_rows_missing_the_numbers_are_skipped_rather_than_crashing():
    assert ca.unapplied([{"ticker": "APH", "qty": None, "avg_entry_price": None,
                          "current_price": None}], {"APH": [APH_SPLIT]}) == []


def test_describe_says_what_it_should_be_not_just_what_is():
    [row] = ca.unapplied([_position()], {"APH": [APH_SPLIT]})
    text = ca.describe(row)
    assert "2-for-1 forward split" in text and "2026-09-03" in text
    assert "$98" in text and "$196" in text          # both the wrong and right value
    assert "nothing to do with the strategy" in text


def test_fetch_never_raises_when_the_feed_is_unreachable(monkeypatch):
    monkeypatch.setattr("engine.bot.accounts.keys_for",
                        lambda _p: (_ for _ in ()).throw(RuntimeError("no keys")))
    assert ca.fetch_splits("ALPACA_X", ["APH"], date(2026, 9, 1), date(2026, 9, 16)) == {}
