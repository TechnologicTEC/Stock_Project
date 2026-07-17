"""
Point-in-time (historical) Screener scoring — steps 2 & 3 of screener validation.

Given a ticker and a past date, this reconstructs the *fundamental* inputs the
live Screener uses — but only from information knowable on that date (SEC EDGAR
filing dates + the historical price we already cache) — and then runs them
through the **exact same scoring curves** as engine/screener.py. That "same
curves" bit is the whole point: we're measuring the real Screener, not a
lookalike.

Two factors can't be reconstructed historically for free — analyst confidence
(no free point-in-time consensus) and news sentiment (needs GDELT, step 6) — so
they score None here, and the Screener's existing weight-redistribution handles
that exactly as it does when those inputs are missing on a live run.

Ratios are built from trailing-twelve-month (TTM) figures, summed from the four
most recent quarters filed on/before the as-of date:
  P/E = market cap / TTM net income   ·   P/B = market cap / equity
  P/S = market cap / TTM revenue      ·   margins/ROE from TTM vs revenue/equity
  revenue & EPS growth = TTM now vs. TTM a year (4 quarters) earlier
"""
from __future__ import annotations

from datetime import date, timedelta

from engine import cache, price_history, screener
from engine.data_sources import analyst_history, edgar_fundamentals, finnhub_client, gdelt_client


def _visible_quarters(series: list[dict], as_of: date) -> list[dict]:
    """The metric's quarterly facts filed on/before `as_of`, oldest-end first."""
    visible = [f for f in series if date.fromisoformat(f["filed"]) <= as_of]
    return sorted(visible, key=lambda f: f["end"])


def _ttm_sum(series: list[dict], as_of: date, quarters_back: int = 0) -> float | None:
    """Sum of 4 quarters ending `quarters_back` quarters ago (0 = the latest
    four; 4 = the four before that, i.e. a year earlier). None if not enough
    history is public yet."""
    visible = _visible_quarters(series, as_of)
    if len(visible) < quarters_back + 4:
        return None
    end = len(visible) - quarters_back
    return sum(f["value"] for f in visible[end - 4:end])


def _latest_value(series: list[dict], as_of: date) -> float | None:
    """Most recent instantaneous value (equity, debt, shares) public by `as_of`."""
    visible = _visible_quarters(series, as_of)
    return visible[-1]["value"] if visible else None


def _last_close_on_or_before(price_df, as_of: date) -> float | None:
    if price_df is None or price_df.empty or "close" not in price_df.columns:
        return None
    closes = price_df[[d <= as_of for d in price_df.index]]["close"]
    return float(closes.iloc[-1]) if len(closes) else None


def _price_as_of(ticker: str, as_of: date) -> float | None:
    """Close on the last trading day on/before `as_of` (point-in-time price).
    A wide-ish window keeps yfinance from flaking on very short ranges."""
    return _last_close_on_or_before(
        price_history.get_history_df(ticker, as_of - timedelta(days=20), as_of), as_of
    )


def _profile_bits(ticker: str) -> tuple[str, str | None, str | None]:
    """Sector bucket, raw industry, and company name from the cached Finnhub
    profile. Sectors rarely change, so using today's for a past valuation curve
    is a small, documented approximation; the name is used to match GDELT's
    organization field for historical news tone. Falls back gracefully."""
    try:
        profile = cache.get_or_fetch(
            f"profile:{ticker}", screener.PROFILE_TTL_SECONDS,
            lambda: finnhub_client.get_company_profile(ticker),
        )
        raw = (profile or {}).get("sector")
        return screener.classify_sector_bucket(raw), raw, (profile or {}).get("name")
    except Exception:
        return screener.DEFAULT_SECTOR_BUCKET, None, None


def _historical_sentiment_factor(company_name: str | None, as_of: date):
    """The news-sentiment factor, reconstructed point-in-time from GDELT tone
    (the live scorer uses *current* news, which would be look-ahead here)."""
    if not company_name:
        return screener.FactorResult(score=None, reasons=["No company name available for news lookup"])
    score = gdelt_client.sentiment_as_of(company_name, as_of)
    if score is None:
        return screener.FactorResult(score=None, reasons=["No GDELT news coverage in the 30 days before this date"])
    return screener.FactorResult(
        score=score, reasons=[f"GDELT news tone over the prior 30 days → {score:.0f}/100 (50 = neutral)"]
    )


