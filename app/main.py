"""Streamlit entry point. Run with: streamlit run app/main.py"""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st

from db.session import init_db

st.set_page_config(page_title="Investment Co-Pilot", page_icon="📊", layout="wide")
init_db()

st.title("📊 Investment Co-Pilot")

st.info(
    "**This is a personal, educational tool — not financial advice.** "
    "It runs on free-tier data, which can be delayed, incomplete, or occasionally "
    "wrong. Don't make real trading decisions based solely on what it shows you.",
    icon="⚠️",
)

st.markdown(
    """
Use the pages in the sidebar to get around:

- **Portfolio** — your holdings, current valuation, and allocation
- **Screener** — explainable weighted-factor stock scoring
- **Health** — concentration, beta, Sharpe ratio, drawdown, and flags
- **News** — headline + earnings-release sentiment (FinBERT), per ticker
- *Backtest, Paper Trading, and Chat are coming in later phases.*

If you haven't added any holdings yet, head to the **Portfolio** page —
you can add them one at a time or import a CSV.
"""
)
