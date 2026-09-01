"""
The per-strategy panel on each bot tab — the reason to open one tab over another.

Everything above these on a tab is the same five numbers in the same shape, on
purpose, so the strategies can be compared. This is the opposite: each function
answers the question only its own strategy can be asked, in that strategy's own
vocabulary. Golden cross gets a chart of the two averages it decides on;
composite gets the ranking and who is near the exit; top decile gets the spread
against the decile it tracks but never trades.

Two rules hold across all of them.

**Read what the bot reads.** Every panel loads through the same accessor the
strategy uses — `screener_common.load_leaderboard`, `decile_spread.current`,
`golden_cross.moving_averages` — rather than recomputing something similar. A
panel that quietly disagrees with the decision it is illustrating is worse than
no panel, because it looks authoritative.

**Never break the tab.** These are illustrations. Each one degrades to a short
note if its data is missing, and none of them can raise: the numbers above them
come from the database and must survive a cold cache, a stale ranking, or a
scan job that has stopped.
"""
from __future__ import annotations

import html
from datetime import date, datetime, timedelta

import plotly.graph_objects as go
import streamlit as st

from app import _cache, _theme


# Days of prices before the spread is worth interpreting. Below this the two
# baskets have barely moved apart and the number is noise — on the day of a
# rebalance it is exactly 0.00% by construction.
MIN_SPREAD_DAYS = 5


def _esc(text) -> str:
    return html.escape(str(text if text is not None else ""))


def _money(v, dp: int = 2) -> str:
    return "—" if v is None else f"${v:,.{dp}f}"


def _signed_pct(v, dp: int = 2) -> str:
    return "—" if v is None else f"{v * 100:+.{dp}f}%"


def _video_link(video: dict) -> str:
    """One dated link to the video a mention came from.

    The date is the label because the titles are long and near-identical
    ("...STOCKS TO BUY NOW!"), so a column of them tells you nothing while a
    column of dates tells you the shape of the coverage. The title rides along
    as the tooltip.
    """
    when = video.get("published_at")
    label = when.strftime("%d %b") if hasattr(when, "strftime") else "video"
    title = _esc(video.get("title") or "")
    return (f'<a href="{_esc(video.get("url"))}" target="_blank" rel="noopener noreferrer" '
            f'title="{title}">{label}</a>')


def _note(title: str, message: str, tag: str | None = None) -> None:
    _theme.panel(title, f'<p class="cp-note">{message}</p>', tag=tag)


def render(name: str, cfg: dict, view: dict) -> None:
    """Draw the panel for `name`, if it has one. Never raises."""
    fn = _PANELS.get(name)
    if fn is None:
        return
    try:
        fn(cfg, view)
    except Exception as exc:                 # noqa: BLE001 — see module docstring
        _note("Strategy detail",
              "This panel could not be drawn. Everything above it is unaffected."
              f'</p><p class="cp-foot">{_esc(exc)}',
              tag="unavailable")


# --------------------------------------------------------------------------
# Golden cross — the two averages, and where they crossed
# --------------------------------------------------------------------------

