# Investment Co-Pilot — Hugging Face **Docker** Space.
#
# Why Docker and not the Streamlit SDK: Streamlit reads its Google-OIDC `[auth]`
# config from .streamlit/secrets.toml at *server startup*. HF's Streamlit SDK gives
# secrets only as env vars, and an in-app shim writes the file too late (after the
# server has already read auth config). Here, entrypoint.sh materializes that file
# from the AUTH_* env vars BEFORE launching Streamlit — so OIDC is configured in
# time. (The HF app dir is also read-only under the Streamlit SDK; here /app is
# writable because we own it.)
FROM python:3.11-slim

# libgomp1: the OpenMP runtime PyTorch's CPU wheel needs at import time.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 \
 && rm -rf /var/lib/apt/lists/*

# Run as a non-root user with a writable home (HF Docker convention).
RUN useradd -m -u 1000 appuser
ENV HOME=/home/appuser \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/home/appuser/.cache/huggingface \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

WORKDIR /app

# CPU-only torch first (skips the multi-GB CUDA wheels), then the rest incl.
# Streamlit (which is a normal dependency here — see requirements.txt).
COPY requirements.txt .
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir -r requirements.txt

COPY --chown=appuser:appuser . /app
USER appuser

EXPOSE 8501
CMD ["sh", "entrypoint.sh"]
