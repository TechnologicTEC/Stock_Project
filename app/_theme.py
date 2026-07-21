"""
"Honest terminal" visual system — applied on every page.

Two layers. `.streamlit/config.toml` carries the base tokens Streamlit
understands natively (dark ground, amber primary). This module layers everything
the native theme can't express and, crucially, the *structure* that makes it read
as a terminal rather than a restyled default:

- a persistent top status bar (brand, market state, as-of, "not advice"),
- a grouped, labelled sidebar nav replacing Streamlit's lowercase file list,
- panel-style metric cards, monospace data, badges, slim advice bars.

Design language (see the signed-off mockup):
- Amber accent on a cool blue-ink ground. One bold hue; everything else quiet.
- All *data* (tickers, prices, scores) is monospace with tabular figures; prose
  stays in the UI sans. That split is the terminal identity.
- Uncertainty is rendered as FAINTNESS (dim, dashed), never a false-confidence
  colour — the app's "faint tilt, not a prediction" personality, made visual.

Call `apply()` once per page, right after st.set_page_config(). It injects the
CSS and renders the top bar + sidebar nav. Cheap and idempotent; Streamlit reruns
each page script, so each must re-inject.
"""
from __future__ import annotations

from datetime import datetime, timezone

import streamlit as st

# Kept in sync with .streamlit/config.toml and the mockup.
#
# Lightened from the first cut (#0a0e14): near-black read as harsh, and — measured,
# not guessed — MUTED sat at a 3.65 contrast ratio on it, under the WCAG AA floor
# of 4.5 for normal text. Captions and table dims were genuinely hard to read. On
# this grey-blue ground muted is 5.01, dim 8.35, body text 13.56, accent 8.94, so
# every tone on the page clears AA.
GROUND, PANEL, PANEL_2 = "#161b26", "#1e2531", "#1a202b"
LINE, LINE_SOFT = "#2c3546", "#242c3a"
TEXT, TEXT_DIM, MUTED = "#dde5ef", "#a9b6c4", "#7e8c9b"
ACCENT, ACCENT_DIM = "#e8b24a", "#8a6f36"
UP, DOWN = "#4bc16d", "#ef6147"

_MONO = 'ui-monospace, "SF Mono", "JetBrains Mono", "Cascadia Code", Menlo, Consolas, monospace'

# Grouped nav — paths are relative to the entrypoint (app/main.py), so pages are
# "pages/…". Labels replace Streamlit's lowercase filenames; grouping encodes what
# each page is *for* (own money → research it → act on it).
_NAV = [
    ("Portfolio", [("main.py", "Dashboard"),
                   ("pages/1_portfolio.py", "Holdings"),
                   ("pages/3_health.py", "Health & Projections")]),
    ("Research", [("pages/2_screener.py", "Screener"),
                  ("pages/6_validation.py", "Validation"),
                  ("pages/4_news.py", "News & Earnings"),
                  ("pages/10_creator_signals.py", "Creator Signals")]),
    ("Execution", [("pages/5_backtest.py", "Backtest"),
                   ("pages/7_paper_trading.py", "Paper Trading"),
                   ("pages/8_chat.py", "Assistant"),
                   ("pages/9_settings.py", "Settings")]),
]

