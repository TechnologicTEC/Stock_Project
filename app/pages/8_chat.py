"""
AI Chat Assistant (Section 6.6). Streamlit only — the tool functions live in
engine/chat_tools.py and the intent routing in engine/chat.py. This is just the
chat UI (Streamlit's built-in st.chat_message / st.chat_input) and history.

It answers from your *own* cached data via a deterministic template responder —
zero cost, no API key. The blueprint's optional stage 2 (an LLM calling the same
tools) would slot into engine/chat.py without changing this page.
"""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st

from app._auth import gate
from db.session import init_db
from engine import chat, chat_llm

st.set_page_config(page_title="Assistant — Investment Co-Pilot", page_icon="📊", layout="wide")
init_db()
gate("chat")  # guest-accessible (Phase B) — sets the current user scope

st.title("Assistant")
if chat_llm.is_available():
    st.caption(
        "Personal, educational tool — not financial advice. **Powered by Gemini**, I answer questions about "
        "**your own portfolio** by calling tools that read the app's cached data — so I only report figures I "
        "can actually look up, never invented ones. (No key? I fall back to a deterministic responder.)"
    )
else:
    st.caption(
        "Personal, educational tool — not financial advice. I answer questions about **your own portfolio** "
        "from the app's cached data. I'm a **deterministic** assistant here (no LLM key set), so I stick to a "
        "set of questions and never invent numbers. Add a free `GEMINI_API_KEY` to `.env` for free-form chat."
    )

with st.expander("💡 Things you can ask"):
    st.markdown(
        "**Your portfolio**\n"
        "- *What's my portfolio worth?* · *How am I doing overall?*\n"
        "- *What's my biggest holding?* · *How much of my portfolio is in AAPL?*\n"
        "- *What are today's movers?* · *How much cash do I have?* · *What's on my watchlist?*\n\n"
        "**What's moving — and why**\n"
        "- *Why is my portfolio down today?* — the biggest movers **and the news around them**\n"
        "- *Any news on ASML?* · *Is the whole market down today?*\n\n"
        "**Deeper reads**\n"
        "- *How does the screener rate PLTR?* · *How did NVDA's last earnings go?*\n"
        "- *Am I beating the S&P this month?* · *What's my 1-year projected range?*\n"
        "- *How risky is my portfolio?* · *What's my biggest concentration risk?*"
    )
    if not chat_llm.is_available():
        st.caption("No `GEMINI_API_KEY` set — I answer everything above from the built-in responder except "
                   "*projections* and *beating-the-benchmark*; those two (and free-form wording) use Gemini.")

if "chat_history" not in st.session_state:
    st.session_state.chat_history = []

col1, col2 = st.columns([5, 1])
if col2.button("Clear", use_container_width=True):
    st.session_state.chat_history = []

# Replay the conversation so far.
for message in st.session_state.chat_history:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

prompt = st.chat_input("Ask about your portfolio…")
if prompt:
    with st.chat_message("user"):
        st.markdown(prompt)

    # Prior turns (before this prompt) give the LLM path conversational context.
    prior_history = list(st.session_state.chat_history)
    st.session_state.chat_history.append({"role": "user", "content": prompt})

    try:
        reply = chat.answer(prompt, history=prior_history).text
    except Exception as exc:  # a tool/data hiccup should never crash the chat
        reply = f"Sorry — I hit a problem reading your data: {exc}"

    with st.chat_message("assistant"):
        st.markdown(reply)
    st.session_state.chat_history.append({"role": "assistant", "content": reply})
