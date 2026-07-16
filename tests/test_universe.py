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
