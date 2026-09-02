"""
App-layer memoization (Streamlit). Streamlit reruns the whole script on every
widget interaction; without caching, each rerun re-queries the (remote Tokyo) DB
and recomputes. These `st.cache_data` wrappers keep heavy reads in-process for a
few minutes so interactions are instant.

⚠️ MULTI-USER SAFETY: `st.cache_data` is a **process-global** cache shared by
every session/user, keyed by the function arguments. So anything per-user MUST
take `user_id` as an argument — that's what keeps user A's data out of user B's
cache. The DB is already scoped to the current user by gate(); `user_id` here is
purely the cache key, never dropped. Portfolio writes call `clear()` so the chart
and health reflect the change immediately rather than after the TTL.
"""
from __future__ import annotations

from datetime import date, datetime

import streamlit as st

from engine import health, news, portfolio

_TTL_SECONDS = 300  # 5 min; also explicitly cleared on portfolio writes


@st.cache_data(ttl=_TTL_SECONDS, show_spinner=False)
def value_history(user_id: int | None, start: date, end: date) -> list[dict]:
    """Portfolio value-over-time — per user (user_id is the cache key)."""
    return portfolio.get_value_history(start, end)


@st.cache_data(ttl=_TTL_SECONDS, show_spinner=False)
def health_report(user_id: int | None, lookback_days: int):
    """Portfolio health metrics — per user (user_id is the cache key)."""
    return health.get_health_report(lookback_days=lookback_days)


@st.cache_data(ttl=_TTL_SECONDS, show_spinner=False)
def portfolio_summary(user_id: int | None):
    """Aggregate value/gain-loss/day-change — per user (user_id is the cache key)."""
    return portfolio.get_portfolio_summary()


@st.cache_data(ttl=_TTL_SECONDS, show_spinner=False)
def live_valuation(user_id: int | None):
    """Per-holding valuation (prices are already source-cached) — per user."""
    return portfolio.get_live_valuation()


@st.cache_data(ttl=_TTL_SECONDS, show_spinner=False)
def upcoming_earnings(tickers: tuple[str, ...], within_days: int = 21) -> list[dict]:
    """Which of `tickers` report earnings within `within_days`, soonest first —
    shared market data, keyed by the ticker set. Dates are source-cached (24h)."""
    from engine import earnings

    out = []
    for ticker in tickers:
        nxt = earnings.next_earnings(ticker)
        if nxt and nxt.get("days_until") is not None and 0 <= nxt["days_until"] <= within_days:
            out.append({"ticker": ticker, **nxt})
    return sorted(out, key=lambda e: e["days_until"])


@st.cache_data(ttl=_TTL_SECONDS, show_spinner=False)
def news_analysis(ticker: str):
    """News + sentiment for a ticker — shared market data, keyed by ticker."""
    return news.analyze_ticker(ticker)


@st.cache_data(ttl=_TTL_SECONDS, show_spinner=False)
def screener_ratings(tickers: tuple[str, ...]) -> dict:
    """{ticker: {"score", "recommendation"}} from the Investment Screener — shared
    market data, keyed by the ticker set. Heavy (per-ticker analyst calls), so
    it's opt-in on the Portfolio page and cached here. Imported lazily to keep
    the screener's stack off every page's import."""
    from engine import screener

    return {r.ticker: {"score": r.overall_score, "recommendation": r.recommendation}
            for r in screener.screen_tickers(list(tickers))}


@st.cache_data(ttl=_TTL_SECONDS, show_spinner=False)
def watchlist_performance(user_id: int | None,
                          items: tuple[tuple[str, datetime | date], ...]) -> list[dict]:
    """Since-added price change per watchlist entry — per user.

    Cached because it costs a quote plus a history lookup *per ticker* and it
    renders inside an expander: Streamlit runs an expander's body whether or not
    it's open, so without this a 30-name watchlist would pay that on every
    rerun of the page, collapsed or not. `items` carries the added-dates so
    adding or removing a ticker re-keys the cache instead of showing stale rows.
    """
    from engine import watchlist

    return watchlist.performance_since_added(
        [{"ticker": t, "added_at": added} for t, added in items])


@st.cache_data(ttl=_TTL_SECONDS, show_spinner=False)
def signal_summary(ticker: str) -> dict:
    """Cross-signal agreement for a ticker — shared market data, keyed by ticker.
    Runs the Screener, so it's opt-in on the page and cached here."""
    from engine import signals

    return signals.aggregate_signals(ticker)


# --------------------------------------------------------------------------
# Trading bot. Note the deliberate absence of a `user_id` cache key on these:
# bot_config, bot_decisions and bot_equity_snapshots are SHARED tables with no
# user_id column (the bot runs as a batch job, not as a person), so there is no
# per-user data to keep apart. Everything above takes user_id precisely because
# it is per-user; these don't because they aren't.
# --------------------------------------------------------------------------

@st.cache_data(ttl=_TTL_SECONDS, show_spinner=False)
def bot_configs() -> list[dict]:
    """Every strategy's control row — the tab list and the slot counts."""
    from engine.bot import journal

    return journal.list_configs()


@st.cache_data(ttl=_TTL_SECONDS, show_spinner=False)
def bot_equity_curve(strategy: str) -> list[dict]:
    """One strategy's daily snapshots, oldest first. The bot writes one row a
    day, so a 5-minute TTL is generous."""
    from engine.bot import journal

    return journal.equity_curve(strategy)


@st.cache_data(ttl=_TTL_SECONDS, show_spinner=False)
def bot_decisions(strategy: str | None, limit: int) -> list[dict]:
    """Newest-first journal rows; `strategy=None` spans all of them."""
    from engine.bot import journal

    return journal.recent_decisions(strategy, limit)


