"""
The committed S&P 500 snapshot — the neutral universe that makes a validation
result mean something (vs measuring the Screener on the user's own picks).
"""
from engine import universe


def test_sp500_loads_the_committed_snapshot():
    tickers = universe.sp500()
    assert 450 <= len(tickers) <= 520          # ~500 constituents, allowing index drift
    assert "AAPL" in tickers and "MSFT" in tickers and "JNJ" in tickers


def test_sp500_is_clean_and_comment_free():
    tickers = universe.sp500()
    assert not any(t.startswith("#") for t in tickers)   # header lines must be stripped
    assert all(t == t.strip().upper() and t for t in tickers)
    assert len(set(tickers)) == len(tickers)             # no duplicates
    # Class shares use the dash form the price providers expect, not BRK.B
    assert not any("." in t for t in tickers)


def test_sp500_is_cached_and_stable():
    assert universe.sp500() is universe.sp500()


def test_sample_is_deterministic_and_spreads_across_the_alphabet():
    # A random sample would make two validation runs disagree for no reason.
    first, second = universe.sample(20, every=5), universe.sample(20, every=5)
    assert first == second
    assert len(first) == 20
    assert first == universe.sp500()[::5][:20]
    # every=5 must not just return the first 20 names alphabetically
    assert first != universe.sp500()[:20]


def test_sample_without_args_returns_the_whole_universe():
    assert universe.sample() == universe.sp500()


# --------------------------------------------------------------------------
# One ticker per COMPANY. The index lists Alphabet, Fox and News Corp twice,
# once per share class, and the screener scored each class separately — so
# top_decile_long bought GOOG and GOOGL and put 4.2% of its book in Alphabet
# where the sizing rule intended 2.1%.
# --------------------------------------------------------------------------

def test_only_one_share_class_of_a_company_is_screened():
    tickers = universe.sp500()
    assert "GOOGL" in tickers and "GOOG" not in tickers
    assert "FOXA" in tickers and "FOX" not in tickers
    assert "NWSA" in tickers and "NWS" not in tickers


def test_the_committed_snapshot_still_records_both():
    """The file is what the index holds; the dedupe is our decision, in code.
    Losing that distinction would make the survivorship note in the data file
    describe a list that no longer exists."""
    both = universe.constituents()
    assert "GOOG" in both and "GOOGL" in both
    assert len(both) == len(universe.sp500()) + len(universe.SECONDARY_SHARE_CLASSES)


def test_every_pair_in_the_map_is_still_in_the_index():
    """A dropped class the index no longer carries is a map entry doing nothing,
    and a survivor it no longer carries is a company screened out entirely."""
    both = set(universe.constituents())
    for dropped, kept in universe.SECONDARY_SHARE_CLASSES.items():
        assert dropped in both, f"{dropped} is no longer in the snapshot"
        assert kept in both, f"{kept} is no longer in the snapshot"
        assert kept in universe.sp500()


def test_a_spin_off_is_not_a_share_class():
    """HON and HONA are Honeywell International and Honeywell Aerospace — two
    companies sharing a prefix, not two classes of one. A prefix-matching rule
    would have silently dropped a real constituent."""
    tickers = universe.sp500()
    assert "HON" in tickers and "HONA" in tickers


def test_the_sample_inherits_the_dedupe():
    """Validation double-counts a company otherwise: the same return series
    twice inflates the sample an IC is measured on."""
    assert "GOOG" not in universe.sample()
    assert "GOOG" not in universe.sample(every=1)
