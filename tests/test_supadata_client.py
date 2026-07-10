"""
engine/data_sources/supadata_client.py — the hosted transcript API. Network is
mocked; covers each documented status code (200 / 202+job / 206 / 429 / 5xx).
"""
from unittest.mock import Mock, patch

import pytest

from engine.data_sources import supadata_client as sd


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("SUPADATA_API_KEY", "sd_test")
    monkeypatch.delenv("SUPADATA_MODE", raising=False)


def _resp(status, body=None, text=""):
    return Mock(status_code=status, json=Mock(return_value=body or {}), text=text)


def test_is_configured_follows_the_key(monkeypatch):
    assert sd.is_configured() is True
    monkeypatch.delenv("SUPADATA_API_KEY")
    assert sd.is_configured() is False


def test_mode_defaults_to_native_when_env_var_is_empty(monkeypatch):
    # GitHub Actions injects "" for an undefined var — get(..., default) would
    # return "" and we'd send `mode=`, which Supadata rejects with a 400.
    monkeypatch.setenv("SUPADATA_MODE", "")
    assert sd.mode() == "native"
    monkeypatch.setenv("SUPADATA_MODE", "   ")
    assert sd.mode() == "native"


def test_mode_honours_valid_values_and_rejects_junk(monkeypatch):
    monkeypatch.setenv("SUPADATA_MODE", "AUTO")
    assert sd.mode() == "auto"
    monkeypatch.setenv("SUPADATA_MODE", "nonsense")
    assert sd.mode() == "native"


def test_max_wait_survives_an_empty_env_var(monkeypatch):
    monkeypatch.setenv("SUPADATA_MAX_WAIT", "")
    assert sd._max_wait() == 180.0
    monkeypatch.setenv("SUPADATA_MAX_WAIT", "45")
    assert sd._max_wait() == 45.0


def test_returns_plain_text_and_uses_native_mode_by_default():
    with patch("engine.data_sources.supadata_client.requests.get",
               return_value=_resp(200, {"content": "hello world", "lang": "en"})) as get:
        assert sd.get_transcript_text("abc123") == "hello world"

    params = get.call_args.kwargs["params"]
    assert params["text"] == "true" and params["mode"] == "native"      # 1 credit, no AI billing
    assert params["url"].endswith("watch?v=abc123")
    assert get.call_args.kwargs["headers"]["x-api-key"] == "sd_test"


def test_long_video_polls_the_async_job_until_completed():
    responses = [
        _resp(202, {"jobId": "job-1"}),          # >20 min video -> async
        _resp(200, {"status": "active"}),
        _resp(200, {"status": "completed", "content": "the transcript", "lang": "en"}),
    ]
    with patch("engine.data_sources.supadata_client.requests.get", side_effect=responses) as get, \
         patch("engine.data_sources.supadata_client.time.sleep"):
        assert sd.get_transcript_text("abc123") == "the transcript"
    assert get.call_count == 3
    assert get.call_args.args[0].endswith("/job-1")


def test_failed_job_raises():
    responses = [
        _resp(202, {"jobId": "job-1"}),
        _resp(200, {"status": "failed", "error": {"message": "bad video"}}),
    ]
    with patch("engine.data_sources.supadata_client.requests.get", side_effect=responses), \
         patch("engine.data_sources.supadata_client.time.sleep"):
        with pytest.raises(sd.SupadataError, match="bad video"):
            sd.get_transcript_text("abc123")


def test_206_is_transcript_unavailable():
    with patch("engine.data_sources.supadata_client.requests.get", return_value=_resp(206)):
        with pytest.raises(sd.TranscriptUnavailable):
            sd.get_transcript_text("abc123")


def test_429_is_quota_exceeded():
    with patch("engine.data_sources.supadata_client.requests.get", return_value=_resp(429)):
        with pytest.raises(sd.QuotaExceeded):
            sd.get_transcript_text("abc123")


def test_other_http_errors_raise_supadata_error():
    body = {"error": "internal-error", "message": "boom"}
    with patch("engine.data_sources.supadata_client.requests.get", return_value=_resp(500, body)):
        with pytest.raises(sd.SupadataError, match="boom"):
            sd.get_transcript_text("abc123")


def test_missing_key_raises_before_any_request(monkeypatch):
    monkeypatch.delenv("SUPADATA_API_KEY")
    with patch("engine.data_sources.supadata_client.requests.get") as get:
        with pytest.raises(sd.SupadataError, match="not set"):
            sd.get_transcript_text("abc123")
    get.assert_not_called()
