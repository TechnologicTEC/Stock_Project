"""
Trading Bot — the automated sibling of the Paper Trading page.

Five strategies, each trading its own $10k Alpaca **paper** account on a daily
GitHub Actions cron. This page never trades: it reads what the bot did and gives
you one control, the per-strategy Stop.

Comparison on top, per-strategy detail in tabs underneath. That layout follows
from the question you actually open this to answer — "which one is winning, and
why" — which is a comparison, and would become a clicking exercise if the five
strategies lived on five pages.

Two things it deliberately refuses to do:

  * **It won't rank on a few weeks of data.** The banner computes the standard
    error on an annualised Sharpe at the current sample size, which is presently
    several times any difference in the table. The leaderboard is a
    scoreboard-in-progress, and the page says so in numbers rather than in a
    disclaimer.
  * **It won't fall over without Alpaca credentials.** Every column that matters
    is read from the database, which the bot writes on each run. The deployed
    Space holds no bot key pairs (only GitHub Actions does), so the live position
    detail is additive — present locally, absent on the Space, and its absence
    costs one panel rather than the page.
"""
import html
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import plotly.graph_objects as go
import streamlit as st

from app import _bot_panels, _cache, _theme
from app._auth import gate
from db.session import init_db
from engine.bot import journal, performance, risk
from engine.bot import positions as bot_positions
from engine.bot import strategies as bot_strategies

st.set_page_config(page_title="Trading Bot — Investment Co-Pilot", page_icon="📊", layout="wide")
_theme.apply()
init_db()
gate("bot")          # restricted: not in GUEST_PAGES, so guests are stopped here

BENCHMARK_LABEL = "SPY buy & hold"
CRON_HOUR_UTC, CRON_MINUTE_UTC = 21, 45      # must match .github/workflows/trade-bot.yml
JOURNAL_LIMIT = 400                          # per strategy, for the trade count and reasons


# --------------------------------------------------------------------------
# Formatting helpers. Every one of them renders None as an em dash rather than
# as 0 — "no data yet" and "zero" are different answers and the page must not
# blur them.
# --------------------------------------------------------------------------

def _money(v, dp: int = 0) -> str:
    """The sign goes OUTSIDE the dollar sign — "-$4.02", not "$-4.02". Cosmetic
    everywhere else on the page, but the holdings table now puts a signed dollar
    P&L under every percentage, so it appears on every row."""
    if v is None:
        return "—"
    return f"{'-' if v < 0 else ''}${abs(v):,.{dp}f}"


def _tidy_pct(v: float) -> str:
    """A percentage with a decimal only where it earns one: 5% and 6.7%, not
    "5.0%" beside "6.7%"."""
    return f"{v:.1%}".replace(".0%", "%")


def _pct(v, dp: int = 1) -> str:
    return f"{v * 100:.{dp}f}%" if v is not None else "—"


def _signed_pct(v, dp: int = 1) -> str:
    return f"{v * 100:+.{dp}f}%" if v is not None else "—"


def _num(v, dp: int = 2) -> str:
    return f"{v:,.{dp}f}" if v is not None else "—"


def _cls(v) -> str:
    """up/down/dim classes for a signed number."""
    if v is None:
        return "dim"
    return "up" if v > 0 else "down" if v < 0 else ""


def _esc(text) -> str:
    return html.escape(str(text if text is not None else ""))


def _sparkline(values: list[float], *, color: str, dashed: bool = False) -> str:
    """A 70x20 inline SVG of one equity curve, for the comparison table.

    Flat or single-point series render as a straight midline rather than as
    nothing — an account that hasn't moved should look like an account that
    hasn't moved, not like a missing cell.
    """
    if not values:
        return '<span class="dim">—</span>'
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    step = 70 / max(1, len(values) - 1) if len(values) > 1 else 70
    points = " ".join(
        f"{i * step:.1f},{18 - (v - lo) / span * 16:.1f}" for i, v in enumerate(values)
    ) if len(values) > 1 else "0,10 70,10"
    dash = ' stroke-dasharray="2 2"' if dashed else ""
    return (
        f'<svg width="70" height="20" viewBox="0 0 70 20" fill="none" aria-hidden="true">'
        f'<polyline points="{points}" stroke="{color}" stroke-width="1.3"{dash} '
        f'stroke-linejoin="round"/></svg>'
    )


