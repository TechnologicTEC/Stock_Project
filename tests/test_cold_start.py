"""Every page must render on an empty database, with no network.

The new-user path, and the one least likely to be exercised by hand once a
developer's own database is full of data. Both of the bugs that took a page
down this month were a value that is None or absent only before there is
history — a two-snapshot equity curve, a holdings table with no fills — so the
empty database is exactly where that class of bug lives.

Sockets are blocked rather than mocked: a page that slips past the cache layer
and reaches for the network fails here instead of silently depending on the
internet (and on someone's API quota) to pass.
"""
import pathlib
import socket

import pytest
from streamlit.testing.v1 import AppTest

_APP = pathlib.Path(__file__).resolve().parent.parent / "app"
_PAGES = sorted((_APP / "pages").glob("*.py"))
_ALL = [_APP / "main.py"] + _PAGES


@pytest.mark.parametrize("page", _ALL, ids=lambda p: p.stem)
def test_page_renders_on_an_empty_database(page, monkeypatch):
    def _no_network(*_args, **_kwargs):
        raise OSError(f"{page.name} tried to reach the network on a cold start")

    monkeypatch.setattr(socket, "socket", _no_network)
    monkeypatch.setattr(socket, "create_connection", _no_network)

    at = AppTest.from_file(str(page), default_timeout=90)
    at.run()
    assert not at.exception, f"{page.name} raised: {[e.value for e in at.exception]}"
