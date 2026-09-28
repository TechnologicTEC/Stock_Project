"""
Creator conviction: buy what a followed creator keeps making a bullish case for.

The Creator Signals page already says the honest thing about this data —
repetition is attention, not conviction. This strategy is the test of whether
that attention is worth anything, run with its own $10k so the answer is a
curve rather than an opinion.

Two ways to clear the bar, both inside a 30-day window:

  >=3 bullish mentions                        a case made repeatedly
  >=2 bullish, 0 bearish, and >=4 mentions    sustained coverage, no dissent

## Eight equal slots, and never a partial buy

Every position is bought at 1/8 of the account (`target_slots` in bot_config)
and is never resized afterwards — no top-ups when the creator keeps talking
about it, no trims when it runs. Until 28 Sep 2026 the book was four slots, with
positions topped up toward 30% as bullish mentions grew; that overcommitted the
account, since four quarters is already all of it, and the fourth buy went
$313 overdrawn. Tane's call on 28 Sep, with six names qualifying: eight slots,
one size, and the clock below doing the job the top-ups were doing badly.

A buy only happens when the account has the cash for ALL of it
(`executor.fund`). Nothing is bought in part and nothing is borrowed.

## The backlog is a stack

More names can qualify than there are slots or cash for. They wait, and when
cash frees up the NEWEST case goes first: the name with the most recent mention.
If several share that date, the one whose first mention in the window is most
recent wins — the fresher case over the one the creator has been making for a
month. Bullish count, then ticker, break any tie left after that.

Only bullish and neutral mentions count toward "newest" and "first". A bearish
mention is the creator arguing against the stock, and must never push it up the
queue.

A name stays in the backlog for as long as it clears the bar. There is no
longer a rule that a name must be mentioned again after each run to be bought:
that existed to stop the first run acting on a months-old backlog, and it would
now throw away a queue that is waiting only for cash.

## The clock: how long a position is held

A position starts with 30 days to live the day it is bought. Every later
mention adds to it, capped at 30:

  bullish   +10 days
  neutral    +5 days
  bearish    nothing — see the reversal rule below

Held with 5 days left and mentioned bullishly, it has 15; mentioned bullishly
again, 25; once more, 30 and no further. At zero it is sold. The clock is
derived every run from the journal's buy and the mentions since, rather than
stored, so there is nothing to keep in step with what actually happened.

A mention that lands after the clock had already run out does not revive it.
A name with no buy on record (it shouldn't happen — every buy is journalled) is
dated from its newest counting mention instead, so it can't be held forever.

Separately, and regardless of the clock, a held name is sold when the creator
turns: more bearish than bullish mentions in the window.

## Why absence means "sell" here, when it means "hold" everywhere else

`score_threshold` holds a name that has dropped off the leaderboard, because a
missing row is missing data. This strategy does the opposite: the clock runs
out when the creator stops talking about a stock, because that IS the signal
decaying.

That is only safe because of `MAX_FEED_SILENCE_DAYS`. If the scan job broke,
every clock would run down together and the book would liquidate on a broken
cron. So the run refuses entirely when the newest mention anywhere is too old.
Freshness is keyed on the newest **mention**, not the newest video: videos
arriving with extraction broken would leave mentions frozen while the feed
looked healthy — the failure this guards against, wearing a disguise.

## Liquidity

Every candidate is screened by `engine/bot/liquidity` before it can be bought;
that module explains why. In practice it rejects nothing the conviction bar
selects — the creator's sub-dollar micro-caps get mentioned once, not three
times. It is insurance against a creator set that changes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_
from datetime import datetime, timedelta

from engine.bot import executor, liquidity
from engine.bot.executor import Target
from engine.bot.strategies import screener_common as common

# The signal window. Long enough for "keeps coming back to it" to mean
# something at ~3 videos a week, short enough that a case made in June is not
# still being traded in September.
WINDOW_DAYS = 30

ENTRY_BULLISH = 3           # arm A: the case made repeatedly
SUSTAINED_BULLISH = 2       # arm B: fewer explicit calls...
SUSTAINED_MENTIONS = 4      # ...but sustained coverage, and no dissent at all

# The clock. Tane's numbers, 28 Sep 2026.
MAX_DAYS_LEFT = 30
DAYS_ADDED = {"bullish": 10, "neutral": 5}      # bearish and unknown add nothing
COUNTED_STANCES = frozenset(DAYS_ADDED)         # what counts as "a mention" for the queue

# The largest gap between video days across the scanned history is 9, and the
# 90th percentile is 6. 21 days is over twice the worst observed silence, so it
# separates "the creator took a break" from "the job is broken" without being
# so wide that every clock has run out before it fires.
MAX_FEED_SILENCE_DAYS = 21

DEFAULT_SLOTS = 8


def prepare(config: dict, today: date_) -> dict:
    """Read the mention window, the holding clocks, and price the candidates.

    All the I/O for this strategy: the creator mentions, when each current
    holding was bought and everything said about it since, and price frames for
    the candidates only — a handful of names, not the whole mention universe.
    """
    from engine import creator_signals
    from engine.bot.strategies import StrategyDataError

    board = creator_signals.mention_leaderboard(days=WINDOW_DAYS, min_mentions=1)

    if not board:
        raise StrategyDataError(
            f"No creator mentions at all in the last {WINDOW_DAYS} days. Either the "
            "scan job has stopped or nothing has been extracted; refusing to run, "
            "because an empty window would read as 'sell every position'."
        )

    last_seen = max((e["last_seen"] for e in board if e.get("last_seen")), default=None)
    if last_seen is None:
        raise StrategyDataError(
            "Creator mentions carry no usable dates, so the feed's freshness "
            "cannot be established."
        )
    silence = (today - last_seen.date()).days
    if silence > MAX_FEED_SILENCE_DAYS:
        raise StrategyDataError(
            f"Newest creator mention is {silence} days old, over the "
            f"{MAX_FEED_SILENCE_DAYS}-day limit. A stalled scan runs every holding's "
            "clock down at once and would liquidate the book on a broken cron "
            "rather than on a signal."
        )

    candidates = [e for e in board if qualifies(e)[0]]
    opened, history = load_clock_inputs()
    frames = liquidity.fetch_frames([e["ticker"] for e in candidates], today)

    return {
        "board": board,
        "candidates": candidates,
        "opened": opened,
        "history": history,
        "frames": frames,
        "feed_silence_days": silence,
        "screen_notional": _notional_from_config(config),
    }


def load_clock_inputs() -> tuple[dict, dict]:
    """(opened, history): when each journal-held name was bought, and every
    mention of it since. The bot page reads the same pair, so the clock it shows
    is the clock the strategy acts on."""
    from engine import creator_signals
    from engine.bot import journal
    from engine.bot import positions as bot_positions

    opened = bot_positions.opened_at(journal.fills("creator_conviction"))
    if not opened:
        return {}, {}
    history = creator_signals.mention_history(opened, since=min(opened.values()))
    return opened, history


def _notional_from_config(config: dict) -> float:
    """Indicative position size, for the liquidity screen only.

    Deliberately taken from the configured starting equity rather than the live
    account: `prepare()` runs before the broker is read. That approximation is
    fine here and nowhere else — it feeds only the participation figure, which
    is slack by two orders of magnitude at any plausible size of this account
    (see liquidity.MAX_PARTICIPATION). The gates that actually bind, the price
    and dollar-volume floors, do not depend on notional at all.
    """
    from engine.bot import risk

    return risk.position_notional(
        float(config.get("starting_equity") or 10_000.0),
        int(config.get("target_slots") or DEFAULT_SLOTS),
        float(config.get("max_position_pct") or 1.0),
    )


# --------------------------------------------------------------------------
# The clock
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Clock:
    """How long a holding has left. `bullish`/`neutral` count the mentions since
    the buy that added time, for the reason shown on the page."""
    opened: date_ | None
    until: date_
    bullish: int = 0
    neutral: int = 0

    def days_left(self, today: date_) -> int:
        return (self.until - today).days


def _as_datetime(value) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    return datetime(value.year, value.month, value.day)


def clock(opened, mentions) -> Clock:
    """The clock for a position bought at `opened`, given `mentions` as
    (when, stance) pairs. Pure.

    30 days from the buy; each later mention adds `DAYS_ADDED[stance]`, capped at
    `MAX_DAYS_LEFT` from the day of the mention. Anything at or before the buy is
    part of the case that bought it, not a reason to hold longer. A mention after
    the clock ran out cannot revive it; one on the very day it runs out still
    counts, since that day's run has not necessarily sold it yet.
    """
    start = _as_datetime(opened)
    until = start.date() + timedelta(days=MAX_DAYS_LEFT)
    bullish = neutral = 0
    for when, stance in sorted((_as_datetime(w), s) for w, s in mentions or ()):
        if when <= start:
            continue
        day = when.date()
        if day > until:
            break
        added = DAYS_ADDED.get(stance, 0)
        if not added:
            continue
        remaining = (until - day).days
        until = day + timedelta(days=min(MAX_DAYS_LEFT, remaining + added))
        bullish += stance == "bullish"
        neutral += stance == "neutral"
    return Clock(opened=start.date(), until=until, bullish=bullish, neutral=neutral)


def holding_clock(ticker: str, opened: dict, history: dict, entry: dict | None) -> Clock | None:
    """The clock for one held name, or None when there is nothing to date it by.

    A name the journal never recorded buying is dated from its newest counting
    mention, as though bought then — so it runs down like everything else rather
    than being held forever on missing data.
    """
    ticker = ticker.upper()
    if opened.get(ticker) is not None:
        return clock(opened[ticker], history.get(ticker) or ())
    days = _counted_days(entry)
    if not days:
        return None
    return Clock(opened=None, until=max(days) + timedelta(days=MAX_DAYS_LEFT))


def _hold_reason(c: Clock, today: date_) -> str:
    added = []
    if c.bullish:
        added.append(f"{c.bullish} bullish")
    if c.neutral:
        added.append(f"{c.neutral} neutral")
    since = (f" Extended by {' and '.join(added)} mention(s) since the buy."
             if added else " Nothing said about it since the buy.")
    bought = f"bought {c.opened:%d %b}" if c.opened else "no buy on record"
    return (f"{c.days_left(today)} day(s) left ({bought}); sold on {c.until:%d %b} "
            f"unless mentioned again.{since}")


# --------------------------------------------------------------------------
# Entry: the bar, and the queue
# --------------------------------------------------------------------------

def qualifies(entry: dict) -> tuple[bool, str]:
    """Does this leaderboard entry clear the conviction bar? -> (ok, why)."""
    stances = entry.get("stances") or {}
    bullish = int(stances.get("bullish") or 0)
    bearish = int(stances.get("bearish") or 0)
    mentions = int(entry.get("mentions") or 0)

    if bullish >= ENTRY_BULLISH:
        return True, f"{bullish} bullish mentions in {WINDOW_DAYS} days"
    if bullish >= SUSTAINED_BULLISH and bearish == 0 and mentions >= SUSTAINED_MENTIONS:
        return True, (f"{mentions} mentions in {WINDOW_DAYS} days, {bullish} bullish "
                      "and none bearish")
    return False, ""


def _counted_days(entry: dict | None) -> list[date_]:
    """Dates of the bullish and neutral mentions in the window.

    Falls back to `last_seen` for an entry carrying no per-video detail, which
    is all a date can honestly be read from.
    """
    if not entry:
        return []
    videos = entry.get("videos")
    if videos is None:
        seen = entry.get("last_seen")
        return [seen.date() if hasattr(seen, "date") else seen] if seen else []
    days = []
    for v in videos:
        when = v.get("published_at")
        if when is not None and v.get("stance") in COUNTED_STANCES:
            days.append(when.date() if hasattr(when, "date") else when)
    return days


def stack_key(entry: dict):
    """Newest case first: latest counting mention, then the most recent FIRST
    counting mention, then more bullish, then ticker."""
    days = _counted_days(entry)
    latest = max(days).toordinal() if days else 0
    first = min(days).toordinal() if days else 0
    bullish = int((entry.get("stances") or {}).get("bullish") or 0)
    return (-latest, -first, -bullish, (entry.get("ticker") or "").upper())


def eligible_entrants(ctx) -> list[dict]:
    """Every name clearing the bar that isn't held, in the order it would be bought."""
    candidates = (ctx.extras or {}).get("candidates") or []
    held = ctx.held_tickers()
    waiting = [e for e in candidates if (e.get("ticker") or "").upper() not in held]
    return sorted(waiting, key=stack_key)


