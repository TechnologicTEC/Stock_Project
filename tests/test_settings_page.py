"""
app/pages/9_settings.py via AppTest: the key statuses say where each key comes from.
"""
from pathlib import Path

from streamlit.testing.v1 import AppTest

PAGE_PATH = str(Path(__file__).resolve().parent.parent / "app" / "pages" / "9_settings.py")


def _labels(at):
    return {t.label for t in at.text_input}


def test_the_owner_sees_keys_from_the_app_settings_as_set(monkeypatch):
    """The owner's keys come from .env / the Space's secrets, not this form; the
    page called them "not set"."""
    monkeypatch.setenv("FINNHUB_API_KEY", "from-env")
    at = AppTest.from_file(PAGE_PATH)
    at.run(timeout=30)
    assert not at.exception
    assert any(l.startswith("Finnhub API key") and "from the app's own settings" in l for l in _labels(at))


def test_a_friend_never_sees_the_owners_keys_as_theirs(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "from-env")
    monkeypatch.setenv("DEV_LOGIN_EMAIL", "friend@x.com")
    monkeypatch.setenv("FRIEND_EMAILS", "friend@x.com")
    at = AppTest.from_file(PAGE_PATH)
    at.run(timeout=30)
    assert not at.exception
    assert any(l.startswith("Finnhub API key") and "not set" in l for l in _labels(at))
