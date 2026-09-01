"""
The autonomous trading bot — a separate process that shares this repo's
`engine/` layer but never runs inside the Streamlit app.

Deliberate boundary: `engine/paper_trading.py` documents that it never places
an order on its own, and that stays true. Everything that trades without a
human clicking lives here instead, behind one auditable door, so "what can
place an order?" has a one-package answer.

Layout mirrors how the run actually flows:

    strategies/  data -> a target book. Pure functions, no I/O, no Alpaca.
    risk.py      the rails every order must pass (kill switches, caps, dedup).
    executor.py  target book vs. what Alpaca actually holds -> orders.
    accounts.py  maps a strategy to its own paper account's clients.
    journal.py   records every decision, acted on or not.

The strategy/executor split is the important one: a strategy that returns a
wrong target is a unit-test failure, not a wrong order.
"""
