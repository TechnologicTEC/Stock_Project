"""
Seed / update the bot_config rows — the bot's control surface.

Idempotent: re-running updates the existing rows rather than duplicating them,
so this is also how you change a slot count or a cap. It never flips `killed`,
because a stop someone engaged deliberately must not be undone by a deploy.

    python scripts/seed_bot_config.py            # apply
    python scripts/seed_bot_config.py --show     # print current rows only

Slot counts and caps come from the bot blueprint, where each was simulated
against this project's own history rather than picked.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from db.session import init_db          # noqa: E402
from engine.bot import journal          # noqa: E402

# Only strategies that actually exist in engine/bot/strategies are listed. The
# rest are added here as they're built, so a row never points at a missing module.
#
# THE HANDOVER. spy_harness and golden_cross share one Alpaca account, which was
# always the plan: the harness is the plumbing test that runs until the real
# signal is ready, and both hold SPY at a single slot, so when the cross is on
# the handover costs no trade at all. They must never run at once, though, or
# they fight over the same book — so seeding golden_cross DISABLES the harness.
# That is a state change, not just a new row; `--show` first if you want to see
# what it will replace. Its journal rows and equity snapshots are untouched, so
# the harness's grade stays readable afterwards.
SEED: list[dict] = [
    {
        "strategy": "spy_harness",
        "key_env_prefix": "ALPACA_GOLDEN_CROSS",
        "target_slots": 1,
        "max_position_pct": 1.0,     # a single-ETF strategy isn't diversifying; 20% would make no sense
        "max_orders_per_run": 5,     # it should only ever need one
        "starting_equity": 10_000.0,
        "enabled": False,            # retired: golden_cross now owns this account
    },
    {
        "strategy": "golden_cross",
        "key_env_prefix": "ALPACA_GOLDEN_CROSS",
        "target_slots": 1,           # one name (SPY), so the live-vs-backtest check is exact
        "max_position_pct": 1.0,     # long-or-cash: when the cross is on, it's on fully
        "max_orders_per_run": 5,
        "starting_equity": 10_000.0,
        "enabled": True,
    },
]


def apply() -> None:
    for row in SEED:
        saved = journal.upsert_config(**row)
        print(f"  {saved['strategy']:<22} slots={saved['target_slots']:<3} "
              f"cap={saved['max_position_pct']:.0%} keys={saved['key_env_prefix']}_* "
              f"enabled={saved['enabled']} killed={saved['killed']}")


def show() -> None:
    rows = journal.list_configs()
    if not rows:
        print("  (no bot_config rows yet — run without --show to seed them)")
        return
    for r in rows:
        print(f"  {r['strategy']:<22} slots={r['target_slots']:<3} "
              f"cap={r['max_position_pct']:.0%} keys={r['key_env_prefix']}_* "
              f"enabled={r['enabled']} killed={r['killed']} updated={r['updated_at']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed or inspect bot_config.")
    parser.add_argument("--show", action="store_true", help="Print current rows without writing.")
    args = parser.parse_args()

    init_db()
    if args.show:
        print("bot_config:")
        show()
    else:
        print("Seeding bot_config:")
        apply()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