def _pct_growth(now: float | None, prior: float | None) -> float | None:
    if now is None or prior is None or prior <= 0:
        return None
    return (now / prior - 1.0) * 100.0


def pit_fundamentals_metrics(ticker: str, as_of: date, price_df=None) -> dict | None:
    """Reconstruct the Screener's fundamental inputs as of `as_of`, keyed by the
    exact Finnhub field names the Screener reads (so it slots straight into the
    existing scorers). Returns None if EDGAR has no data for this ticker, or {}
    if there isn't enough filed history yet to compute anything. `price_df`, if
    given, supplies the point-in-time price (avoids a second network fetch)."""
    series = edgar_fundamentals.get_pit_fundamentals(ticker)
    if not series:
        return None

    rev_ttm = _ttm_sum(series.get("revenue", []), as_of)
    rev_prev = _ttm_sum(series.get("revenue", []), as_of, quarters_back=4)
    ni_ttm = _ttm_sum(series.get("net_income", []), as_of)
    gp_ttm = _ttm_sum(series.get("gross_profit", []), as_of)
    eps_ttm = _ttm_sum(series.get("eps_diluted", []), as_of)
    eps_prev = _ttm_sum(series.get("eps_diluted", []), as_of, quarters_back=4)
    equity = _latest_value(series.get("equity", []), as_of)
    debt = _latest_value(series.get("long_term_debt", []), as_of)
    shares = _latest_value(series.get("shares", []), as_of)

    price = _last_close_on_or_before(price_df, as_of) if price_df is not None else _price_as_of(ticker, as_of)
    market_cap = price * shares if price and shares else None

    metrics: dict[str, float] = {}
    if market_cap and ni_ttm and ni_ttm > 0:
        metrics["peTTM"] = market_cap / ni_ttm
    if market_cap and equity and equity > 0:
        metrics["pbAnnual"] = market_cap / equity
    if market_cap and rev_ttm and rev_ttm > 0:
        metrics["psTTM"] = market_cap / rev_ttm
    if (g := _pct_growth(rev_ttm, rev_prev)) is not None:
        metrics["revenueGrowthTTMYoy"] = g
    if (g := _pct_growth(eps_ttm, eps_prev)) is not None:
        metrics["epsGrowthTTMYoy"] = g
    if gp_ttm is not None and rev_ttm:
        metrics["grossMarginTTM"] = gp_ttm / rev_ttm * 100.0
    if ni_ttm is not None and rev_ttm:
        metrics["netProfitMarginTTM"] = ni_ttm / rev_ttm * 100.0
    if ni_ttm is not None and equity and equity > 0:
        metrics["roeTTM"] = ni_ttm / equity * 100.0
    if debt is not None and equity and equity > 0:
        metrics["totalDebt/totalEquityAnnual"] = debt / equity
    return metrics


