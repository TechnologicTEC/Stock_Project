"""
Scheduled Creator Signals scan (docs/creator-signals-plan.md; run by GitHub
Actions — see .github/workflows/creator-signals.yml). For each active creator it
polls the YouTube feed, transcribes new videos, extracts the stocks discussed,
screens them, and stores the results.

Connects via DATABASE_URL as an admin / BYPASSRLS Postgres role — the creator
tables are global/shared, not per-user. Idempotent (videos are deduped).

Run:
    DATABASE_URL=<postgres URL> PRICE_HISTORY_PREFER_ALPACA=1 GEMINI_API_KEY=... \
    FINNHUB_API_KEY=... ALPACA_API_KEY=... ALPACA_SECRET_KEY=... \
    EDGAR_USER_AGENT='Your Name you@example.com' python scripts/scan_creators.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")  # runnable from repo root

from engine import config  # noqa: F401,E402  (loads .env if present)
from db.models import Base  # noqa: E402
from db.session import configure, get_engine  # noqa: E402
from engine import creator_signals  # noqa: E402


def main() -> None:
    configure()  # DATABASE_URL from env
    Base.metadata.create_all(get_engine())  # ensure the creator tables exist (idempotent)
    print("scanning creators for new videos...", flush=True)
    creator_signals.scan_creators()


if __name__ == "__main__":
    main()