_CSS = f"""
<style>
:root {{
  --cp-ground:{GROUND}; --cp-panel:{PANEL}; --cp-panel-2:{PANEL_2};
  --cp-line:{LINE}; --cp-line-soft:{LINE_SOFT};
  --cp-text:{TEXT}; --cp-dim:{TEXT_DIM}; --cp-muted:{MUTED};
  --cp-accent:{ACCENT}; --cp-accent-dim:{ACCENT_DIM};
  --cp-up:{UP}; --cp-down:{DOWN};
  --cp-mono:{_MONO};
}}

/* ---------- ground ---------- */
.stApp {{
  background:
    radial-gradient(1100px 560px at 82% -8%, rgba(232,178,74,.05), transparent 60%),
    var(--cp-ground);
}}
/* Streamlit's own header strip: make it vanish so our bar reads as THE top. The
   sidebar-collapse control lives here and stays clickable. */
[data-testid="stHeader"] {{ background: transparent; height: 0; }}
[data-testid="stMainBlockContainer"], .block-container {{
  padding-top: 3.9rem; padding-bottom: 4rem; max-width: 1240px;
}}

/* ---------- typography ---------- */
.stApp, .stMarkdown, p, li, label {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}}
h1 {{ font-size: 1.5rem; letter-spacing: -.01em; font-weight: 700; text-wrap: balance; }}
h2 {{ font-size: 1.16rem; letter-spacing: -.005em; font-weight: 650; }}
h3 {{ font-size: 1rem; font-weight: 650; color: var(--cp-text); }}
h1, h2, h3 {{ margin-bottom: .5rem; }}
a {{ text-decoration: none; }} a:hover {{ text-decoration: underline; }}
.cp-eyebrow {{
  font-size: 10.5px; letter-spacing: .16em; text-transform: uppercase;
  color: var(--cp-muted); font-weight: 700;
}}

/* ---------- top status bar: full-bleed across the whole viewport ----------
   z-index 999992 is picked, not arbitrary: Streamlit's sidebar sits at 999991
   (it was painting over the bar's left end), while its dropdown/modal portal is
   at 1000110. Sitting between the two puts the bar above the sidebar — as in the
   mockup — without ever covering a menu, tooltip or dialog. */
.cp-topbar {{
  position: fixed; top: 0; left: 0; right: 0; z-index: 999992;
  display: flex; align-items: center; gap: 14px;
  margin: 0; padding: 10px 18px;
  border: 0; border-bottom: 1px solid var(--cp-line); border-radius: 0;
  background: linear-gradient(180deg, #212936, #1a202b);
  font-size: 12px;
}}
/* clear the fixed bar (it spans over the sidebar too, as in the mockup) */
[data-testid="stSidebar"] {{ padding-top: 44px; }}
.cp-topbar .brand {{ display:flex; align-items:center; gap:8px; font-weight:700; letter-spacing:.01em; color:var(--cp-text); }}
.cp-topbar .brand .glyph {{ color: var(--cp-accent); font-size: 15px; }}
.cp-topbar .brand small {{ color:var(--cp-muted); font-weight:500; letter-spacing:.14em; font-size:9.5px; text-transform:uppercase; }}
.cp-topbar .spacer {{ flex: 1; }}
.cp-chip {{
  display:inline-flex; align-items:center; gap:7px; font-family: var(--cp-mono); font-size: 11px;
  color: var(--cp-dim); padding: 3px 9px; border:1px solid var(--cp-line-soft); border-radius:5px; background: var(--cp-panel-2);
}}
.cp-chip .dot {{ width:7px; height:7px; border-radius:50%; }}
.cp-chip .dot.open {{ background: var(--cp-up); box-shadow: 0 0 0 3px rgba(75,193,109,.15); }}
.cp-chip .dot.closed {{ background: var(--cp-muted); box-shadow: 0 0 0 3px rgba(95,109,124,.15); }}
.cp-pill-advice {{
  font-size: 9.5px; letter-spacing:.12em; text-transform:uppercase; font-weight:700;
  color: var(--cp-accent); border:1px dashed var(--cp-accent-dim); border-radius:5px;
  padding: 3px 8px; background: rgba(232,178,74,.06);
}}
/* identity, in the header rather than the sidebar */
.cp-id {{ display:inline-flex; align-items:center; gap:8px; font-size: 12px; color: var(--cp-dim); }}
.cp-id .av {{
  width:22px; height:22px; border-radius:50%; display:inline-grid; place-items:center;
  background: linear-gradient(135deg,#2c3646,#1d2430); border:1px solid var(--cp-line);
  font-size:10px; color: var(--cp-accent); font-weight:700; flex: 0 0 auto;
}}
.cp-id .who {{ max-width: 210px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
.cp-id .role {{ color: var(--cp-muted); }}
@media (max-width: 900px) {{ .cp-id .who, .cp-id .role {{ display:none; }} }}

/* ---------- page header (eyebrow + title + sub) ---------- */
.cp-head {{ margin: 2px 0 18px; }}
.cp-head h1 {{ margin: 3px 0 0; }}
.cp-head .sub {{ color: var(--cp-muted); font-family: var(--cp-mono); font-size: 12px; margin-top: 4px; }}

/* ---------- slim advice bar (replaces the fat blue box) ---------- */
.cp-advice {{
  display:flex; align-items:center; gap:10px; margin: 0 0 18px; padding: 9px 13px;
  border:1px solid var(--cp-line); border-left: 2px solid var(--cp-accent-dim);
  border-radius: 7px; background: rgba(232,178,74,.04);
  color: var(--cp-dim); font-size: 12.5px; line-height: 1.5;
}}
.cp-advice b {{ color: var(--cp-text); }}

/* ---------- sidebar nav (custom, grouped) ---------- */
[data-testid="stSidebarNav"] {{ display: none; }}      /* hide the lowercase file list */
[data-testid="stSidebar"] {{ background: var(--cp-panel-2); border-right: 1px solid var(--cp-line); }}
.cp-sb-brand {{ display:flex; align-items:center; gap:8px; font-weight:700; padding: 2px 4px 2px; color:var(--cp-text); }}
.cp-sb-brand .glyph {{ color: var(--cp-accent); }}
.cp-navsec {{ margin: 15px 4px 5px; }}
[data-testid="stSidebar"] [data-testid="stPageLink"] a {{
  border-radius: 6px; padding: 5px 9px; margin: 1px 0; border: 1px solid transparent;
}}
[data-testid="stSidebar"] [data-testid="stPageLink"] a p {{ font-size: 13.5px; color: var(--cp-dim); }}
[data-testid="stSidebar"] [data-testid="stPageLink"] a:hover {{ background: rgba(255,255,255,.03); }}
[data-testid="stSidebar"] [data-testid="stPageLink"] a[aria-current] {{
  background: rgba(232,178,74,.10); border-color: rgba(232,178,74,.22);
}}
[data-testid="stSidebar"] [data-testid="stPageLink"] a[aria-current] p {{ color: var(--cp-text); font-weight: 600; }}

/* ---------- metric cards ---------- */
[data-testid="stMetric"] {{
  background: linear-gradient(180deg, var(--cp-panel), var(--cp-panel-2));
  border: 1px solid var(--cp-line); border-radius: 8px; padding: 13px 15px 14px;
}}
[data-testid="stMetricLabel"] p {{
  font-size: 10.5px !important; letter-spacing:.13em; text-transform:uppercase;
  color: var(--cp-muted) !important; font-weight: 600;
}}
[data-testid="stMetricValue"] {{
  font-family: var(--cp-mono) !important; font-variant-numeric: tabular-nums;
  letter-spacing: -.02em; font-weight: 600; font-size: 1.7rem;
}}
[data-testid="stMetricDelta"] {{ font-family: var(--cp-mono) !important; font-variant-numeric: tabular-nums; font-size: .82rem; }}

/* ---------- buttons ---------- */
.stButton > button, .stDownloadButton > button, .stFormSubmitButton > button {{
  border-radius: 6px; font-weight: 600; letter-spacing:.01em; border: 1px solid var(--cp-line);
}}
.stButton > button[kind="primary"], .stFormSubmitButton > button {{ color: #17130a; }}
.stButton > button:focus-visible {{ outline: 2px solid var(--cp-accent); outline-offset: 2px; }}

/* ---------- inputs / tabs / expanders / alerts ---------- */
[data-baseweb="input"], [data-baseweb="select"], [data-baseweb="textarea"] {{ border-radius: 6px; }}
.stTabs [data-baseweb="tab-list"] {{ gap: 4px; border-bottom: 1px solid var(--cp-line); }}
.stTabs [aria-selected="true"] {{ color: var(--cp-accent); }}
[data-testid="stExpander"] details {{
  background: linear-gradient(180deg, var(--cp-panel), var(--cp-panel-2));
  border: 1px solid var(--cp-line); border-radius: 8px;
}}
[data-testid="stExpander"] summary:hover {{ color: var(--cp-accent); }}
[data-testid="stAlert"] {{ border-radius: 7px; border: 1px solid var(--cp-line); }}

/* ---------- data tables ---------- */
[data-testid="stDataFrame"], [data-testid="stTable"] {{
  font-family: var(--cp-mono); font-variant-numeric: tabular-nums;
  border: 1px solid var(--cp-line); border-radius: 8px;
}}
[data-testid="stTable"] th {{ text-transform: uppercase; letter-spacing:.08em; font-size:10px; color: var(--cp-muted); font-weight:600; }}
hr {{ border-color: var(--cp-line-soft); margin: 1.4rem 0; }}

/* ---------- badges ---------- */
.cp-badge {{
  font-family: var(--cp-mono); font-size: 11px; font-weight: 600; letter-spacing:.02em;
  padding: 2px 8px; border-radius: 4px; border: 1px solid transparent; white-space: nowrap;
}}
.cp-badge.sb {{ color:#8ff0aa; background: rgba(75,193,109,.13); border-color: rgba(75,193,109,.30); }}
.cp-badge.b  {{ color:#bfe8c9; background: rgba(75,193,109,.08); border-color: rgba(75,193,109,.18); }}
.cp-badge.h  {{ color: var(--cp-dim); background: rgba(147,161,177,.08); border-color: var(--cp-line); }}
.cp-badge.s  {{ color:#f3b3a5; background: rgba(239,97,71,.10); border-color: rgba(239,97,71,.24); }}
.cp-badge.faint {{ color: var(--cp-accent); background: rgba(232,178,74,.05); border: 1px dashed var(--cp-accent-dim); }}

/* ---------- panels (the mockup's body sections) ---------- */
.cp-panel {{
  background: linear-gradient(180deg, var(--cp-panel), var(--cp-panel-2));
  border: 1px solid var(--cp-line); border-radius: 8px; padding: 15px 16px; margin-bottom: 14px;
}}
.cp-panel > .ph {{ display:flex; align-items:center; justify-content:space-between; margin-bottom: 12px; }}
.cp-panel > .ph .tag {{ font-family: var(--cp-mono); font-size: 10.5px; color: var(--cp-muted); }}

/* ---------- terminal table ----------
   Everything is LEFT-aligned with generous right padding: right-aligned columns
   pushed each value hard against the next column's edge, which made the numbers
   tiring to read. Monospace + tabular-nums keeps digits lining up regardless of
   alignment, so nothing is lost by aligning left. Vertical rules are explicitly
   removed — Streamlit's own markdown-table CSS was drawing them. */
.cp-table {{ width: 100%; border-collapse: collapse; }}
.cp-table th, .cp-table td {{
  border-left: 0 !important; border-right: 0 !important; text-align: left;
}}
.cp-table thead th {{
  font-size: 10px; letter-spacing:.1em; text-transform: uppercase;
  color: var(--cp-muted); font-weight: 600;
  padding: 0 22px 9px 0; border-bottom: 1px solid var(--cp-line); white-space: nowrap;
}}
.cp-table tbody td {{
  padding: 10px 22px 10px 0; border-bottom: 1px solid var(--cp-line-soft);
  font-size: 13px; color: var(--cp-text);
}}
.cp-table th:last-child, .cp-table td:last-child {{ padding-right: 0; }}
.cp-table tbody tr:last-child td {{ border-bottom: none; }}
.cp-table tbody tr:hover td {{ background: rgba(255,255,255,.015); }}
.cp-table .tick {{ font-family: var(--cp-mono); font-weight: 600; letter-spacing:.02em; }}
.cp-table .co {{ color: var(--cp-dim); font-size: 12px; }}
.cp-table .val {{ font-family: var(--cp-mono); font-variant-numeric: tabular-nums; }}
.cp-table .up {{ color: var(--cp-up); }} .cp-table .down {{ color: var(--cp-down); }}
/* keep the mono/tabular figures, but don't re-introduce right alignment */
.cp-table td.num, .cp-table th.num {{ text-align: left; }}
/* wide tables scroll inside their own panel — never the page body */
.cp-scroll {{ overflow-x: auto; }}
.cp-table td.num, .cp-table th.num {{ font-family: var(--cp-mono); font-variant-numeric: tabular-nums; white-space: nowrap; }}
.cp-table .dim {{ color: var(--cp-muted); }}
.cp-wbar {{ height: 5px; border-radius: 3px; background: var(--cp-line); overflow: hidden; min-width: 46px; display:inline-block; vertical-align: middle; }}
.cp-wbar > i {{ display:block; height:100%; background: linear-gradient(90deg, var(--cp-accent-dim), var(--cp-accent)); }}

/* ---------- signal confidence (uncertainty rendered as faintness) ---------- */
.cp-conf .ic {{ display:flex; align-items:baseline; gap:10px; }}
.cp-conf .ic b {{ font-family: var(--cp-mono); font-size: 32px; font-weight: 600; letter-spacing:-.02em; color: var(--cp-accent); }}
.cp-conf .ic .t {{ font-family: var(--cp-mono); font-size: 11.5px; color: var(--cp-dim); }}
.cp-meter {{
  margin: 13px 0 6px; height: 10px; border-radius: 5px; position: relative; overflow: hidden;
  background: repeating-linear-gradient(90deg, var(--cp-line) 0 1px, transparent 1px 26px), var(--cp-panel-2);
  border: 1px solid var(--cp-line);
}}
.cp-meter > i {{ position:absolute; inset:0 auto 0 0; background: linear-gradient(90deg, rgba(232,178,74,.25), var(--cp-accent)); border-radius: 5px 0 0 5px; }}
.cp-verdict {{
  display:inline-flex; align-items:center; gap:8px; margin-top: 4px;
  font-size: 10.5px; font-family: var(--cp-mono); letter-spacing:.04em; text-transform: uppercase;
  color: var(--cp-accent); border:1px dashed var(--cp-accent-dim); border-radius:5px; padding: 4px 9px;
  background: rgba(232,178,74,.05);
}}
.cp-note {{ color: var(--cp-dim); font-size: 12px; line-height:1.55; margin: 11px 0 0; }}
.cp-note b {{ color: var(--cp-text); }}
.cp-foot {{ margin-top: 11px; padding-top: 10px; border-top:1px dashed var(--cp-line); color: var(--cp-muted); font-size: 11.5px; line-height:1.5; }}
.cp-foot b {{ color: var(--cp-dim); }}

/* NB: the per-factor micro-bar (.cp-fbar) was removed rather than kept as dead
   CSS. At real IC magnitudes (~0.03) it rendered ~5px wide at 40% opacity, so all
   that showed was its centre tick — a headerless column of stray dashes. Direction
   is already carried by the green/red IC value and significance by the YES/NO
   badge, so the column encoded nothing the row didn't already say. */

/* strip default chrome (keep sidebar collapse + page nav we render ourselves) */
#MainMenu, [data-testid="stToolbar"], [data-testid="stDecoration"], footer {{ display: none; }}
</style>
"""

