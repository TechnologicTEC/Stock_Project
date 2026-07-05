#!/bin/sh
set -e

# Materialize Streamlit's [auth] secrets.toml from the AUTH_* env vars BEFORE
# Streamlit starts, so Google OIDC is configured at server boot. (An in-app shim
# runs too late — Streamlit reads auth config when the server starts, not per
# script run.) This reuses the exact same logic app/_auth.py uses; it's a no-op
# when the AUTH_* vars aren't set.
python -c "from app._auth import _ensure_auth_secrets; _ensure_auth_secrets()"

exec streamlit run app/main.py \
    --server.port=8501 \
    --server.address=0.0.0.0 \
    --server.headless=true