# --------------------------------------------------------------------------
# The book
# --------------------------------------------------------------------------

def _slots(ctx) -> int:
    return max(1, int(ctx.config.get("target_slots") or DEFAULT_SLOTS))


def _keep(ctx, notional: float) -> list[Target]:
    """Held names whose clock is still running and whose creator hasn't turned.
    Anything not restated here is closed by the planner."""
    extras = ctx.extras or {}
    by_ticker = {(e.get("ticker") or "").upper(): e for e in extras.get("board") or []}
    opened = extras.get("opened") or {}
    history = extras.get("history") or {}

    kept = []
    for ticker in sorted(ctx.held_tickers()):
        entry = by_ticker.get(ticker)
        stances = (entry or {}).get("stances") or {}
        if entry is not None and int(stances.get("bearish") or 0) > int(stances.get("bullish") or 0):
            continue                                  # the creator turned
        c = holding_clock(ticker, opened, history, entry)
        if c is None or c.days_left(ctx.today) <= 0:
            continue                                  # time's up
        kept.append(Target(ticker=ticker, notional=notional, sizing=executor.HOLD,
                           reason=_hold_reason(c, ctx.today)))
    return kept


def _queue(ctx, free: int, notional: float) -> tuple[list, list]:
    """Walk the backlog in stack order until `free` slots are taken.

    Returns (chosen, declined): (entry, assessment) pairs given a slot, and the
    liquidity assessments of names passed over on the way. A name further down
    than the last free slot is neither — it was never about to be bought.
    """
    entrants = eligible_entrants(ctx)
    if free <= 0 or not entrants:
        return [], []
    tradable, excluded = liquidity.screen(
        [e["ticker"] for e in entrants], (ctx.extras or {}).get("frames") or {},
        notional, held=ctx.held_tickers(),
    )
    passed = {a.ticker.upper(): a for a in tradable}
    failed = {a.ticker.upper(): a for a in excluded}

    chosen, declined = [], []
    for entry in entrants:
        if len(chosen) >= free:
            break
        ticker = (entry.get("ticker") or "").upper()
        if ticker in passed:
            chosen.append((entry, passed[ticker]))
        elif ticker in failed:
            declined.append(failed[ticker])
    return chosen, declined


