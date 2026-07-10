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

import logging
import os
import sys

sys.path.insert(0, ".")  # runnable from repo root

from engine import config  # noqa: F401,E402  (loads .env if present)
from db.models import Base  # noqa: E402
from db.session import configure, get_engine  # noqa: E402
from engine import creator_signals, mailer  # noqa: E402
from engine.data_sources import supadata_client, youtube_data_api  # noqa: E402


def _providers() -> str:
    """Which optional providers are actually live. Printed up front so a missing
    secret can't masquerade as 'YouTube blocked us'."""
    proxy = bool(os.environ.get("YT_PROXY_URL")
                 or (os.environ.get("WEBSHARE_PROXY_USERNAME") and os.environ.get("WEBSHARE_PROXY_PASSWORD")))
    on = lambda flag: "on" if flag else "OFF"  # noqa: E731
    return (f"providers: youtube_data_api={on(youtube_data_api.is_configured())} | "
            f"supadata={on(supadata_client.is_configured())} (mode={supadata_client.mode()}) | "
            f"transcript_proxy={on(proxy)} | digest_email={on(mailer.is_configured())}")


def main() -> None:
    # INFO so engine warnings (e.g. "Supadata … failed — falling back") reach the log.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    configure()  # DATABASE_URL from env
    Base.metadata.create_all(get_engine())  # ensure the creator tables exist (idempotent)
    print(_providers(), flush=True)
    print("scanning creators for new videos...", flush=True)
    creator_signals.scan_creators()


if __name__ == "__main__":
    main()
