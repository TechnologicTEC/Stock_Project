"""
scripts/warm_cache.py — the scheduled (GitHub Actions) warm-up. These cover the
news pass added on top of prices+fundamentals: it runs per ticker with
force=True, is skipped when WARM_NEWS is off, and one ticker's failure doesn't
abort the run. Every engine call is mocked — no network, no real DB reads.
"""
import importlib.util
import pathlib
from contextlib import ExitStack
from unittest.mock import patch

_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "warm_cache.py"
_spec = importlib.util.spec_from_file_location("warm_cache_mod", _PATH)
warm_cache = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(warm_cache)


def _common(stack, news_warm=True):
    """Patch out everything main() touches except news, and return nothing —
    news.ensure_fresh is patched by each test so it can assert on it."""
    stack.enter_context(patch.object(warm_cache, "configure"))
    stack.enter_context(patch.object(warm_cache, "all_tickers", return_value=["AAA", "BBB"]))
    stack.enter_context(patch.object(warm_cache, "bot_tickers", return_value=[]))
    stack.enter_context(patch.object(warm_cache.price_history, "refresh", return_value=5))
    stack.enter_context(patch.object(warm_cache.cache, "get_or_fetch_fundamentals"))
    stack.enter_context(patch.object(warm_cache.time, "sleep"))
    stack.enter_context(patch.object(warm_cache, "WARM_NEWS", news_warm))


def test_main_warms_news_for_each_ticker():
    with ExitStack() as stack:
        _common(stack, news_warm=True)
        ensure = stack.enter_context(patch.object(warm_cache.news, "ensure_fresh", return_value=2))
        warm_cache.main()
    assert sorted(c.args[0] for c in ensure.call_args_list) == ["AAA", "BBB"]
    assert all(c.kwargs.get("force") is True for c in ensure.call_args_list)


def test_main_skips_news_when_disabled():
    with ExitStack() as stack:
        _common(stack, news_warm=False)
        ensure = stack.enter_context(patch.object(warm_cache.news, "ensure_fresh"))
        warm_cache.main()
    ensure.assert_not_called()


def test_main_news_failure_is_isolated():
    with ExitStack() as stack:
        _common(stack, news_warm=True)
        ensure = stack.enter_context(
            patch.object(warm_cache.news, "ensure_fresh", side_effect=RuntimeError("boom")))
        warm_cache.main()  # must not raise — a bad ticker can't sink the whole run
    assert ensure.call_count == 2  # attempted for both tickers despite the error


# --------------------------------------------------------------------------
# Bot-traded names. check_fills grades a fill against that day's OPEN, which
# needs a cached bar — and nothing was fetching one for the names the bot
# trades, because it holds neither holdings nor watchlist.
# --------------------------------------------------------------------------

def test_bot_traded_names_get_their_prices_warmed():
    with ExitStack() as stack:
        _common(stack, news_warm=False)
        stack.enter_context(patch.object(warm_cache, "bot_tickers",
                                         return_value=["MU", "GOOG"]))
        refresh = stack.enter_context(
            patch.object(warm_cache.price_history, "refresh", return_value=5))
        warm_cache.main()
    warmed = [c.args[0] for c in refresh.call_args_list]
    assert warmed == ["AAA", "BBB", "MU", "GOOG"]


def test_a_bot_name_already_held_is_not_warmed_twice():
    with ExitStack() as stack:
        _common(stack, news_warm=False)
        stack.enter_context(patch.object(warm_cache, "bot_tickers",
                                         return_value=["AAA", "MU"]))
        refresh = stack.enter_context(
            patch.object(warm_cache.price_history, "refresh", return_value=5))
        warm_cache.main()
    warmed = [c.args[0] for c in refresh.call_args_list]
    assert warmed == ["AAA", "BBB", "MU"]


def test_bot_names_skip_fundamentals_and_news():
    """Prices only. The full treatment is ~10s a name — across a 50-name decile
    book that turns a 3-minute job into a 20-minute one for data nothing reads."""
    with ExitStack() as stack:
        _common(stack, news_warm=True)
        stack.enter_context(patch.object(warm_cache, "bot_tickers", return_value=["MU"]))
        stack.enter_context(patch.object(warm_cache.price_history, "refresh", return_value=5))
        funds = stack.enter_context(
            patch.object(warm_cache.cache, "get_or_fetch_fundamentals"))
        newsed = stack.enter_context(
            patch.object(warm_cache.news, "ensure_fresh", return_value=1))
        warm_cache.main()
    assert [c.args[0] for c in funds.call_args_list] == ["AAA", "BBB"]
    assert [c.args[0] for c in newsed.call_args_list] == ["AAA", "BBB"]


def test_one_bot_price_failure_does_not_abort_the_rest():
    def flaky(ticker, *a, **k):
        if ticker == "MU":
            raise RuntimeError("upstream 500")
        return 5

    with ExitStack() as stack:
        _common(stack, news_warm=False)
        stack.enter_context(patch.object(warm_cache, "bot_tickers",
                                         return_value=["MU", "GOOG"]))
        refresh = stack.enter_context(
            patch.object(warm_cache.price_history, "refresh", side_effect=flaky))
        warm_cache.main()
    assert "GOOG" in [c.args[0] for c in refresh.call_args_list]