def build(ctx) -> list[Target]:
    """The target book: holdings with time left, then the backlog, newest first."""
    from engine.bot.strategies import StrategyDataError

    if not (ctx.extras or {}).get("board"):
        raise StrategyDataError(
            "No creator mention window on the context — prepare() did not run."
        )

    notional = common.notional_for(ctx)
    slots = _slots(ctx)
    targets = _keep(ctx, notional)

    chosen, _declined = _queue(ctx, slots - len(targets), notional)
    for entry, assessment in chosen:
        _ok, why = qualifies(entry)
        days = _counted_days(entry)
        newest = f" Newest mention {max(days):%d %b}." if days else ""
        targets.append(Target(
            ticker=(entry.get("ticker") or "").upper(), notional=notional,
            sizing=executor.HOLD,
            reason=(f"Creator conviction: {why}.{newest} Bought at 1/{slots} of the "
                    f"account with {MAX_DAYS_LEFT} days on the clock. {assessment.reason}"),
        ))
    return targets


def liquidity_notes(ctx) -> list[dict]:
    """Names that would have been bought but failed the liquidity screen.

    Returned for the runner to journal. Usually empty — that is the expected
    result, not a sign it isn't running.

    Scoped to names the queue actually reached. With the backlog persisting
    between runs, reporting every illiquid qualifier would repeat the same
    "declined" row on every run while all slots were full and nothing was
    about to be bought at all.
    """
    from engine.bot import journal, risk

    notional = common.notional_for(ctx)
    free = _slots(ctx) - len(_keep(ctx, notional))
    _chosen, declined = _queue(ctx, free, notional)
    # The routing keys travel with the note so the runner doesn't have to know
    # which strategy sent it — see scripts/run_bot.py.
    return [{**a.as_note(), "action": journal.SKIP, "status": journal.BLOCKED,
             "blocked_by": risk.LIQUIDITY} for a in declined]
