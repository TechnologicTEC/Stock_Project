"""
engine/chat_llm.py — the optional Gemini path. The google-genai client is mocked
(via _client), so these test the availability gate, the tool wrappers, request
construction, and the empty-response fallback trigger — no key, no network, and
no `google-genai` install required.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from engine import chat_llm


# --------------------------------------------------------------------------
# Availability gate
# --------------------------------------------------------------------------

def test_is_available_requires_key_and_sdk(monkeypatch):
    monkeypatch.delenv("CHAT_LLM_DISABLED", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert chat_llm.is_available() is False

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    with patch("engine.chat_llm._genai_installed", return_value=True):
        assert chat_llm.is_available() is True
    with patch("engine.chat_llm._genai_installed", return_value=False):
        assert chat_llm.is_available() is False        # key but no SDK
    with patch("engine.chat_llm._genai_installed", return_value=True):
        monkeypatch.setenv("CHAT_LLM_DISABLED", "1")
        assert chat_llm.is_available() is False          # explicit kill switch


# --------------------------------------------------------------------------
# Tool wrappers delegate to chat_tools (so mocks/data flow through)
# --------------------------------------------------------------------------

def test_tool_wrappers_delegate_to_chat_tools():
    with patch("engine.chat_llm.chat_tools.get_portfolio_value", return_value={"total_value": 1.0}) as f:
        assert chat_llm.get_portfolio_value() == {"total_value": 1.0}
    f.assert_called_once()

    with patch("engine.chat_llm.chat_tools.get_holding_weight", return_value={"ticker": "AAPL"}) as g:
        chat_llm.get_holding_weight("aapl")
    g.assert_called_once_with("aapl")


# --------------------------------------------------------------------------
# Request construction + response reading
# --------------------------------------------------------------------------

def test_answer_calls_gemini_with_tools_and_history():
    client = MagicMock()
    client.models.generate_content.return_value = SimpleNamespace(text="Your portfolio is worth $1,000.")
    history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]

    with patch("engine.chat_llm._client", return_value=client):
        text = chat_llm.answer("what's it worth?", history=history)

    assert "1,000" in text
    call = client.models.generate_content.call_args
    assert call.kwargs["model"] == chat_llm._model()

    config = call.kwargs["config"]
    assert config["system_instruction"]
    assert chat_llm.get_portfolio_value in config["tools"]       # the wrapper functions are the tools

    contents = call.kwargs["contents"]
    assert contents[0]["role"] == "user"                         # history threaded, starts on a user turn
    assert any(c["role"] == "model" for c in contents)           # assistant mapped to Gemini's 'model'
    assert contents[-1]["parts"][0]["text"] == "what's it worth?"


def test_answer_raises_on_empty_text():
    client = MagicMock()
    client.models.generate_content.return_value = SimpleNamespace(text="")
    with patch("engine.chat_llm._client", return_value=client):
        with pytest.raises(RuntimeError):
            chat_llm.answer("hi")


# --------------------------------------------------------------------------
# History shaping
# --------------------------------------------------------------------------

def test_history_contents_maps_roles_and_trims():
    history = [{"role": "assistant", "content": "a"}] + \
              [{"role": ("user" if i % 2 == 0 else "assistant"), "content": f"m{i}"} for i in range(20)]
    contents = chat_llm._history_contents(history)
    assert len(contents) <= chat_llm.MAX_HISTORY_MESSAGES
    assert contents[0]["role"] == "user"
    assert all(c["role"] in ("user", "model") for c in contents)