_RATING_CLASS = {"Strong Buy": "sb", "Buy": "b", "Hold": "h", "Sell": "s", "Strong Sell": "s"}


def _us_market_open(now: datetime | None = None) -> bool:
    """Rough US regular-session check (weekday, ~13:30–20:00 UTC ≈ 9:30–16:00 ET).
    Ignores holidays and DST edges — it drives a status dot, not a trade."""
    now = now or datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 13 * 60 + 30 <= minutes < 20 * 60


def top_bar(email: str | None = None, role: str | None = None) -> None:
    """The fixed status bar. Rendered by `_auth.gate()` rather than by apply(),
    because only gate() knows who's signed in — and since the bar is
    position:fixed, where it sits in the DOM makes no difference to where it
    paints. That's what lets the identity live in the header, as in the mockup,
    instead of tucked at the bottom of the sidebar."""
    now = datetime.now(timezone.utc)
    is_open = _us_market_open(now)
    state = "MKT OPEN" if is_open else "MKT CLOSED"
    dot = "open" if is_open else "closed"
    stamp = now.strftime("%d %b · %H:%M UTC")

    identity = ""
    if email or role:
        who = email or "Guest"
        initial = (who[:1] or "?").upper()
        suffix = f' <span class="role">· {role}</span>' if role else ""
        identity = (f'<span class="cp-id"><span class="av">{initial}</span>'
                    f"<span class=\"who\">{who}</span>{suffix}</span>")

    st.markdown(
        f'<div class="cp-topbar">'
        f'<span class="brand"><span class="glyph">◈</span> Investment Co-Pilot <small>terminal</small></span>'
        f'<span class="cp-chip"><span class="dot {dot}"></span> {state}</span>'
        f'<span class="cp-chip">{stamp}</span>'
        f'<span class="spacer"></span>'
        f'<span class="cp-pill-advice">Not advice</span>'
        f"{identity}"
        f"</div>",
        unsafe_allow_html=True,
    )