@st.cache_data(ttl=120, show_spinner=False)
def bot_account_view(key_env_prefix: str) -> dict:
    """Live Alpaca positions for one strategy. Shorter TTL than the DB reads —
    it's the only thing here that changes intraday — and it never raises, so a
    missing key pair costs one panel rather than the page."""
    from engine.bot import live

    return live.account_view(key_env_prefix)


@st.cache_data(ttl=_TTL_SECONDS, show_spinner=False)
def bot_leaderboard() -> dict:
    """The cached S&P 500 ranking the screener strategies trade off.

    Read through the strategies' own loader rather than the screener's, so the
    page shows exactly what the bot would act on — including refusing a ranking
    the bot would consider too stale to trade.
    """
    from datetime import date as _date

    from engine.bot.strategies import screener_common

    try:
        return screener_common.load_leaderboard(_date.today())
    except Exception as exc:                 # noqa: BLE001 — a panel, not the page
        return {"rows": [], "error": str(exc)}


@st.cache_data(ttl=_TTL_SECONDS, show_spinner=False)
def bot_decile_spread() -> dict | None:
    """Top-minus-bottom decile return since the last rebalance snapshot."""
    from engine.bot import decile_spread

    try:
        return decile_spread.current()
    except Exception:                        # noqa: BLE001
        return None


@st.cache_data(ttl=_TTL_SECONDS, show_spinner=False)
def bot_creator_mentions() -> list[dict]:
    """The creator mention window the conviction strategy reads, with videos."""
    from engine.bot.strategies import creator_conviction
    from engine import creator_signals

    try:
        return creator_signals.mention_leaderboard(
            days=creator_conviction.WINDOW_DAYS, min_mentions=1)
    except Exception:                        # noqa: BLE001
        return []


@st.cache_data(ttl=_TTL_SECONDS, show_spinner=False)
def bot_position_names(tickers: tuple[str, ...]) -> dict[str, str]:
    """{TICKER: company name} for a strategy's holdings.

    Almost free: the S&P leaderboard and the creator mention table are already
    loaded above for other reasons and both carry a name, so this is a
    dictionary join for every S&P or creator holding. Only a name in neither
    reaches the profile lookup, which is itself DB-cached for 30 days.
    """
    from engine import portfolio
    from engine.bot import positions

    return positions.resolve_names(
        tickers,
        leaderboard_rows=(bot_leaderboard().get("rows") or []),
        mentions=bot_creator_mentions(),
        lookup=portfolio.get_profile_cached,
    )


@st.cache_data(ttl=_TTL_SECONDS, show_spinner=False)
def bot_fills(strategy: str) -> list[dict]:
    """Every journalled trade for one strategy, oldest first — the history the
    holding dates and the reconstructed book are both replayed from."""
    from engine.bot import journal

    return journal.fills(strategy)


@st.cache_data(ttl=_TTL_SECONDS, show_spinner=False)
def bot_reconstructed_book(strategy: str) -> list[dict]:
    """The book rebuilt from the journal and priced at the last cached close.

    This is what the holdings panel falls back to when the Alpaca keys aren't in
    the environment — which is the normal case on the deployed Space, where only
    GitHub Actions holds them. Weaker than the broker's own numbers and labelled
    as such on the page, but it is the bot's own trade history rather than an
    empty panel.
    """
    from datetime import date as _date

    from engine import cache as _price_cache
    from engine import price_history
    from engine.bot import positions

    try:
        fills = bot_fills(strategy)
        if not fills:
            return []

        # Two passes over the same bars. The FIRST fill's date sets the window,
        # because a dollar order has to be sized at the price it filled at —
        # today's close cannot say how many shares $500 bought in September.
        tickers = {(f.get("ticker") or "").upper() for f in fills if f.get("ticker")}
        first = min((f["decided_at"] for f in fills if f.get("decided_at")), default=None)
        start = first.date() if hasattr(first, "date") else (first or _date.today())
        bars = _price_cache.get_bars_for(
            tickers, price_history.canonical_source(), start, _date.today())

        book = positions.book_from_fills(
            fills, fill_price=positions.fill_price_lookup(bars))
        if not book:
            return []
        closes = {t: [(d, c) for d, _o, c in series] for t, series in bars.items()}
        return positions.price_book(book, closes)
    except Exception:                        # noqa: BLE001 — a panel, not the page
        return []


@st.cache_data(ttl=_TTL_SECONDS, show_spinner=False)
def bot_sma_frame(ticker: str, lookback_days: int) -> list[dict]:
    """Closes plus the two SMAs golden_cross decides on, oldest first.

    Computed with the strategy's own `moving_averages`, so the chart cannot
    drift from the numbers the bot acted on.
    """
    from datetime import date as _date
    from datetime import timedelta as _timedelta

    from engine import price_history
    from engine.bot.strategies import golden_cross

    try:
        today = _date.today()
        df = price_history.get_history_df(ticker, today - _timedelta(days=lookback_days), today)
        if df is None or df.empty or "close" not in df:
            return []
        fast, slow = golden_cross.moving_averages(df["close"])
        out = []
        for when, close, f, s_ in zip(df.index, df["close"], fast, slow):
            out.append({"date": when, "close": float(close),
                        "fast": None if f != f else float(f),
                        "slow": None if s_ != s_ else float(s_)})
        return out
    except Exception:                        # noqa: BLE001
        return []


def clear() -> None:
    """Drop all cached results. Call after any write so nothing shows stale."""
    st.cache_data.clear()
