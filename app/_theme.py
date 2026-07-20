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
GROUND, PANEL, PANEL_2 = "#0a0e14", "#121924", "#0d131c"
LINE, LINE_SOFT = "#202b39", "#18212c"
TEXT, TEXT_DIM, MUTED = "#d1dbe7", "#93a1b1", "#5f6d7c"
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
  padding-top: 1.1rem; padding-bottom: 4rem; max-width: 1240px;
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

/* ---------- top status bar ---------- */
.cp-topbar {{
  display: flex; align-items: center; gap: 14px;
  margin: 0 0 18px; padding: 9px 14px;
  border: 1px solid var(--cp-line); border-radius: 8px;
  background: linear-gradient(180deg, #0e141d, #0b1017);
  font-size: 12px;
}}
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


def _top_bar() -> None:
    now = datetime.now(timezone.utc)
    is_open = _us_market_open(now)
    state = "MKT OPEN" if is_open else "MKT CLOSED"
    dot = "open" if is_open else "closed"
    stamp = now.strftime("%d %b · %H:%M UTC")
    st.markdown(
        f'<div class="cp-topbar">'
        f'<span class="brand"><span class="glyph">◈</span> Investment Co-Pilot <small>terminal</small></span>'
        f'<span class="cp-chip"><span class="dot {dot}"></span> {state}</span>'
        f'<span class="cp-chip">{stamp}</span>'
        f'<span class="spacer"></span>'
        f'<span class="cp-pill-advice">Not advice</span>'
        f'</div>',
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
    """Inject the terminal stylesheet and render the chrome (top bar + nav).
    Call once per page, right after set_page_config()."""
    st.markdown(_CSS, unsafe_allow_html=True)
    _top_bar()
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


def badge_html(label: str, kind: str | None = None) -> str:
    cls = kind or _RATING_CLASS.get(label, "h")
    return f'<span class="cp-badge {cls}">{label}</span>'
