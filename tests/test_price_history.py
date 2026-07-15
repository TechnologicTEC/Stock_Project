"""
price_history.refresh() — the unconditional fetch used by the scheduled warm job
(scripts/warm_cache.py) and forced refreshes. Unlike ensure_cached, it must hit
the source every call (that's how the warm job pulls in the day's new bar).
Network is mocked at the yfinance client, matching the rest of the suite.
"""
from datetime import date
from unittest.mock import patch

from engine import cache, price_history


def _bars(*days):
    return [{"date": date(2026, 6, d), "open": 1.0, "high": 2.0, "low": 0.5,
             "close": float(d), "volume": 100} for d in days]


def test_refresh_fetches_and_caches(monkeypatch):
    monkeypatch.delenv("PRICE_HISTORY_PREFER_ALPACA", raising=False)  # keep yfinance first so the mock is used
    with patch("engine.price_history.yfinance_client.get_historical_ohlcv", return_value=_bars(1, 2, 3)) as fetch:
        n = price_history.refresh("aapl", date(2026, 6, 1), date(2026, 6, 3))

    assert n == 3 and fetch.called
    cached = {h["date"]: h["close"]
              for h in cache.get_price_history("AAPL", "yfinance", date(2026, 6, 1), date(2026, 6, 3))}
    assert cached == {date(2026, 6, 1): 1.0, date(2026, 6, 2): 2.0, date(2026, 6, 3): 3.0}


def test_refresh_is_unconditional_unlike_ensure_cached(monkeypatch):
    monkeypatch.delenv("PRICE_HISTORY_PREFER_ALPACA", raising=False)
    with patch("engine.price_history.yfinance_client.get_historical_ohlcv", return_value=_bars(1, 2, 3)) as fetch:
        price_history.refresh("AAPL", date(2026, 6, 1), date(2026, 6, 3))
        price_history.refresh("AAPL", date(2026, 6, 1), date(2026, 6, 3))
        assert fetch.call_count == 2  # refresh always re-fetches...

        # ...whereas ensure_cached, with the range already cached, does NOT touch the source.
        fetch.reset_mock()
        price_history.ensure_cached("AAPL", date(2026, 6, 1), date(2026, 6, 3))
        assert fetch.call_count == 0


def test_refresh_returns_zero_and_caches_nothing_when_source_empty(monkeypatch):
    monkeypatch.delenv("PRICE_HISTORY_PREFER_ALPACA", raising=False)
    # yfinance empty; Alpaca not configured in tests -> nothing to cache, no crash.
    with patch("engine.price_history.yfinance_client.get_historical_ohlcv", return_value=[]):
        with patch("engine.data_sources.alpaca_client.is_configured", return_value=False):
            n = price_history.refresh("ZZZZ", date(2026, 6, 1), date(2026, 6, 3))
    assert n == 0
    assert cache.get_price_history("ZZZZ", "yfinance", date(2026, 6, 1), date(2026, 6, 3)) == []


# --------------------------------------------------------------------------
# canonical_source — decided identically everywhere so local and the Space (and
# a shared cache) reconstruct the SAME series. This is the local-vs-online fix.
# --------------------------------------------------------------------------

def test_canonical_source_explicit_env_wins(monkeypatch):
    monkeypatch.setenv("PRICE_HISTORY_SOURCE", "alpaca")
    assert price_history.canonical_source() == "alpaca"
    monkeypatch.setenv("PRICE_HISTORY_SOURCE", "yfinance")
    assert price_history.canonical_source() == "yfinance"
    # an unknown value falls back to the safe default rather than a bad label
    monkeypatch.setenv("PRICE_HISTORY_SOURCE", "bloomberg")
    assert price_history.canonical_source() == "yfinance"


def test_canonical_source_prefers_alpaca_when_configured(monkeypatch):
    monkeypatch.delenv("PRICE_HISTORY_SOURCE", raising=False)  # conftest sets it; clear for this test
    with patch("engine.data_sources.alpaca_client.is_configured", return_value=True):
        assert price_history.canonical_source() == "alpaca"
    with patch("engine.data_sources.alpaca_client.is_configured", return_value=False):
        assert price_history.canonical_source() == "yfinance"


def test_fetch_bars_uses_exactly_the_named_provider(monkeypatch):
    # Honest labelling: a bar cached under "alpaca" must have come from Alpaca, so
    # a shared cache never mixes providers into one inconsistent series.
    with patch("engine.price_history._alpaca_bars", return_value=_bars(1)) as alp, \
         patch("engine.price_history._yf_bars", return_value=_bars(2)) as yf:
        assert price_history._fetch_bars("AAPL", date(2026, 6, 1), date(2026, 6, 1), "alpaca") == _bars(1)
        assert alp.called and not yf.called
