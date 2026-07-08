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
