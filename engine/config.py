"""
Loads .env once, on first import, from the project root — so every module
that reads an API key from os.environ just works, regardless of which
script or page imported it first. Also quiets one noisy Streamlit logger
(see below). Imported early on every page via db.session, so both effects
apply process-wide no matter which entry point runs first.

Usage elsewhere: `from engine import config  # noqa: F401`
"""
import logging
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

# Streamlit's hot-reload file-watcher walks every imported module to decide
# what to watch. Once FinBERT is loaded, `transformers` is in memory, and the
# watcher pokes each of its hundreds of *vision* models — which lazily import
# `torchvision` (intentionally not installed; FinBERT is text-only). Every one
# raises ModuleNotFoundError, which the watcher logs as a WARNING traceback,
# flooding the console on every rerun. Silence just that one logger — hot
# reload, FinBERT, and every other warning are untouched. (Alternative if you
# prefer: `pip install torchvision`, but that's a large, otherwise-unused dep.)
logging.getLogger("streamlit.watcher.local_sources_watcher").setLevel(logging.ERROR)