def _golden_cross(cfg: dict, view: dict) -> None:
    from engine.bot.strategies import golden_cross

    ticker = golden_cross.UNIVERSE[0]
    frame = _cache.bot_sma_frame(ticker, golden_cross.LOOKBACK_DAYS)
    if len(frame) < golden_cross.SLOW:
        _note("Moving averages",
              f"Needs {golden_cross.SLOW} daily closes for the "
              f"{golden_cross.SLOW}-day average; {len(frame)} cached. The strategy "
              "refuses to trade on a short series rather than reading the gap as a "
              "sell, so this panel is empty for the same reason.",
              tag=f"{len(frame)} bars")
        return

    dates = [r["date"] for r in frame]
    with _theme.section(f"{ticker} · {golden_cross.FAST}/{golden_cross.SLOW} moving averages",
                        tag="the signal itself"):
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=dates, y=[r["close"] for r in frame], name=ticker,
                                 mode="lines", line=dict(width=1.4),
                                 hovertemplate="%{x|%d %b} · $%{y:,.2f}<extra>" + ticker + "</extra>"))
        fig.add_trace(go.Scatter(x=dates, y=[r["fast"] for r in frame],
                                 name=f"{golden_cross.FAST}-day", mode="lines",
                                 line=dict(width=1.6),
                                 hovertemplate="%{x|%d %b} · $%{y:,.2f}<extra>50d</extra>"))
        fig.add_trace(go.Scatter(x=dates, y=[r["slow"] for r in frame],
                                 name=f"{golden_cross.SLOW}-day", mode="lines",
                                 line=dict(width=1.6, dash="dot"),
                                 hovertemplate="%{x|%d %b} · $%{y:,.2f}<extra>200d</extra>"))

        # Mark the crossings. Only where BOTH averages exist, so the start of
        # the 200-day series isn't drawn as a golden cross that never happened.
        crosses = []
        for prev, cur in zip(frame, frame[1:]):
            if None in (prev["fast"], prev["slow"], cur["fast"], cur["slow"]):
                continue
            was, now = prev["fast"] > prev["slow"], cur["fast"] > cur["slow"]
            if was != now:
                crosses.append((cur["date"], cur["close"], now))
        if crosses:
            fig.add_trace(go.Scatter(
                x=[c[0] for c in crosses], y=[c[1] for c in crosses],
                name="Crossover", mode="markers",
                marker=dict(size=9, symbol="diamond",
                            color=[_theme.UP if c[2] else _theme.DOWN for c in crosses]),
                hovertemplate="%{x|%d %b %Y}<extra>crossover</extra>"))
        fig.update_layout(height=320, hovermode="x unified",
                          legend=dict(orientation="h", y=-0.18))
        fig.update_yaxes(title_text="Price")
        st.plotly_chart(fig, width="stretch", key="bot_gc_sma", theme=None)

        last = frame[-1]
        state = "above" if (last["fast"] or 0) > (last["slow"] or 0) else "below"
        st.caption(
            f"The {golden_cross.FAST}-day is **{state}** the {golden_cross.SLOW}-day: "
            f"{_money(last['fast'])} vs {_money(last['slow'])}. Invested while it is above, "
            f"in cash otherwise. {len(crosses)} crossover(s) in this window — green entered, "
            "red exited."
        )


# --------------------------------------------------------------------------
# Composite rebalance — the ranking, and who is near the exit
# --------------------------------------------------------------------------

def _leaderboard_rows() -> tuple[list[dict], str | None]:
    payload = _cache.bot_leaderboard()
    return list(payload.get("rows") or []), payload.get("error")


def _rank_table(rows: list[dict], held: set[str], *, highlight_from: int | None = None) -> str:
    body = []
    for r in rows:
        ticker = (r.get("ticker") or "").upper()
        own = ' <span class="cp-pill">held</span>' if ticker in held else ""
        near = ' class="dim"' if (highlight_from and (r.get("rank") or 0) >= highlight_from) else ""
        body.append(
            f'<tr{near}><td class="num dim">{r.get("rank")}</td>'
            f'<td><span class="tick">{_esc(ticker)}</span>{own}</td>'
            f'<td class="dim">{_esc((r.get("name") or "")[:34])}</td>'
            f'<td class="num">{r.get("score")}</td></tr>'
        )
    return ('<div class="cp-scroll"><table class="cp-table"><thead><tr>'
            '<th class="num">Rank</th><th>Ticker</th><th>Name</th>'
            f'<th class="num">Score</th></tr></thead><tbody>{"".join(body)}</tbody></table></div>')


def _composite(cfg: dict, view: dict) -> None:
    from engine.bot.strategies import composite_rebalance as comp

    rows, error = _leaderboard_rows()
    if error or not rows:
        _note("The ranking",
              _esc(error) if error
              else "No leaderboard cached. The weekly screen job writes it.",
              tag="unavailable")
        return

    ranked = sorted(rows, key=lambda r: r.get("rank") or 10 ** 9)
    held = {(p["ticker"] or "").upper() for p in (view.get("positions") or [])}
    slots = int(cfg.get("target_slots") or comp.TARGET_RANK)

    _theme.panel(
        f"Top {slots} by rank, and the buffer to rank {comp.EXIT_RANK}",
        _rank_table(ranked[:comp.EXIT_RANK], held, highlight_from=slots + 1)
        + f'<p class="cp-note">Names enter at rank {slots} or better and are only sold once '
          f'they fall past rank {comp.EXIT_RANK} — the dimmed rows are that buffer. Without it '
          "a name drifting a point or two of composite would round-trip every month. "
          "Rebalanced monthly, matching the 30-day horizon the ranking's skill was measured at."
          "</p>",
        tag=f"{len(ranked)} ranked",
    )


# --------------------------------------------------------------------------
# Strong Buy threshold — the queue above 75 and the hold band
# --------------------------------------------------------------------------