def historical_raw_data(ticker: str, as_of: date,
                        include_analyst: bool = True) -> tuple[object, str | None] | None:
    """Everything knowable about `ticker` on `as_of`, as a `TickerRawData` plus the
    company name — the **expensive, per-ticker** half of a reconstruction (prices,
    EDGAR filings, profile, analyst events). None if EDGAR has nothing.

    Split out from historical_screener_score so callers can gather a WHOLE
    UNIVERSE's raw data for one date and then score it as a batch. The scorers
    already take `dict[str, TickerRawData]`; they just never get more than one
    ticker today, which is why their cross-sectional percentiles are degenerate."""
    ticker = ticker.strip().upper()
    # One price fetch feeds both the as-of price (for P/E-P/B-P/S) and the
    # momentum factor's window.
    price_df = price_history.get_history_df(
        ticker, as_of - timedelta(days=screener.MOMENTUM_LOOKBACK_DAYS), as_of
    )
    metrics = pit_fundamentals_metrics(ticker, as_of, price_df=price_df)
    if metrics is None:
        return None

    sector_bucket, raw_industry, company_name = _profile_bits(ticker)

    # Reconstructed analyst consensus (step 4) supplies the recommendation
    # component; price targets and insider data have no free point-in-time
    # history, so they stay None and the analyst scorer uses what it has.
    #
    # `include_analyst=False` skips it entirely. That's not an optimisation — it's
    # the difference between a batch job finishing and timing out. The rating-event
    # source is Yahoo via yfinance, which BLOCKS datacenter IPs, and yfinance
    # doesn't fail fast when blocked: it hangs and retries. On a GitHub runner that
    # turned ~18s/ticker into ~56s/ticker (an 8-hour ETA on 503 names) to fetch
    # data that comes back empty there anyway. Callers that can't reach Yahoo
    # should pass False and lose only a factor they were never going to get.
    recommendation = analyst_history.recommendation_as_of(ticker, as_of) if include_analyst else None

    raw = screener.TickerRawData(
        ticker=ticker, fundamentals=metrics, price_df=price_df,
        recommendation=recommendation, price_target=None, insider_mspr=None,
        sector_bucket=sector_bucket, raw_industry=raw_industry, errors=[],
    )
    return raw, company_name


def score_reconstructed_batch(raw_by_ticker: dict, as_of: date, *,
                              company_names: dict | None = None,
                              include_news: bool = True) -> dict[str, dict]:
    """Score a whole batch of point-in-time raw data at once — the **cheap** half.

    Pass the entire universe and the factor scorers see every name on the date,
    which is the precondition for cross-sectional (percentile / sector-relative)
    scoring. Passing one ticker reproduces the old behaviour EXACTLY: today the
    scorers use absolute curves, and their peer percentiles feed only the
    explanation text, never the score (see screener._curve_reason). So batching
    changes the *reasons*, never the numbers — which is what makes the date-major
    rewrite verifiable against the ticker-major baseline."""
    company_names = company_names or {}
    # Every factor uses the live scorers *except* sentiment: the live sentiment
    # scorer reads current news (look-ahead for a past date), so reconstruct it
    # point-in-time from GDELT tone instead (step 6) — but only if asked, since
    # that's a BigQuery query. Otherwise it's None and its weight redistributes.
    scored_factors = {name: screener.FACTOR_SCORERS[name](raw_by_ticker)
                      for name in screener.FACTOR_WEIGHTS if name != "sentiment"}

    out: dict[str, dict] = {}
    for ticker in raw_by_ticker:
        factors = {name: scored_factors[name][ticker] for name in scored_factors}
        factors["sentiment"] = (
            _historical_sentiment_factor(company_names.get(ticker), as_of) if include_news
            else screener.FactorResult(score=None, reasons=["News sentiment not included in this run"])
        )
        overall = screener.combine_factor_scores(factors)
        out[ticker] = {
            "ticker": ticker,
            "as_of": as_of,
            "overall_score": overall,
            "recommendation": screener._recommendation_for(overall),
            "factor_scores": {name: fr.score for name, fr in factors.items()},
            "metrics": raw_by_ticker[ticker].fundamentals,
        }
    return out


def historical_screener_score(ticker: str, as_of: date, include_news: bool = True,
                              include_analyst: bool = True) -> dict | None:
    """The Screener's overall score for `ticker` **as it would have scored on
    `as_of`**, using only then-knowable data and the live scoring curves.
    Returns None if EDGAR has nothing for the ticker; the score itself can still
    be None if too little was filed by that date.

    `include_news` gates the GDELT news-sentiment factor (a BigQuery query per
    call). With it False, sentiment scores None and its weight is redistributed
    — a fast, quota-free 5-factor reconstruction.

    Single-ticker convenience wrapper over historical_raw_data +
    score_reconstructed_batch."""
    ticker = ticker.strip().upper()
    built = historical_raw_data(ticker, as_of, include_analyst=include_analyst)
    if built is None:
        return None
    raw, company_name = built
    scored = score_reconstructed_batch({ticker: raw}, as_of,
                                       company_names={ticker: company_name},
                                       include_news=include_news)
    return scored[ticker]