def _next_scheduled_run(now: datetime) -> datetime:
    """The next weekday 21:45 UTC — the cron in trade-bot.yml, computed rather
    than hardcoded as a sentence so it can't drift out of date on the page."""
    target = now.replace(hour=CRON_HOUR_UTC, minute=CRON_MINUTE_UTC, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    while target.weekday() >= 5:                      # Sat/Sun roll to Monday
        target += timedelta(days=1)
    return target


_STATUS_BADGE = {
    journal.FILLED: "sb", journal.SUBMITTED: "b",
    journal.BLOCKED: "s", journal.ERROR: "s",
    journal.SKIPPED: "h", journal.DRY_RUN: "faint",
    journal.CANCELLED: "faint",     # reached the broker, pulled before it filled
}


def _status_badge(status: str) -> str:
    return _theme.badge_html(status or "—", _STATUS_BADGE.get(status, "h"))


# --------------------------------------------------------------------------
# The stat strip — six numbers in one band, replacing six metric cards.
# --------------------------------------------------------------------------

def _stat(key: str, value: str, *, cls: str = "", se: str | None = None) -> str:
    """One cell. `value` and `se` are pre-formatted HTML, `key` is escaped."""
    se_html = f' <span class="se">{se}</span>' if se else ""
    return (f'<div class="st"><span class="k">{_esc(key)}</span>'
            f'<span class="v {cls}">{value}{se_html}</span></div>')


def _strip(*cells: str) -> None:
    st.markdown(f'<div class="cp-strip">{"".join(c for c in cells if c)}</div>',
                unsafe_allow_html=True)


# Above this many slots the pips stop being countable and become a texture —
# 50 of them wrap to five rows and say less than the bar they replaced. The
# threshold is where "how many are filled" is still answerable at a glance.
MAX_PIPS = 30


def _slots_cell(held: int, slots: int) -> str:
    """One pip per slot where they can be counted, the old bar where they can't."""
    if slots and slots <= MAX_PIPS:
        pips = "".join(f'<i class="cp-pip{" f" if i < held else ""}"></i>' for i in range(slots))
        inner = f'<div class="cp-pips">{pips}</div>'
    else:
        fill = min(100.0, (held / slots * 100.0) if slots else 0.0)
        inner = (f'<span class="cp-wbar" style="width:150px">'
                 f'<i style="width:{fill:.0f}%"></i></span>')
    return ('<div class="cp-slots"><div>'
            '<span class="k" style="font-family:var(--cp-mono);font-size:9.5px;'
            'letter-spacing:.13em;text-transform:uppercase;color:var(--cp-muted);'
            'display:block;margin-bottom:5px">Slots</span>'
            f'{inner}</div></div>')


# --------------------------------------------------------------------------
# The holdings table. Columns appear only when some row can fill them: a
# strategy with no ranking behind it (golden cross, creator conviction) would
# otherwise carry a Score column of em dashes, and a book rebuilt from the
# journal has no intraday move to report.
# --------------------------------------------------------------------------

def _positions_table(rows: list[dict]) -> str:
    has_today = any(r.get("change_today_pct") is not None for r in rows)
    has_score = any(r.get("score") is not None for r in rows)
    has_reason = any(r.get("reason") for r in rows)
    has_held = any(r.get("days_held") is not None for r in rows)

    head = ['<th>Holding</th>', '<th class="num">Value</th>', '<th class="num">P&amp;L</th>']
    if has_today:
        head.append('<th class="num">Today</th>')
    head.append('<th class="num">Weight</th>')
    head.append('<th class="num">Shares</th>')
    head.append('<th class="num">Entry &rarr; now</th>')
    if has_held:
        head.append('<th class="num">Held</th>')
    if has_score:
        head.append('<th class="num">Score</th>')
    if has_reason:
        head.append("<th>Why it's held</th>")

    body = []
    for r in rows:
        name = r.get("name")
        weight = r.get("weight")
        cells = [
            f'<td><span class="tick">{_esc(r["ticker"])}</span>'
            + (f'<span class="sub">{_esc(name)}</span>' if name else "")
            + "</td>",
            f'<td class="num big">{_money(r.get("market_value"), 2)}</td>',
            f'<td class="num {_cls(r.get("unrealized_pl"))}">'
            f'{_signed_pct(r.get("unrealized_plpc"), 2)}'
            f'<span class="sub {_cls(r.get("unrealized_pl"))}">'
            f'{_money(r.get("unrealized_pl"), 2)}</span></td>',
        ]
        if has_today:
            cells.append(f'<td class="num {_cls(r.get("change_today_pct"))}">'
                         f'{_signed_pct(r.get("change_today_pct"), 1)}</td>')
        # The weight bar is scaled to a QUARTER of the book, not to 100%: at
        # 50 slots every position is 2% and a bar scaled to the whole book would
        # be one pixel on every row, encoding nothing.
        bar = (f'<div class="cp-wbar" style="display:block;width:54px;margin-top:5px">'
               f'<i style="width:{min(100.0, weight * 400):.0f}%"></i></div>'
               ) if weight is not None else ""
        cells.append(f'<td class="num dim">{_pct(weight, 1)}{bar}</td>')
        cells.append(f'<td class="num dim">{_num(r.get("qty"), 4)}</td>')
        cells.append(f'<td class="num dim">{_money(r.get("avg_entry_price"), 2)} &rarr; '
                     f'{_money(r.get("current_price"), 2)}</td>')
        if has_held:
            days = r.get("days_held")
            cells.append(f'<td class="num dim">{days}d</td>' if days is not None
                         else '<td class="num dim">—</td>')
        if has_score:
            score, rank = r.get("score"), r.get("rank")
            if score is None:
                cells.append('<td class="num dim">—</td>')
            else:
                rank_html = f'<span class="sub">rank {rank}</span>' if rank else ""
                cells.append(f'<td class="num">{_theme.badge_html(f"{score:.1f}", "sb")}'
                             f"{rank_html}</td>")
        if has_reason:
            cells.append(f'<td class="dim" style="max-width:230px;font-size:12px">'
                         f'{_esc(r.get("reason") or "—")}</td>')
        body.append(f"<tr>{''.join(cells)}</tr>")

    return ('<div class="cp-scroll"><table class="cp-table"><thead><tr>'
            f'{"".join(head)}</tr></thead><tbody>{"".join(body)}</tbody></table></div>')


def _last_run_summary(decisions: list[dict]) -> str:
    """What the newest run did, in one line — so a COLLAPSED journal still
    answers the daily question. Hiding the panel must not hide the fact that it
    ran, or the fold saves pixels by costing information."""
    if not decisions:
        return "no runs yet"
    newest = decisions[0]
    run = [d for d in decisions if d.get("run_id") == newest.get("run_id")]
    traded = [d for d in run if d["status"] in (journal.SUBMITTED, journal.FILLED)]
    buys = sum(1 for d in traded if (d.get("action") or "").lower() == "buy")
    sells = len(traded) - buys
    blocked = sum(1 for d in run if d["status"] in (journal.BLOCKED, journal.ERROR))

    did = []
    if buys:
        did.append(f"{buys} buy{'s' if buys != 1 else ''}")
    if sells:
        did.append(f"{sells} sell{'s' if sells != 1 else ''}")
    if blocked:
        did.append(f"{blocked} blocked")
    return (f"last run {newest['decided_at']:%d %b %H:%M} UTC · "
            + (", ".join(did) if did else "nothing to do"))


# --------------------------------------------------------------------------
# Gather. One pass over the strategies; everything below reads from `rows`.
# --------------------------------------------------------------------------

_theme.page_header(
    "Trading Bot",
    eyebrow="Execution",
    sub="Autonomous paper trading · one Alpaca account per strategy · daily after the close",
)
st.caption(
    "Personal, educational tool — not financial advice. Every account here is **paper money** "
    "(Alpaca's paper endpoint, asserted before each order). This page is read-only apart from the "
    "per-strategy **Stop**; the bot itself runs on a GitHub Actions schedule, not from this browser."
)

configs = _cache.bot_configs()

if not configs:
    _theme.panel(
        "No strategies configured",
        '<p class="cp-note">Nothing has been seeded into <code>bot_config</code> yet. Run '
        "<code>python scripts/seed_bot_config.py</code> to register the strategies, then run "
        "<code>python scripts/run_bot.py --strategy &lt;name&gt; --dry-run</code> to see what one "
        "would do.</p>",
    )
    st.stop()

rows = []
for cfg in configs:
    name = cfg["strategy"]
    curve = _cache.bot_equity_curve(name)
    decisions = _cache.bot_decisions(name, JOURNAL_LIMIT)
    trades = sum(1 for d in decisions if d["status"] in (journal.SUBMITTED, journal.FILLED))
    rows.append({
        "config": cfg,
        "name": name,
        "label": bot_strategies.label(name),
        "curve": curve,
        "decisions": decisions,
        # Fetched once here and reused by the tab below — same cached call.
        # SLOTS and CASH are present-tense questions and have to be answered
        # from the broker, not from the last snapshot. The snapshot is written
        # the moment a run finishes submitting, which is BEFORE its orders
        # fill: after the close they queue until the next open, and on an
        # intraday run they are still filling. Reading it as "slots filled now"
        # showed 9 of 15 while the account actually held all 15.
        "view": _cache.bot_account_view(cfg.get("key_env_prefix") or ""),
        "summary": performance.summarise(
            curve, starting_equity=cfg.get("starting_equity"), trades=trades),
    })

rows.sort(key=lambda r: (r["summary"]["total_return"] is None,
                         -(r["summary"]["total_return"] or 0.0)))

# The SPY row is built from whichever strategy has been running longest — its
# `benchmark_equity` series IS SPY buy-and-hold from that strategy's inception,
# anchored by the bot on the same date. Any shorter strategy's benchmark is the
# same series over a later window, so the longest one is the honest choice.
_bench_source = max(
    (r for r in rows if any(p.get("benchmark_equity") is not None for p in r["curve"])),
    key=lambda r: len(r["curve"]),
    default=None,
)
bench_curve = [p for p in (_bench_source["curve"] if _bench_source else [])
               if p.get("benchmark_equity") is not None]
bench_values = [float(p["benchmark_equity"]) for p in bench_curve]
bench_summary = performance.summarise(
    [{"date": p["date"], "equity": p["benchmark_equity"]} for p in bench_curve]
) if bench_curve else None


# --------------------------------------------------------------------------
# Run status + the global stop
# --------------------------------------------------------------------------

now = datetime.now(timezone.utc)
last_decided = max((d["decided_at"] for r in rows for d in r["decisions"][:1]), default=None)
max_days = max((r["summary"]["days"] for r in rows), default=0)

# BOT_TRADING_ENABLED is a GitHub Actions *repo variable*, so it is normally
# absent from the app's own environment. Absent must not be reported as "off" —
# that would be a confident wrong answer about whether the bot is armed.
_switch = risk.trading_switch_state()
if _switch == risk.SWITCH_UNSET:
    switch_html = _theme.badge_html("set in Actions", "faint")
    switch_note = (f"<code>{risk.TRADING_ENABLED_VAR}</code> is a GitHub Actions repository "
                   "variable and isn't visible from the app — check it in the repo's settings.")
elif _switch == risk.SWITCH_ON:
    switch_html = _theme.badge_html("armed", "sb")
    switch_note = f"<code>{risk.TRADING_ENABLED_VAR}=true</code> in this environment."
else:
    switch_html = _theme.badge_html("global stop", "s")
    switch_note = (f"<code>{risk.TRADING_ENABLED_VAR}</code> is not <code>true</code> here, so no "
                   "order would be placed from this environment.")

killed = [r["label"] for r in rows if r["config"].get("killed")]
disabled = [r["label"] for r in rows if not r["config"].get("enabled")]

_theme.panel(
    "Run status",
    '<table class="cp-table"><tbody>'
    f'<tr><td class="dim">Strategies</td><td class="val">{len(rows)} registered'
    + (f' · <span class="down">{len(killed)} stopped</span>' if killed else "")
    + (f' · <span class="dim">{len(disabled)} disabled</span>' if disabled else "")
    + "</td></tr>"
    f'<tr><td class="dim">Last decision</td><td class="val">'
    f'{_esc(last_decided.strftime("%d %b %H:%M UTC")) if last_decided else "never"}</td></tr>'
    f'<tr><td class="dim">Next scheduled run</td><td class="val">'
    f'{_next_scheduled_run(now).strftime("%d %b %H:%M UTC")} '
    f'<span class="dim">· weekdays, 15 min after the warm-cache job</span></td></tr>'
    f'<tr><td class="dim">Global switch</td><td>{switch_html} '
    f'<span class="dim">{switch_note}</span></td></tr>'
    "</tbody></table>",
    tag=f"as of {now.strftime('%H:%M UTC')}",
)


# --------------------------------------------------------------------------
# The honesty banner — computed, not asserted.
# --------------------------------------------------------------------------

if max_days >= 2:
    se_at_one = performance.sharpe_stderr(max_days - 1, 1.0)
    days_needed = performance.days_for_sharpe_precision(0.5, 1.0)
    # One <span> wrapper, not bare text: .cp-advice is display:flex, so each
    # top-level child becomes its own column — several <b>s would lay the
    # sentence out as a row of narrow blocks instead of a paragraph.
    _theme.advice(
        f"<span><b>Day {max_days}.</b> At this sample size the standard error on an annualised "
        f"Sharpe of 1.0 is <b>±{se_at_one:.1f}</b> — several times any gap in the table below. Read "
        f"the ranking as a scoreboard in progress, not a result: pinning a Sharpe to ±0.5 takes "
        f"about <b>{days_needed:,} trading days</b> (~{days_needed / 252:.0f} years). Return and "
        "drawdown are descriptive and true today; Sharpe is an estimate and isn't.</span>"
    )
else:
    _theme.advice(
        "<span><b>Not enough history to compare yet.</b> The bot writes one equity snapshot per "
        "strategy per weekday run — the table fills in as those accumulate.</span>"
    )


# --------------------------------------------------------------------------
# Comparison table. SPY sits in rank order among the strategies, not as a
# footnote, so "we beat the market" has to be read rather than assumed.
# --------------------------------------------------------------------------

def _strategy_row(r: dict) -> tuple[float, str]:
    s, cfg, view = r["summary"], r["config"], r["view"]
    slots = cfg.get("target_slots") or 1

    # Live where we can reach the broker, the last snapshot where we can't (the
    # deployed Space holds no bot keys). Both halves from the SAME source, so
    # the cash percentage is never a live number divided by a stale one.
    if view.get("available"):
        held, cash_now, equity_now = len(view["positions"]), view["cash"], view["equity"]
    else:
        held, cash_now, equity_now = s["positions_count"], s["cash"], s["equity"]
    cash_pct = (cash_now / equity_now) if (cash_now is not None and equity_now) else None
    stopped = ' <span class="cp-badge s">stopped</span>' if cfg.get("killed") else ""
    curve_values = [float(p["equity"]) for p in r["curve"]]

    # The Sharpe never appears without its error bar. A naked "5.04" in a table
    # reads as a fact; "5.04 ±2.9" reads as what it is. And when the error
    # exceeds the estimate the number itself goes dim — the app's existing idiom
    # of rendering uncertainty as faintness rather than as a footnote.
    if s["sharpe"] is None:
        sharpe = (f'<span class="dim" title="Needs {performance.MIN_POINTS_FOR_SHARPE} daily '
                  'returns before it means anything">—</span>')
    else:
        se = s["sharpe_se"]
        faint = " dim" if (se is not None and se >= abs(s["sharpe"])) else ""
        sharpe = (f'<span class="{faint.strip()}">{s["sharpe"]:.2f}</span>'
                  + (f' <span class="dim">±{se:.1f}</span>' if se is not None else ""))

    html_row = (
        f'<tr><td><span class="tick">{_esc(r["label"])}</span>{stopped}</td>'
        f'<td class="num">{_money(s["equity"])}</td>'
        f'<td class="num {_cls(s["total_return"])}">{_signed_pct(s["total_return"])}</td>'
        f'<td class="num {_cls(s["excess_return"])}">{_signed_pct(s["excess_return"])}</td>'
        f'<td class="num">{sharpe}</td>'
        f'<td class="num {"down" if s["max_drawdown"] else "dim"}">{_pct(s["max_drawdown"])}</td>'
        f'<td class="num dim">{held if held is not None else "—"} / {slots}</td>'
        f'<td class="num dim">{_pct(cash_pct, 0)}</td>'
        f'<td class="num dim">{s["trades"]}</td>'
        f'<td>{_sparkline(curve_values, color=_theme.UP if (s["total_return"] or 0) >= 0 else _theme.DOWN)}</td>'
        "</tr>"
    )
    return (s["total_return"] if s["total_return"] is not None else -9e9), html_row


ranked = [_strategy_row(r) for r in rows]

if bench_summary:
    ranked.append((
        bench_summary["total_return"] if bench_summary["total_return"] is not None else -9e9,
        f'<tr><td><span class="tick dim">{BENCHMARK_LABEL}</span></td>'
        f'<td class="num dim">{_money(bench_summary["equity"])}</td>'
        f'<td class="num dim">{_signed_pct(bench_summary["total_return"])}</td>'
        f'<td class="num dim">—</td>'
        f'<td class="num dim">'
        f'{_num(bench_summary["sharpe"]) if bench_summary["sharpe"] is not None else "—"}</td>'
        f'<td class="num dim">{_pct(bench_summary["max_drawdown"])}</td>'
        f'<td class="num dim">—</td><td class="num dim">0%</td><td class="num dim">1</td>'
        f'<td>{_sparkline(bench_values, color=_theme.MUTED, dashed=True)}</td></tr>',
    ))

ranked.sort(key=lambda pair: -pair[0])

_theme.panel(
    "How they compare",
    '<div class="cp-scroll"><table class="cp-table"><thead><tr>'
    '<th>Strategy</th><th class="num">Equity</th><th class="num">Return</th>'
    '<th class="num">vs SPY</th><th class="num">Sharpe</th><th class="num">Max DD</th>'
    '<th class="num">Slots</th><th class="num">Cash</th><th class="num">Trades</th>'
    "<th>Curve</th></tr></thead>"
    f'<tbody>{"".join(h for _, h in ranked)}</tbody></table></div>'
    '<p class="cp-foot">Drawdown and cash sit beside return on purpose — a leader holding half its '
    "book in cash after a deep drawdown is a different result from the same return earned fully "
    "invested, and a return column alone hides that. <b>vs SPY</b> is percentage points of return "
    "against SPY bought on that strategy's own first day. Each <b>Sharpe</b> carries its standard "
    "error, and goes dim where that error is larger than the estimate itself.</p>",
    tag=f"{len(rows)} strategies · SPY pinned in rank order",
)


# --------------------------------------------------------------------------
# Combined chart — every account rebased to 100 so five books that started on
# different days at different equities share one axis.
# --------------------------------------------------------------------------

plottable = [r for r in rows if len(r["curve"]) >= 2]

if plottable:
    with _theme.section("Account value since inception", tag="rebased to 100 at each start"):
        fig = go.Figure()
        for r in plottable:
            values = [float(p["equity"]) for p in r["curve"]]
            fig.add_trace(go.Scatter(
                x=[p["date"] for p in r["curve"]],
                y=performance.rebased(values),
                name=r["label"], mode="lines",
                hovertemplate="%{x|%d %b} · %{y:.2f}<extra>" + _esc(r["label"]) + "</extra>",
            ))
        if len(bench_values) >= 2:
            fig.add_trace(go.Scatter(
                x=[p["date"] for p in bench_curve],
                y=performance.rebased(bench_values),
                name=BENCHMARK_LABEL, mode="lines",
                line=dict(color=_theme.MUTED, dash="dash", width=1.6),
                hovertemplate="%{x|%d %b} · %{y:.2f}<extra>SPY</extra>",
            ))
        fig.add_hline(y=100, line_width=1, line_color=_theme.LINE)
        fig.update_layout(height=340, hovermode="x unified",
                          legend=dict(orientation="h", y=-0.16))
        fig.update_yaxes(title_text="Rebased (start = 100)")
        st.plotly_chart(fig, width="stretch", key="bot_combined", theme=None)


# --------------------------------------------------------------------------
# Per-strategy tabs
# --------------------------------------------------------------------------

st.markdown("### Per strategy")

# Tabs use the SHORT labels and the blueprint's build order, not the table's
# ranking. Two separate fixes: the full labels run to 40 characters and pushed
# the last tabs off the row behind a scroll chevron, and a tab that changes
# position because a curve crossed overnight makes the page harder to use every
# day. The comparison table above is already ranked; this is navigation.
tab_rows = sorted(rows, key=lambda r: bot_strategies.display_index(r["name"]))
tabs = st.tabs([bot_strategies.short_label(r["name"]) for r in tab_rows])

# The ranking the screener strategies trade off, fetched once for all tabs. It
# carries a company name, score and rank per ticker, which is what turns the
# holdings table from a list of symbols into something you can read. Strategies
# that don't trade the S&P (golden cross, creator conviction) simply find no
# match and their columns drop out.
leaderboard_rows = list(_cache.bot_leaderboard().get("rows") or [])

for tab, r in zip(tabs, tab_rows):
    with tab:
        name, cfg, s, curve = r["name"], r["config"], r["summary"], r["curve"]
        _theme.eyebrow(r["label"])

        if cfg.get("killed"):
            st.warning(f"**{r['label']} is stopped.** Its runs will halt at the rails and place no "
                       "orders until it's resumed. Existing positions are left untouched — a stop "
                       "stops trading, it doesn't liquidate.")
        elif not cfg.get("enabled"):
            st.info(f"**{r['label']} is disabled** in `bot_config` and won't run.")

        # ---- quick numbers ----
        # Six st.metric cards became one strip. Same six figures, 66px instead
        # of 110, and the height is what buys the holdings table its place. The
        # Sharpe keeps its error bar inline rather than in a delta slot, so the
        # estimate and its uncertainty stay one number rather than two rows.
        if s["sharpe"] is None:
            sharpe_html, sharpe_cls, sharpe_se = "—", "dim", None
        else:
            se = s["sharpe_se"]
            sharpe_html = f'{s["sharpe"]:.2f}'
            sharpe_cls = "dim" if (se is not None and se >= abs(s["sharpe"])) else ""
            sharpe_se = f"±{se:.1f}" if se is not None else None

        _strip(
            _stat("Equity", _money(s["equity"], 0)),
            _stat("Return", _signed_pct(s["total_return"]), cls=_cls(s["total_return"])),
            _stat("vs SPY", _signed_pct(s["excess_return"]), cls=_cls(s["excess_return"])),
            _stat("Sharpe", sharpe_html, cls=sharpe_cls, se=sharpe_se),
            _stat("Max drawdown", _pct(s["max_drawdown"]),
                  cls="down" if s["max_drawdown"] else "dim"),
            _stat("Days live", f"{s['days']}"),
        )

        # ---- this strategy's own account value, with SPY dashed behind it ----
        if len(curve) >= 2:
            with _theme.section("Account value", tag=f"since {curve[0]['date']:%d %b %Y}"):
                own_bench = [p for p in curve if p.get("benchmark_equity") is not None]
                start = cfg.get("starting_equity")

                fig = go.Figure()
                fig.add_trace(go.Scatter(
                    x=[p["date"] for p in curve],
                    y=[float(p["equity"]) for p in curve],
                    name=r["label"], mode="lines", fill="tozeroy",
                    line=dict(color=_theme.ACCENT_INK, width=2),
                    fillcolor="rgba(232,178,74,.10)",
                    hovertemplate="%{x|%d %b} · $%{y:,.0f}<extra></extra>",
                ))
                if len(own_bench) >= 2:
                    fig.add_trace(go.Scatter(
                        x=[p["date"] for p in own_bench],
                        y=[float(p["benchmark_equity"]) for p in own_bench],
                        name=BENCHMARK_LABEL, mode="lines",
                        line=dict(color=_theme.MUTED, dash="dash", width=1.6),
                        hovertemplate="%{x|%d %b} · $%{y:,.0f}<extra>SPY</extra>",
                    ))
                if start:
                    fig.add_hline(y=float(start), line_width=1, line_dash="dot",
                                  line_color=_theme.LINE)

                # Frame the y-axis on the data, not on zero. `fill="tozeroy"`
                # would otherwise drag the axis to $0 and squash a whole year's
                # movement into the top few percent of the plot — a +16% run
                # rendered as a flat line. The fill still reaches the bottom of
                # the frame; it's simply clipped there, which is the filled-area
                # look the design asked for. Includes the benchmark and the
                # starting line so neither can fall outside the window.
                span_values = [float(p["equity"]) for p in curve]
                span_values += [float(p["benchmark_equity"]) for p in own_bench]
                if start:
                    span_values.append(float(start))
                lo, hi = min(span_values), max(span_values)
                pad = (hi - lo) * 0.08 or max(1.0, hi * 0.005)

                fig.update_layout(height=280, hovermode="x unified",
                                  legend=dict(orientation="h", y=-0.2))
                fig.update_yaxes(title_text="Account value (USD)",
                                 range=[lo - pad, hi + pad])
                # The caption that used to sit here now lives in "How this
                # strategy works" at the foot of the tab. It explains the chart
                # rather than reading it, and an explanation you have already
                # read is just something between you and the next panel.
                st.plotly_chart(fig, width="stretch", key=f"bot_curve_{name}", theme=None)
        else:
            _theme.panel("Account value",
                         '<p class="cp-note">Needs at least two daily snapshots to draw a line. '
                         "The bot writes one per weekday run.</p>")

        # ---- live account: positions, or an honest note about why not ----
        view = r["view"]
        slots = cfg.get("target_slots") or 1

        # Take cash and equity from the SAME source. Mixing live cash with the
        # last snapshot's equity would divide two numbers measured a day apart
        # and print a cash percentage that was never true of either.
        if view["available"]:
            held_now = len(view["positions"])
            cash_now, equity_now = view["cash"], view["equity"]
        else:
            held_now = s["positions_count"] or 0
            cash_now, equity_now = s["cash"], s["equity"]
        cash_pct = (cash_now / equity_now) if (cash_now is not None and equity_now) else None
        invested_pct = (1.0 - cash_pct) if cash_pct is not None else None

        # ---- slots, filled, cash ----
        _strip(
            _slots_cell(held_now, slots),
            _stat("Filled", f"{held_now}", se=f"/ {slots}"),
            _stat("Cash", _money(cash_now, 0), se=_pct(cash_pct, 0)),
            _stat("Invested", _pct(invested_pct, 0)),
            _stat("Position size",
                  f'equity × {_tidy_pct(min(1 / slots, cfg.get("max_position_pct") or 1.0))}'
                  if slots else "—"),
        )

        # ---- open positions ----
        # Two sources, one table. Live from the broker where the key pair is in
        # the environment; otherwise replayed from the journal and priced at the
        # last cached close, which is what the deployed Space gets — it holds no
        # bot keys, only GitHub Actions does. The reconstruction is weaker (it
        # assumes a submitted order filled, and a close is not "now") so it is
        # labelled as such rather than passed off as live.
        if view["available"]:
            raw, live_book = view["positions"], True
        else:
            raw, live_book = _cache.bot_reconstructed_book(name), False

        if raw:
            fills = _cache.bot_fills(name)
            enriched = bot_positions.enrich(
                raw,
                equity=equity_now,
                names=_cache.bot_position_names(tuple(p["ticker"] for p in raw)),
                ranks=bot_positions.rank_index(leaderboard_rows),
                reasons=bot_positions.latest_reasons(r["decisions"]),
                since=bot_positions.held_since(fills),
                today=now.date(),
            )
            invested = sum(p.get("market_value") or 0.0 for p in enriched)

            if live_book:
                tag = f"{len(enriched)} held · {_money(invested)} invested · live from Alpaca"
                footer = ""
            else:
                priced = next((p.get("priced_at") for p in raw if p.get("priced_at")), None)
                tag = (f"{len(enriched)} held · rebuilt from the journal"
                       + (f" · priced {priced:%d %b}" if priced else ""))
                footer = (
                    '<p class="cp-foot"><b>Reconstructed, not live.</b> No Alpaca key pair for '
                    "this strategy in this environment, so these are the bot's own filled "
                    "quantities priced at the last cached close — not the broker's position "
                    "list, and not an intraday value. Everything else on this tab is read from "
                    f'the database and is unaffected.</p><p class="cp-foot">'
                    f'{_esc(view["error"])}</p>')

            _theme.panel("Open positions", _positions_table(enriched) + footer, tag=tag)
        elif view["available"]:
            _theme.panel("Open positions",
                         '<p class="cp-note">The account holds nothing right now — all cash.</p>')
        else:
            _theme.panel(
                "Open positions",
                '<p class="cp-note">No Alpaca key pair for this strategy here, and the journal '
                "has no filled orders to rebuild a book from — so this strategy has not bought "
                "anything yet. Everything above is read from the database and is unaffected."
                f'</p><p class="cp-foot">{_esc(view["error"])}</p>',
                tag="nothing to show",
            )

        # ---- the panel only this strategy can have ----
        _bot_panels.render(name, cfg, view)

        # ---- what it decided, including the runs where nothing happened ----
        # Folded away, but the LABEL carries the last run's outcome. Collapsing
        # a panel is only free if the collapsed state still answers the question
        # the panel existed for — "did it run today, and what did it do" —
        # otherwise the fold saves pixels by costing information.
        recent = r["decisions"][:15]
        with st.expander(f"Recent decisions — {_last_run_summary(r['decisions'])}"):
            if recent:
                body = "".join(
                    f'<tr><td class="dim num">{d["decided_at"]:%d %b %H:%M}</td>'
                    f'<td><span class="tick">{_esc(d["ticker"] or "—")}</span></td>'
                    f'<td>{_esc((d["action"] or "").title())}</td>'
                    f'<td>{_esc(d["reason"])}</td>'
                    f'<td>{_status_badge(d["status"])}'
                    + (f' <span class="dim">{_esc(d["blocked_by"])}</span>'
                       if d["blocked_by"] else "")
                    + "</td></tr>"
                    for d in recent
                )
                _theme.panel(
                    "Recent decisions",
                    '<div class="cp-scroll"><table class="cp-table"><thead><tr>'
                    '<th>When</th><th>Ticker</th><th>Action</th><th>Reason</th><th>Result</th>'
                    f"</tr></thead><tbody>{body}</tbody></table></div>",
                    tag=f"{len(recent)} of {len(r['decisions'])}",
                )
            else:
                _theme.panel("Recent decisions",
                             '<p class="cp-note">This strategy hasn\'t run yet.</p>')

        # ---- the prose that used to sit between every panel ----
        # Six explanatory captions per tab were six things to scroll past on the
        # 250th day of reading the same tab. Gathered here so the tab is numbers
        # by default and an explanation on request.
        with st.expander("How this strategy works"):
            st.markdown(
                f"**Sizing.** Every position is `equity × min(1/{slots}, "
                f"{(cfg.get('max_position_pct') or 1.0):.0%})`, so it grows with the account "
                "instead of being pinned to a dollar figure. The same rule runs on all six "
                "strategies on purpose — these curves are meant to test the *signals*, not six "
                "different sizing schemes.\n\n"
                f"**Unfilled slots stay in cash.** {held_now} of {slots} are working; the rest "
                "isn't idle by accident, it's the strategy declining to buy something it doesn't "
                "rate.\n\n"
                "**The chart.** Every tab uses the same y-axis convention — framed on the data, "
                "not on zero — so flipping between tabs is a fair visual comparison. It answers a "
                "different question from the chart at the top of the page: not *which is ahead* "
                "but *how this one got here* — a steady grind or one lucky week, and where the "
                "drawdown actually happened.\n\n"
                "**Holdings.** Value, P&L and today's move come from the broker. The company "
                "name, score and rank come from the cached S&P ranking the bot trades off, so "
                "the score you see is the one it will act on next run. *Held* counts from the "
                "first buy of the current holding — a name bought, sold and bought again dates "
                "from the second buy."
            )

        # ---- the one control on the page ----
        with st.expander("Controls"):
            st.caption(
                f"`{name}` · account keys `{cfg.get('key_env_prefix') or '—'}` · "
                f"{cfg.get('target_slots')} slots · "
                f"max {(cfg.get('max_position_pct') or 0):.0%} per position · "
                f"started at {_money(cfg.get('starting_equity'), 2)}"
            )
            if cfg.get("killed"):
                # Resuming ARMS an autonomous trader, so it takes two deliberate
                # actions; stopping takes one. The asymmetry is the same
                # fail-safe principle as BOT_TRADING_ENABLED in risk.py — the
                # accidental outcome should always be the safe one.
                confirm = st.checkbox(
                    "I want this strategy to start placing orders again on the next scheduled run.",
                    key=f"bot_confirm_{name}",
                )
                if st.button("Resume trading", key=f"bot_resume_{name}", disabled=not confirm):
                    journal.set_killed(name, False)
                    _cache.clear()
                    st.rerun()
            else:
                st.caption(
                    "Stop halts this strategy at the rails on its next run — no orders, existing "
                    "positions untouched. It's a database flag, so it takes effect without a "
                    "deploy. The global stop is the `BOT_TRADING_ENABLED` repo variable."
                )
                if st.button("⏸ Stop this strategy", key=f"bot_stop_{name}", type="primary"):
                    journal.set_killed(name, True)
                    _cache.clear()
                    st.rerun()


# --------------------------------------------------------------------------
# The global journal. The rows worth having are the ones where nothing happened
# — but that is a question you ask occasionally, not a table you want between
# you and the page every day. Folded, with the last run named on the label so
# the collapsed state still says whether the bot ran at all.
#
# Worth knowing: Streamlit runs an expander's body whether or not it is open, so
# this saves screen space rather than query time. That's fine — every read
# inside it goes through _cache — but it does mean the filters below are
# evaluated on every rerun regardless.
# --------------------------------------------------------------------------

# Deliberately NOT _last_run_summary here: that summarises one run_id, which
# belongs to a single strategy. "7 buys" on a label that says "every strategy"
# would be a wrong number rather than a short one.
_journal_label = (f"newest {last_decided:%d %b %H:%M} UTC" if last_decided
                  else "nothing journalled yet")

with st.expander(f"Decision journal — every strategy · {_journal_label}"):
    st.caption(
        "Newest first. The interesting rows are the ones where **nothing happened** — an order "
        "blocked by a full slot list, a name skipped by a filter, a duplicate refused on a "
        "workflow retry. A log of fills tells you what the bot did; this tells you what it decided."
    )

    f1, f2, f3 = st.columns([2, 2, 1])
    pick = f1.selectbox("Strategy", ["All strategies"] + [r["label"] for r in rows], index=0)
    statuses = f2.multiselect(
        "Result",
        [journal.SUBMITTED, journal.FILLED, journal.BLOCKED, journal.SKIPPED,
         journal.DRY_RUN, journal.ERROR],
        default=[],
        placeholder="Any result",
    )
    limit = f3.number_input("Rows", min_value=10, max_value=500, value=60, step=10)

    _selected = next((r["name"] for r in rows if r["label"] == pick), None)
    entries = _cache.bot_decisions(_selected, int(limit) * (1 if _selected else 3))
    if statuses:
        entries = [d for d in entries if d["status"] in statuses]
    entries = entries[: int(limit)]

    _label_by_name = {r["name"]: r["label"] for r in rows}

    if entries:
        body = "".join(
            f'<tr><td class="dim num">{d["decided_at"]:%d %b %H:%M}</td>'
            f'<td class="dim">{_esc(_label_by_name.get(d["strategy"], d["strategy"]))}</td>'
            f'<td><span class="tick">{_esc(d["ticker"] or "—")}</span></td>'
            f'<td>{_esc((d["action"] or "").title())}</td>'
            f'<td>{_esc(d["reason"])}</td>'
            f'<td>{_status_badge(d["status"])}'
            + (f' <span class="dim">{_esc(d["blocked_by"])}</span>' if d["blocked_by"] else "")
            + "</td></tr>"
            for d in entries
        )
        _theme.panel(
            "All decisions",
            '<div class="cp-scroll"><table class="cp-table"><thead><tr>'
            '<th>When</th><th>Strategy</th><th>Ticker</th><th>Action</th><th>Reason</th>'
            f"<th>Result</th></tr></thead><tbody>{body}</tbody></table></div>",
            tag=f"{len(entries)} rows",
        )
    else:
        _theme.panel("All decisions",
                     '<p class="cp-note">No decisions match those filters yet.</p>')
