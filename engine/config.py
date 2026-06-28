"""
Loads .env once, on first import, from the project root — so every module
that reads an API key from os.environ just works, regardless of which
script or page imported it first.

Usage elsewhere: `from engine import config  # noqa: F401`
"""
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")