def _score_threshold(cfg: dict, view: dict) -> None:
    from engine.bot.strategies import score_threshold as thr

    rows, error = _leaderboard_rows()
    if error or not rows:
        _note("Scores", _esc(error) if error else "No leaderboard cached.", tag="unavailable")
        return

    held = {(p["ticker"] or "").upper() for p in (view.get("positions") or [])}
    slots = int(cfg.get("target_slots") or 20)
    scored = [r for r in rows if r.get("score") is not None]
    above = sorted((r for r in scored if float(r["score"]) >= thr.ENTRY_SCORE),
                   key=lambda r: -float(r["score"]))
    waiting = [r for r in above if (r.get("ticker") or "").upper() not in held]
    band = [r for r in scored
            if thr.EXIT_SCORE <= float(r["score"]) < thr.ENTRY_SCORE
            and (r.get("ticker") or "").upper() in held]
    free = max(0, slots - len(held))

    rowsh = "".join(
        f'<tr><td><span class="tick">{_esc((r.get("ticker") or "").upper())}</span></td>'
        f'<td class="dim">{_esc((r.get("name") or "")[:34])}</td>'
        f'<td class="num">{r.get("score")}</td></tr>' for r in waiting[:12])
    _theme.panel(
        f"Clearing {thr.ENTRY_SCORE:.0f}, waiting for a slot",
        ('<div class="cp-scroll"><table class="cp-table"><thead><tr><th>Ticker</th>'
         '<th>Name</th><th class="num">Score</th></tr></thead>'
         f"<tbody>{rowsh}</tbody></table></div>" if waiting else
         '<p class="cp-note">Nothing above the entry score that isn\'t already held.</p>')
        + f'<p class="cp-note"><b>{len(above)}</b> names clear {thr.ENTRY_SCORE:.0f} · '
          f'<b>{free}</b> of {slots} slots free. Unfilled slots stay in cash — it never reaches '
          "further down the ranking to fill one, because a 70-scoring name bought to occupy a "
          "slot is not the strategy.</p>",
        tag=f"{len(waiting)} queued",
    )

    if band:
        held_rows = "".join(
            f'<tr><td><span class="tick">{_esc((r.get("ticker") or "").upper())}</span></td>'
            f'<td class="num">{r.get("score")}</td></tr>' for r in
            sorted(band, key=lambda r: float(r["score"])))
        _theme.panel(
            f"In the hold band ({thr.EXIT_SCORE:.0f}–{thr.ENTRY_SCORE:.0f})",
            '<div class="cp-scroll"><table class="cp-table"><thead><tr><th>Ticker</th>'
            f'<th class="num">Score</th></tr></thead><tbody>{held_rows}</tbody></table></div>'
            f'<p class="cp-note">Held but no longer clearing the entry. Sold below '
            f'{thr.EXIT_SCORE:.0f}, or immediately below {thr.HARD_EXIT_SCORE:.0f} — the index '
            "median, where a name has stopped being a good company having a wobble.</p>",
            tag=f"{len(band)} decaying",
        )


# --------------------------------------------------------------------------
# Creator conviction — the mentions that triggered it, and the 30-day clock
# --------------------------------------------------------------------------

