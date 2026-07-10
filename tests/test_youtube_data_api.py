"""
engine/data_sources/youtube_data_api.py — the official Data API client. Network
is mocked; the memoized uploads-playlist lookup is cleared around each test.
"""
from unittest.mock import Mock, patch

import pytest

from engine.data_sources import youtube_data_api as api

_CID = "UC0BGhWsIbV7Dm-lsvhdlMbA"


@pytest.fixture(autouse=True)
def _key_and_fresh_memo(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "yt_test")
    api.refresh()
    yield
    api.refresh()


def _resp(status, body=None, text=""):
    return Mock(status_code=status, json=Mock(return_value=body or {}), text=text)


def test_is_configured_follows_the_key(monkeypatch):
    assert api.is_configured() is True
    monkeypatch.delenv("YOUTUBE_API_KEY")
    assert api.is_configured() is False


def test_list_uploads_resolves_playlist_then_lists_items():
    channels = _resp(200, {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UUxyz"}}}]})
    items = _resp(200, {"items": [
        {"snippet": {"title": "5 Stocks"}, "contentDetails": {"videoId": "AAA", "videoPublishedAt": "2026-07-08T12:00:00Z"}},
        {"snippet": {"title": "Older", "resourceId": {"videoId": "BBB"}, "publishedAt": "2026-07-01T09:00:00Z"},
         "contentDetails": {}},
    ]})
    with patch("engine.data_sources.youtube_data_api.requests.get", side_effect=[channels, items]) as get:
        videos = api.list_uploads(_CID, limit=5)

    assert [v["video_id"] for v in videos] == ["AAA", "BBB"]
    assert videos[0]["title"] == "5 Stocks" and videos[0]["url"].endswith("v=AAA")
    assert videos[0]["published_at"].year == 2026 and videos[0]["published_at"].tzinfo is None  # naive UTC
    assert get.call_args_list[1].kwargs["params"]["playlistId"] == "UUxyz"
    assert get.call_args_list[1].kwargs["params"]["key"] == "yt_test"


def test_uploads_playlist_is_memoized_across_calls():
    channels = _resp(200, {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UUxyz"}}}]})
    items = _resp(200, {"items": []})
    with patch("engine.data_sources.youtube_data_api.requests.get",
               side_effect=[channels, items, items]) as get:
        api.list_uploads(_CID)
        api.list_uploads(_CID)
    assert get.call_count == 3          # channels once, playlistItems twice


def test_resolve_channel_by_handle_uses_forHandle():
    body = {"items": [{"id": _CID, "snippet": {"title": "ZipTrader", "customUrl": "@ziptrader"}}]}
    with patch("engine.data_sources.youtube_data_api.requests.get", return_value=_resp(200, body)) as get:
        info = api.resolve_channel("https://www.youtube.com/@ZipTrader")
    assert info["channel_id"] == _CID and info["display_name"] == "ZipTrader"
    assert get.call_args.kwargs["params"]["forHandle"] == "@ZipTrader"


def test_resolve_channel_by_id_uses_id_param():
    body = {"items": [{"id": _CID, "snippet": {"title": "ZipTrader"}}]}
    with patch("engine.data_sources.youtube_data_api.requests.get", return_value=_resp(200, body)) as get:
        info = api.resolve_channel(_CID)
    assert info["channel_id"] == _CID
    assert get.call_args.kwargs["params"]["id"] == _CID


def test_resolve_channel_raises_value_error_when_no_such_channel():
    with patch("engine.data_sources.youtube_data_api.requests.get", return_value=_resp(200, {"items": []})):
        with pytest.raises(ValueError, match="No YouTube channel found"):
            api.resolve_channel("@nobody")


def test_http_error_raises_youtube_api_error():
    body = {"error": {"message": "quotaExceeded"}}
    with patch("engine.data_sources.youtube_data_api.requests.get", return_value=_resp(403, body)):
        with pytest.raises(api.YouTubeApiError, match="quotaExceeded"):
            api.resolve_channel(_CID)
