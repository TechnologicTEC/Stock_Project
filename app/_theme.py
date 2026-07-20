"""
"Honest terminal" visual system — applied on every page.

The look is set in two places: `.streamlit/config.toml` carries the base tokens
(dark ground, amber primary) that Streamlit understands natively, and this module
layers everything Streamlit's theme can't express — monospace data, panel-style
metric cards, a proper sidebar nav, badges, and the chrome removal — as one CSS
injection plus a few small HTML render helpers.

Design language (see the signed-off mockup):
- Amber accent on a cool blue-ink ground. One bold hue; everything else quiet.
- All *data* (tickers, prices, scores) is monospace with tabular figures; prose
  stays in the UI sans. That split is the terminal identity.
- Uncertainty is rendered as FAINTNESS (dim, dashed), never a false-confidence
  colour — the app's "faint tilt, not a prediction" personality, made visual.

Call `apply()` once per page, right after st.set_page_config(). It's cheap and
idempotent; Streamlit reruns each page script, so each must re-inject.
"""
from __future__ import annotations

import streamlit as st

# Kept in sync with .streamlit/config.toml and the mockup.
GROUND = "#0a0e14"
PANEL = "#121924"
PANEL_2 = "#0d131c"
LINE = "#202b39"
LINE_SOFT = "#18212c"
TEXT = "#d1dbe7"
TEXT_DIM = "#93a1b1"
MUTED = "#5f6d7c"
ACCENT = "#e8b24a"
ACCENT_DIM = "#8a6f36"
UP = "#4bc16d"
DOWN = "#ef6147"

_MONO = 'ui-monospace, "SF Mono", "JetBrains Mono", "Cascadia Code", Menlo, Consolas, monospace'

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
[data-testid="stMainBlockContainer"], .block-container {{
  padding-top: 2.4rem; padding-bottom: 4rem; max-width: 1220px;
}}

/* ---------- top header bar ---------- */
[data-testid="stHeader"] {{
  background: linear-gradient(180deg, rgba(14,20,29,.92), rgba(10,14,20,.72));
  border-bottom: 1px solid var(--cp-line);
  backdrop-filter: blur(6px);
}}

/* ---------- typography ---------- */
.stApp, .stMarkdown, p, li, label, .stCaption {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}}
h1 {{ font-size: 1.55rem; letter-spacing: -.01em; font-weight: 700; text-wrap: balance; }}
h2 {{ font-size: 1.2rem;  letter-spacing: -.005em; font-weight: 650; }}
h3 {{ font-size: 1.02rem; font-weight: 650; color: var(--cp-text); }}
h1, h2, h3 {{ margin-bottom: .5rem; }}
a {{ text-decoration: none; }} a:hover {{ text-decoration: underline; }}

/* uppercase eyebrow label */
.cp-eyebrow {{
  font-size: 10.5px; letter-spacing: .16em; text-transform: uppercase;
  color: var(--cp-muted); font-weight: 700; margin-bottom: 2px;
}}

/* ---------- metric cards (the KPI tiles) ---------- */
[data-testid="stMetric"] {{
  background: linear-gradient(180deg, var(--cp-panel), var(--cp-panel-2));
  border: 1px solid var(--cp-line); border-radius: 8px;
  padding: 14px 16px 15px;
}}
[data-testid="stMetricLabel"] p {{
  font-size: 10.5px !important; letter-spacing: .13em; text-transform: uppercase;
  color: var(--cp-muted) !important; font-weight: 600;
}}
[data-testid="stMetricValue"] {{
  font-family: var(--cp-mono) !important; font-variant-numeric: tabular-nums;
  letter-spacing: -.02em; font-weight: 600;
}}
[data-testid="stMetricDelta"] {{ font-family: var(--cp-mono) !important; font-variant-numeric: tabular-nums; }}

/* ---------- sidebar + page nav ---------- */
[data-testid="stSidebar"] {{
  background: var(--cp-panel-2); border-right: 1px solid var(--cp-line);
}}
[data-testid="stSidebarNav"] {{ padding-top: .4rem; }}
[data-testid="stSidebarNav"] a {{
  border-radius: 6px; margin: 1px 6px; border: 1px solid transparent;
}}
[data-testid="stSidebarNav"] a span {{ color: var(--cp-dim); font-size: 13.5px; }}
[data-testid="stSidebarNav"] a:hover {{ background: rgba(255,255,255,.03); }}
[data-testid="stSidebarNav"] a[aria-current="page"] {{
  background: rgba(232,178,74,.10); border-color: rgba(232,178,74,.22);
}}
[data-testid="stSidebarNav"] a[aria-current="page"] span {{ color: var(--cp-text); font-weight: 600; }}

