"""
Per-strategy Alpaca paper accounts.

Each strategy trades its own $10k paper account, so Alpaca computes its equity
curve directly and no attribution logic of ours sits in between. That needs one
thing `engine/data_sources/alpaca_client.py` can't give us: it memoizes its
clients with `@lru_cache(maxsize=1)` and reads a single global key pair, which
is right for the manual Paper Trading page and wrong for six accounts.

So this module keeps its own cache, keyed on the key pair. The existing
single-account functions are left completely untouched — the Paper Trading page
keeps behaving exactly as it did.

Everything here is paper-only and asserted to be (see `assert_paper`): the
client is constructed with paper=True, and we verify the resolved endpoint
before any order is placed rather than trusting the flag.
"""
from __future__ import annotations

from functools import lru_cache

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.trading.client import TradingClient

from engine import config, credentials  # noqa: F401  (config: side effect loads .env)

# The only endpoint this module may ever talk to. Checked at runtime, not
# assumed from paper=True — a wrong endpoint here spends real money.
PAPER_HOST = "paper-api.alpaca.markets"


class BotAccountError(RuntimeError):
    """Missing or unusable credentials for a strategy's account."""


def keys_for(key_env_prefix: str) -> tuple[str, str]:
    """Resolve `<PREFIX>_KEY` / `<PREFIX>_SECRET` for one strategy.

    e.g. "ALPACA_GOLDEN_CROSS" -> (ALPACA_GOLDEN_CROSS_KEY, ALPACA_GOLDEN_CROSS_SECRET).
    Raises rather than falling back to the shared ALPACA_API_KEY: silently
    trading the *manual* paper account would corrupt the one record we can't
    reconstruct.
    """
    prefix = (key_env_prefix or "").strip().rstrip("_")
    if not prefix:
        raise BotAccountError("No key_env_prefix configured for this strategy.")

    key = credentials.get(f"{prefix}_KEY")
    secret = credentials.get(f"{prefix}_SECRET")
    if not key or not secret:
        missing = [n for n, v in ((f"{prefix}_KEY", key), (f"{prefix}_SECRET", secret)) if not v]
        raise BotAccountError(
            f"Missing Alpaca credentials for this strategy: {', '.join(missing)}. "
            "Add them as GitHub repository secrets (and to .env for local runs)."
        )
    return key, secret


@lru_cache(maxsize=16)
def trading_client_for(key: str, secret: str) -> TradingClient:
    """A paper TradingClient for one account. Cached per key pair, so five
    strategies in one process get five clients rather than fighting over one."""
    return TradingClient(key, secret, paper=True)


@lru_cache(maxsize=16)
def data_client_for(key: str, secret: str) -> StockHistoricalDataClient:
    """Market data works with any valid pair, so each strategy uses its own —
    which also spreads the per-key rate limit across five pairs instead of one."""
    return StockHistoricalDataClient(key, secret)


def assert_paper(client: TradingClient) -> None:
    """Fail loudly unless this client is pointed at the paper endpoint.

    paper=True is already hardcoded at construction; this verifies the *resolved*
    endpoint anyway, so no future refactor, config change, or SDK default can
    quietly promote the bot to real money. Called before every order.
    """
    # alpaca-py stores this as a BaseURL enum, whose str() is the MEMBER NAME
    # ("BaseURL.TRADING_PAPER"), not the URL — so unwrap .value first. Getting
    # this wrong fails closed (every account is refused), which is the right
    # direction for a safety check but still a bug.
    raw = getattr(client, "_base_url", "") or ""
    base_url = str(getattr(raw, "value", raw))
    sandbox = getattr(client, "_sandbox", None)
    if PAPER_HOST not in base_url or sandbox is False:
        raise BotAccountError(
            f"Refusing to trade: client endpoint is {base_url!r} (sandbox={sandbox!r}), "
            f"which is not the paper endpoint ({PAPER_HOST}). This is a bug, not a config problem."
        )


def clients_for(key_env_prefix: str) -> tuple[TradingClient, StockHistoricalDataClient]:
    """The pair of clients for one strategy, paper-asserted before returning."""
    key, secret = keys_for(key_env_prefix)
    trading = trading_client_for(key, secret)
    assert_paper(trading)
    return trading, data_client_for(key, secret)


def reset_cache() -> None:
    """Drop cached clients — tests, and any place credentials change mid-process."""
    trading_client_for.cache_clear()
    data_client_for.cache_clear()