def _sidebar_nav() -> None:
    with st.sidebar:
        st.markdown('<div class="cp-sb-brand"><span class="glyph">◈</span> Co-Pilot</div>',
                    unsafe_allow_html=True)
        for section, items in _NAV:
            st.markdown(f'<div class="cp-eyebrow cp-navsec">{section}</div>', unsafe_allow_html=True)
            for path, label in items:
                try:
                    st.page_link(path, label=label)
                except Exception:
                    # A path that can't resolve shouldn't blank the whole sidebar.
                    st.markdown(f'<div style="padding:5px 9px;color:var(--cp-muted)">{label}</div>',
                                unsafe_allow_html=True)


def apply() -> None:
    """Inject the terminal stylesheet and render the sidebar nav. Call once per
    page, right after set_page_config(). The top bar is rendered separately by
    `_auth.gate()`, which is where the signed-in identity becomes known."""
    st.markdown(_CSS, unsafe_allow_html=True)
    _sidebar_nav()


def page_header(title: str, eyebrow: str | None = None, sub: str | None = None) -> None:
    """A terminal-style page header: small uppercase kicker, tight title, optional
    monospace sub-line. Replaces the giant emoji st.title."""
    parts = ['<div class="cp-head">']
    if eyebrow:
        parts.append(f'<div class="cp-eyebrow">{eyebrow}</div>')
    parts.append(f"<h1>{title}</h1>")
    if sub:
        parts.append(f'<div class="sub">{sub}</div>')
    parts.append("</div>")
    st.markdown("".join(parts), unsafe_allow_html=True)


def advice(html: str) -> None:
    """A slim, quiet 'not advice' bar — the terminal alternative to a fat st.info."""
    st.markdown(f'<div class="cp-advice">⚠&nbsp; {html}</div>', unsafe_allow_html=True)


def eyebrow(text: str) -> None:
    st.markdown(f'<div class="cp-eyebrow">{text}</div>', unsafe_allow_html=True)


def panel(title: str, body_html: str, tag: str | None = None, extra_class: str = "") -> None:
    """A titled body section — the mockup's panel. `body_html` is rendered as-is,
    so callers build tables/meters with the .cp-* classes above."""
    tag_html = f'<span class="tag">{tag}</span>' if tag else ""
    st.markdown(
        f'<div class="cp-panel {extra_class}">'
        f'<div class="ph"><span class="cp-eyebrow">{title}</span>{tag_html}</div>'
        f"{body_html}</div>",
        unsafe_allow_html=True,
    )


def badge_html(label: str, kind: str | None = None) -> str:
    cls = kind or _RATING_CLASS.get(label, "h")
    return f'<span class="cp-badge {cls}">{label}</span>'