/* ---------- buttons ---------- */
.stButton > button, .stDownloadButton > button, .stFormSubmitButton > button {{
  border-radius: 6px; font-weight: 600; letter-spacing: .01em; border: 1px solid var(--cp-line);
}}
.stButton > button[kind="primary"], .stFormSubmitButton > button {{
  color: #17130a;  /* dark text on amber for contrast */
}}
.stButton > button:focus-visible {{ outline: 2px solid var(--cp-accent); outline-offset: 2px; }}

/* ---------- inputs / selects / tabs ---------- */
[data-baseweb="input"], [data-baseweb="select"], [data-baseweb="textarea"], .stNumberInput div[data-baseweb] {{
  border-radius: 6px;
}}
.stTabs [data-baseweb="tab-list"] {{ gap: 4px; border-bottom: 1px solid var(--cp-line); }}
.stTabs [data-baseweb="tab"] {{ font-weight: 600; }}
.stTabs [aria-selected="true"] {{ color: var(--cp-accent); }}

/* ---------- expanders as panels ---------- */
[data-testid="stExpander"] details {{
  background: linear-gradient(180deg, var(--cp-panel), var(--cp-panel-2));
  border: 1px solid var(--cp-line); border-radius: 8px;
}}
[data-testid="stExpander"] summary {{ font-weight: 600; }}
[data-testid="stExpander"] summary:hover {{ color: var(--cp-accent); }}

/* ---------- alerts (info / warning / etc) ---------- */
[data-testid="stAlert"] {{ border-radius: 7px; border: 1px solid var(--cp-line); }}

/* ---------- data tables: monospace, tabular, hairline ---------- */
[data-testid="stDataFrame"], [data-testid="stTable"] {{
  font-family: var(--cp-mono); font-variant-numeric: tabular-nums;
  border: 1px solid var(--cp-line); border-radius: 8px;
}}
[data-testid="stTable"] th {{
  text-transform: uppercase; letter-spacing: .08em; font-size: 10px;
  color: var(--cp-muted); font-weight: 600;
}}

/* ---------- dividers ---------- */
hr {{ border-color: var(--cp-line-soft); margin: 1.5rem 0; }}

/* ---------- badges (rating pills etc, via badge_html) ---------- */
.cp-badge {{
  font-family: var(--cp-mono); font-size: 11px; font-weight: 600; letter-spacing: .02em;
  padding: 2px 8px; border-radius: 4px; border: 1px solid transparent; white-space: nowrap;
}}
.cp-badge.sb {{ color:#8ff0aa; background: rgba(75,193,109,.13); border-color: rgba(75,193,109,.30); }}
.cp-badge.b  {{ color:#bfe8c9; background: rgba(75,193,109,.08); border-color: rgba(75,193,109,.18); }}
.cp-badge.h  {{ color: var(--cp-dim); background: rgba(147,161,177,.08); border-color: var(--cp-line); }}
.cp-badge.s  {{ color:#f3b3a5; background: rgba(239,97,71,.10); border-color: rgba(239,97,71,.24); }}
.cp-badge.faint {{ color: var(--cp-accent); background: rgba(232,178,74,.05); border: 1px dashed var(--cp-accent-dim); }}

/* ---------- strip default chrome (keep the sidebar nav + collapse control) ---------- */
#MainMenu, [data-testid="stToolbar"], [data-testid="stDecoration"], footer {{ display: none; }}
</style>
"""

_RATING_CLASS = {
    "Strong Buy": "sb", "Buy": "b", "Hold": "h", "Sell": "s", "Strong Sell": "s",
}


def apply() -> None:
    """Inject the terminal stylesheet. Call once per page after set_page_config."""
    st.markdown(_CSS, unsafe_allow_html=True)


def eyebrow(text: str) -> None:
    """A small uppercase kicker above a title — the terminal's section marker."""
    st.markdown(f'<div class="cp-eyebrow">{text}</div>', unsafe_allow_html=True)


def badge_html(label: str, kind: str | None = None) -> str:
    """Inline HTML for a rating/status pill. `kind` overrides the class; otherwise
    it's derived from a recommendation label (Strong Buy → green, Sell → red…)."""
    cls = kind or _RATING_CLASS.get(label, "h")
    return f'<span class="cp-badge {cls}">{label}</span>'
