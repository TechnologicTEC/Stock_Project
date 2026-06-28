"""
SQLAlchemy models — direct implementation of the schema sketch in
Section 8 of the blueprint, plus one extra table (`ApiCache`) that
isn't in that table but is needed to make the caching rule in
Section 5 ("dashboard pages never call external APIs directly")
actually enforceable for data sources that don't have their own
structured cache table (FRED series, EDGAR filings, Alpaca quotes).
"""
from __future__ import annotations

from datetime import date as date_, datetime

from sqlalchemy import Float, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from engine.time_utils import utcnow


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------
# Portfolio tables
# --------------------------------------------------------------------------

class Holding(Base):
    """Your current positions (manual entry or CSV import — see Section 2:
    there's no free way to auto-sync a real brokerage account)."""

    __tablename__ = "holdings"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(10), index=True)
    shares: Mapped[float] = mapped_column(Float)
    cost_basis: Mapped[float] = mapped_column(Float)
    purchase_date: Mapped[date_]


class Transaction(Base):
    """Buy/sell history, used for performance tracking over time."""

    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(10), index=True)
    type: Mapped[str] = mapped_column(String(4))  # "buy" / "sell"
    shares: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    date: Mapped[date_]


class WatchlistItem(Base):
    """Stocks you're tracking but don't own."""

    __tablename__ = "watchlist"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(10), unique=True, index=True)
    added_at: Mapped[datetime] = mapped_column(default=utcnow)


# --------------------------------------------------------------------------
# Cache tables — everything engine/cache.py reads from / writes to.
# This is the layer that keeps the whole app inside free-tier rate limits.
# --------------------------------------------------------------------------

class PriceCache(Base):
    """TTL-checked cache for OHLCV bars. One row per (ticker, date, source) —
    `source` is kept distinct from `ticker` so Finnhub/yfinance/Alpaca data
    for the same ticker+date can coexist without clobbering each other."""

    __tablename__ = "price_cache"
    __table_args__ = (UniqueConstraint("ticker", "date", "source", name="uq_price_cache_ticker_date_source"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(10), index=True)
    date: Mapped[date_] = mapped_column(index=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[int]
    source: Mapped[str] = mapped_column(String(20))
    fetched_at: Mapped[datetime]


class FundamentalsCache(Base):
    """Cached fundamentals blob (P/E, margins, growth, debt ratios, ...),
    refreshed roughly daily. One row per ticker; the whole payload is
    stored as JSON since the shape varies by data source."""

    __tablename__ = "fundamentals_cache"

    ticker: Mapped[str] = mapped_column(String(10), primary_key=True)
    data_json: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime]


class NewsCache(Base):
    """Cached headlines + FinBERT sentiment scores. Deduped by `url` so
    re-running a news fetch for the same ticker is always safe to repeat."""

    __tablename__ = "news_cache"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(10), index=True)
    headline: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(100))
    url: Mapped[str] = mapped_column(String(500), unique=True)
    published_at: Mapped[datetime]
    sentiment_score: Mapped[float | None] = mapped_column(Float, nullable=True)


class ApiCache(Base):
    """Generic TTL cache for anything that doesn't have its own structured
    table above — FRED series, EDGAR filing indexes, Alpaca snapshots, and
    (Section 6.6) misc tool-call results for the chat assistant later on.
    Also doubles as the staleness marker for news fetches (see cache.py),
    since news_cache itself has no fetched_at column in the Section 8 spec."""

    __tablename__ = "api_cache"

    cache_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value_json: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime]


# --------------------------------------------------------------------------
# Screener / backtesting tables (written to starting Phase 2 / Phase 5,
# but defined now so the schema is stable from day one)
# --------------------------------------------------------------------------

class ScreenerScore(Base):
    """Historical record of screener outputs — also doubles as backtesting
    input, since you can replay how the score would have ranked things."""

    __tablename__ = "screener_scores"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(10), index=True)
    date: Mapped[date_] = mapped_column(index=True)
    overall_score: Mapped[float] = mapped_column(Float)
    sub_scores_json: Mapped[str] = mapped_column(Text)
    recommendation: Mapped[str] = mapped_column(String(20))


class BacktestRun(Base):
    """Saved backtest results, so you can compare strategy tweaks over time."""

    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_config_json: Mapped[str] = mapped_column(Text)
    start_date: Mapped[date_]
    end_date: Mapped[date_]
    results_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
