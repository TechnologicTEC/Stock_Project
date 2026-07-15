"""
GDELT news tone via BigQuery — the free, point-in-time *news* signal for
screener validation (step 6).

The GDELT Global Knowledge Graph tags every monitored article with a **tone**
score (roughly −10 negative … +10 positive), so we get historical news
sentiment without fetching or scoring article text — which is what makes a
years-long backfill feasible. We query the *partitioned* GKG table filtered by
`_PARTITIONTIME`, so partition pruning keeps a scan to well under a GB per month
of history (a 2-day window measured ~0.13 GB in testing).

Two hard quota guards, because GKG is tens of TB and a careless query could eat
a free-tier month in one shot: every query is (1) **dry-run first** and skipped
if the estimate exceeds MAX_SCAN_GB, and (2) run with `maximum_bytes_billed` as
an absolute backstop. Any failure (auth, quota cap, no coverage) degrades to
"no data" — the sentiment factor just scores None and its weight redistributes.

Honest caveats: this is GDELT's own tone, not FinBERT (we can't get article
text at scale historically), and company matching is a fuzzy substring against
GDELT's organization field, so it's noisy — thin or empty for smaller names.

Requires `google-cloud-bigquery` (in requirements.txt) and application-default
credentials (`gcloud auth application-default login`) + GOOGLE_CLOUD_PROJECT.
The bigquery import is deferred into the client so this module still imports
(and tests still run) in environments without it.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from functools import lru_cache

from engine import cache

logger = logging.getLogger(__name__)

_TABLE = "gdelt-bq.gdeltv2.gkg_partitioned"
# Legal/entity suffixes stripped before the org LIKE. GDELT's organization field
# stores "apple", not "apple inc." — so matching the profile name verbatim
# ("Apple Inc.") found nothing. Dropping these makes the match actually hit.
_ORG_SUFFIXES = {"inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited",
                 "plc", "lp", "llc", "holdings", "holding", "group", "the", "sa", "nv", "ag",
                 "class", "common", "stock", "ordinary", "shares"}


def _org_query_term(company_name: str) -> str:
    """The core company name for GDELT's org LIKE — lowercased, punctuation and
    legal suffixes removed ('Apple Inc.' -> 'apple')."""
    words = re.sub(r"[^a-z0-9 ]", " ", (company_name or "").lower()).split()
    kept = [w for w in words if w not in _ORG_SUFFIXES]
    return " ".join(kept).strip() or (company_name or "").strip().lower()
GDELT_TONE_TTL_SECONDS = 7 * 24 * 60 * 60   # historical tone doesn't change; cache a week
SENTIMENT_WINDOW_DAYS = 30                  # news lookback feeding a single as-of score
MAX_SCAN_GB = 60.0                          # dry-run estimate above this -> skip (don't spend quota)
MAX_BYTES_BILLED = 80_000_000_000           # absolute backstop on any single query


@lru_cache(maxsize=1)
def _bq_client():
    from google.cloud import bigquery  # deferred: keeps the module importable without the dep
    return bigquery.Client()


def is_configured() -> bool:
    """True when BigQuery is actually usable *here*.

    GDELT tone is the only historical news source and it runs on BigQuery, which
    needs Google Cloud credentials + a project. Those exist on a dev machine after
    `gcloud auth application-default login`, but NOT on a deployed Space unless a
    service-account key is added — where the news factor therefore stays blank.
    Callers use this to say so rather than silently returning no data."""
    try:
        _bq_client()
        return True
    except Exception as exc:
        logger.info("GDELT/BigQuery unavailable here (%s: %s)", type(exc).__name__, exc)
        return False


def _run_daily_tone_query(company_name: str, start: date, end: date) -> list[dict]:
    """One BigQuery call: average article tone per day for articles whose GDELT
    organization field mentions `company_name`, over [start, end). Dry-run
    guarded + byte-capped. Returns [] rather than raising on cost/coverage."""
    from google.cloud import bigquery

    client = _bq_client()
    org_like = f"%{_org_query_term(company_name)}%"
    sql = f"""
        SELECT DATE(_PARTITIONTIME) AS day,
               AVG(SAFE_CAST(SPLIT(V2Tone, ',')[OFFSET(0)] AS FLOAT64)) AS avg_tone,
               COUNT(*) AS n
        FROM `{_TABLE}`
        WHERE _PARTITIONTIME >= @start AND _PARTITIONTIME < @end
          AND LOWER(V2Organizations) LIKE @org
          AND V2Tone != ''
        GROUP BY day
        ORDER BY day
    """
    params = [
        bigquery.ScalarQueryParameter("start", "TIMESTAMP", datetime(start.year, start.month, start.day)),
        bigquery.ScalarQueryParameter("end", "TIMESTAMP", datetime(end.year, end.month, end.day)),
        bigquery.ScalarQueryParameter("org", "STRING", org_like),
    ]

    dry = client.query(sql, job_config=bigquery.QueryJobConfig(
        query_parameters=params, dry_run=True, use_query_cache=False))
    if (dry.total_bytes_processed or 0) / 1e9 > MAX_SCAN_GB:
        return []  # too expensive for this range — skip rather than burn quota

    job = client.query(sql, job_config=bigquery.QueryJobConfig(
        query_parameters=params, maximum_bytes_billed=MAX_BYTES_BILLED))
    return [
        {"day": r["day"].isoformat(),
         "avg_tone": float(r["avg_tone"]) if r["avg_tone"] is not None else None,
         "n": int(r["n"])}
        for r in job.result()
    ]


def get_daily_tone(company_name: str, start: date, end: date) -> list[dict]:
    """Cached per-day average tone for `company_name` over [start, end). Empty
    list on any failure (no bigquery, no credentials, quota guard, no coverage)."""
    if not company_name or not company_name.strip():
        return []
    # _v2: the org-name normalization changed what the query matches, so bust any
    # empties cached under the old (suffix-mismatched) query.
    key = f"gdelt_tone_v2:{company_name.strip().lower()}:{start.isoformat()}:{end.isoformat()}"
    try:
        return cache.get_or_fetch(key, GDELT_TONE_TTL_SECONDS,
                                  lambda: _run_daily_tone_query(company_name, start, end))
    except Exception:
        return []


def tone_to_sentiment(avg_tone: float | None) -> float | None:
    """Map GDELT tone (~−10..+10, centred on 0) onto the Screener's 0–100 scale:
    0 = strongly negative, 50 = neutral, 100 = strongly positive. ±5 tone spans
    the full range, which covers essentially all real company-level averages."""
    if avg_tone is None:
        return None
    return max(0.0, min(100.0, 50.0 + avg_tone * 10.0))


def daily_tone_for_year(company_name: str, year: int) -> list[dict]:
    """A whole calendar year of daily tone in ONE cached query.

    The walk-forward asks for a 30-day window per as-of date. Querying per date
    meant ~24 BigQuery jobs *per ticker* — a pooled run was ~288 jobs, ~24
    minutes, and half the monthly free query quota. One job per company-year is
    cached and then reused by every as-of date, every ticker pass, and every
    re-run. Same bytes, ~12x fewer round trips."""
    if not company_name or not company_name.strip():
        return []
    key = f"gdelt_tone_year:{company_name.strip().lower()}:{year}"
    try:
        return cache.get_or_fetch(
            key, GDELT_TONE_TTL_SECONDS,
            lambda: _run_daily_tone_query(company_name, date(year, 1, 1), date(year + 1, 1, 1)),
        )
    except Exception as exc:
        # Never swallow this silently: "no BigQuery credentials" and "no news
        # coverage" both produced an empty factor, which looked identical.
        logger.warning("GDELT tone for %r/%s failed (%s: %s) — news factor will be blank",
                       company_name, year, type(exc).__name__, exc)
        return []


def sentiment_as_of(company_name: str, as_of: date, window_days: int = SENTIMENT_WINDOW_DAYS) -> float | None:
    """Point-in-time news sentiment (0–100) for `company_name` as of `as_of`:
    the article-count-weighted average tone over the preceding `window_days`,
    mapped to the Screener's scale. None if there's no coverage in the window.

    Reads from the cached per-year tone series rather than issuing a query per
    as-of date (see daily_tone_for_year)."""
    if not company_name or not company_name.strip():
        return None
    window_start = as_of - timedelta(days=window_days)

    rows: list[dict] = []
    for year in range(window_start.year, as_of.year + 1):   # a window can straddle a year boundary
        rows.extend(daily_tone_for_year(company_name, year))

    weighted, total = 0.0, 0
    for row in rows:
        try:
            day = date.fromisoformat(row["day"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (window_start <= day < as_of):
            continue
        tone, n = row.get("avg_tone"), row.get("n") or 0
        if tone is not None and n:
            weighted += tone * n
            total += n
    if not total:
        return None
    return round(tone_to_sentiment(weighted / total), 1)
