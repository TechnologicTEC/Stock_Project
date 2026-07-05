"""
Guards the app entry point (app/main.py) — the file `streamlit run` and the HF
Space actually launch. It's not under app/pages/, so the page tests don't cover
it; a missing import here (e.g. `gate`) crashes the whole home page on startup,
which is exactly the kind of break this catches.
"""
from pathlib import Path

from streamlit.testing.v1 import AppTest

_MAIN = Path(__file__).resolve().parent.parent / "app" / "main.py"


def test_main_page_boots_without_exception():
    at = AppTest.from_file(str(_MAIN))
    at.run(timeout=30)
    assert not at.exception, at.exception
    assert any("Investment Co-Pilot" in t.value for t in at.title)