def _creator(cfg: dict, view: dict) -> None:
    from engine.bot.strategies import creator_conviction as cc

    board = _cache.bot_creator_mentions()
    if not board:
        _note("Creator mentions",
              f"No mentions in the last {cc.WINDOW_DAYS} days. The strategy refuses to run "
              "on an empty window rather than reading it as 'sell everything'.",
              tag="nothing scanned")
        return

    today = date.today()
    newest = max((e["last_seen"] for e in board if e.get("last_seen")), default=None)
    silence = (today - newest.date()).days if newest else None
    held = {(p["ticker"] or "").upper() for p in (view.get("positions") or [])}

    entries = sorted(board, key=lambda e: (-(e.get("stances") or {}).get("bullish", 0),
                                           -(e.get("mentions") or 0)))
    shown = [e for e in entries
             if cc.qualifies(e)[0] or (e.get("ticker") or "").upper() in held][:10]

    body = []
    for e in shown:
        ticker = (e.get("ticker") or "").upper()
        st_ = e.get("stances") or {}
        bull = int(st_.get("bullish") or 0)
        seen = e.get("last_seen")
        days_left = (cc.WINDOW_DAYS - (today - seen.date()).days) if seen else None
        ok, _why = cc.qualifies(e)
        flag = ('<span class="cp-pill">held</span>' if ticker in held
                else ('<span class="cp-pill">qualifies</span>' if ok else ""))
        links = " ".join(_video_link(v) for v in (e.get("videos") or [])[:4] if v.get("url"))
        body.append(
            f'<tr><td><span class="tick">{_esc(ticker)}</span> {flag}</td>'
            f'<td class="num">{bull}</td>'
            f'<td class="num dim">{e.get("mentions")}</td>'
            f'<td class="num dim">{_esc(seen.strftime("%d %b")) if seen else "—"}</td>'
            f'<td class="num">{days_left if days_left is not None else "—"}</td>'
            f'<td class="dim">{links or "—"}</td></tr>')

    _theme.panel(
        f"The {cc.WINDOW_DAYS}-day mention window",
        '<div class="cp-scroll"><table class="cp-table"><thead><tr><th>Ticker</th>'
        '<th class="num">Bullish</th><th class="num">All</th><th class="num">Last seen</th>'
        '<th class="num">Days left</th><th>Videos</th></tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table></div>'
        f'<p class="cp-note">Buys on <b>{cc.ENTRY_BULLISH} bullish mentions</b>, or '
        f'{cc.SUSTAINED_BULLISH} bullish with no bearish across {cc.SUSTAINED_MENTIONS}+ '
        "mentions — and only when a name is mentioned <i>again</i>, so the backlog standing at "
        "go-live is never bought. <b>Days left</b> is how long the newest mention has before it "
        f"ages out of the window; at zero bullish the position is sold. Size grows with the case: "
        f"the ceiling is reached at {cc.ENTRY_BULLISH + cc.MENTIONS_TO_MAX} bullish mentions.</p>"
        + (f'<p class="cp-foot">Newest mention anywhere is {silence} day(s) old; the run refuses '
           f"past {cc.MAX_FEED_SILENCE_DAYS}, so a stalled scan cannot empty the window and "
           "liquidate the book.</p>" if silence is not None else ""),
        tag=f"{len(board)} names mentioned",
    )


# --------------------------------------------------------------------------
# Top decile long — the short side it measures but never trades
# --------------------------------------------------------------------------

def _top_decile(cfg: dict, view: dict) -> None:
    spread = _cache.bot_decile_spread()
    if not spread:
        _note("Top-minus-bottom spread",
              "Recorded at each monthly rebalance, then priced forward. Nothing to measure "
              "until the first rebalance has happened.",
              tag="awaiting the first snapshot")
        return

    top, bottom, gap = spread["top_return"], spread["bottom_return"], spread["spread"]
    days = spread["days"]

    # A verdict needs time to have passed. On the day of a rebalance the start
    # and end prices are the same bar, so every number is +0.00% — reading that
    # as "the ranking failed" would be nonsense dressed as a finding.
    if days < MIN_SPREAD_DAYS:
        verdict = (f"it is far too early to read — {days} day(s) of prices cannot separate "
                   "two 50-name baskets")
    elif (gap or 0) > 0:
        verdict = "the ranking separated the two ends, which is the result this strategy exists to find"
    else:
        verdict = "the ranking did not separate the two ends over this window"
    _theme.panel(
        "Top-minus-bottom decile spread",
        '<div class="cp-scroll"><table class="cp-table"><thead><tr><th>Decile</th>'
        '<th class="num">Return</th><th class="num">Names priced</th></tr></thead><tbody>'
        f'<tr><td>Top (bought)</td><td class="num">{_signed_pct(top)}</td>'
        f'<td class="num dim">{spread["top_priced"]}</td></tr>'
        f'<tr><td>Bottom (tracked, never traded)</td><td class="num">{_signed_pct(bottom)}</td>'
        f'<td class="num dim">{spread["bottom_priced"]}</td></tr>'
        f'<tr><td><b>Spread</b></td><td class="num"><b>{_signed_pct(gap)}</b></td>'
        f'<td class="num dim">—</td></tr>'
        "</tbody></table></div>"
        f'<p class="cp-note">Since the rebalance on <b>{spread["as_of"]:%d %b %Y}</b> '
        f'({days} day{"" if days == 1 else "s"}), equal-weighted. The bottom decile is <b>measured, never '
        "traded</b> — shorting it would need whole shares this account cannot borrow, but the "
        f"comparison survives without the trade. On this reading, {verdict}. A name that "
        "cannot be priced is dropped from its own side and counted, never scored as flat.</p>"
        + (f'<p class="cp-foot">{spread["missing"]} name(s) could not be priced.</p>'
           if spread.get("missing") else ""),
        tag=f"{spread['days']}d since the snapshot",
    )


_PANELS = {
    "golden_cross": _golden_cross,
    "spy_harness": _golden_cross,
    "composite_rebalance": _composite,
    "score_threshold": _score_threshold,
    "creator_conviction": _creator,
    "top_decile_long": _top_decile,
}
