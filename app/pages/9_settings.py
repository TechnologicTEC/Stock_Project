"""
Settings — per-user API keys (Phase C). Each signed-in user brings their own
keys (Finnhub, their own Alpaca paper account, Gemini, …); they're encrypted at
rest and only ever used for that user. Guests are blocked (they use the demo).
"""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st

from app import _theme
from app._auth import gate
from db.session import init_db
from engine import auth, credentials

st.set_page_config(page_title="Settings — Investment Co-Pilot", page_icon="🔑", layout="wide")
_theme.apply()
init_db()
identity = gate("settings")  # owner/friend only; guests are stopped here

_theme.page_header("Settings", eyebrow="Execution")
st.caption(
    "These keys are **yours** — stored encrypted and used only for your account. Nothing is shared with other "
    "users, and none of it is shown back after you save it. Leave a field blank to keep the current value. "
    "Each key unlocks the features noted; the app works without them, it just shows less."
)

stored = credentials.stored_key_names(identity.user_id)

with st.form("api_keys"):
    entries: dict[str, str] = {}
    for name, meta in credentials.MANAGED_KEYS.items():
        status = "✅ set" if name in stored else "— not set"
        entries[name] = st.text_input(
            f"{meta['label']}  ·  {status}",
            type="password" if meta["secret"] else "default",
            help=meta["help"],
            placeholder="leave blank to keep current",
            key=f"key_{name}",
        )
    saved = st.form_submit_button("Save keys", type="primary")

if saved:
    changed = [name for name, val in entries.items() if (val or "").strip()]
    for name in changed:
        credentials.save_user_key(identity.user_id, name, entries[name].strip())
    # Reload the freshly-saved keys into this session's credentials context.
    credentials.set_current_keys(
        credentials.load_user_keys(identity.user_id), fallback=auth.credentials_fallback(identity.role)
    )
    st.success(f"Saved {len(changed)} key(s)." if changed else "No changes — every field was blank.")
    st.rerun()

if stored:
    st.divider()
    st.subheader("Remove keys")
    to_remove = st.multiselect("Delete stored keys", sorted(stored))
    if st.button("Remove selected") and to_remove:
        for name in to_remove:
            credentials.delete_user_key(identity.user_id, name)
        st.rerun()
