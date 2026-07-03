"""
Exercises app/pages/8_chat.py via AppTest. engine/chat.py is mocked (its routing
is covered in test_chat.py), so this only checks the chat UI wiring.
"""
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from engine import chat

PAGE_PATH = str(Path(__file__).resolve().parent.parent / "app" / "pages" / "8_chat.py")


def test_chat_page_renders_prompts():
    at = AppTest.from_file(PAGE_PATH)
    at.run(timeout=30)
    assert not at.exception
    assert any("Things you can ask" in e.label for e in at.expander)


def test_chat_page_answers_a_question():
    reply = chat.ChatResponse("Your portfolio is worth **$1,234.00**.", "portfolio_value")
    with patch("engine.chat.answer", return_value=reply) as mock_answer:
        at = AppTest.from_file(PAGE_PATH)
        at.run(timeout=30)
        at.chat_input[0].set_value("what's my portfolio worth?")
        at.run(timeout=30)

    assert not at.exception
    assert mock_answer.call_args.args[0] == "what's my portfolio worth?"
    assert any("Your portfolio is worth" in str(m.value) for m in at.markdown)


def test_chat_page_survives_a_tool_error():
    with patch("engine.chat.answer", side_effect=RuntimeError("boom")):
        at = AppTest.from_file(PAGE_PATH)
        at.run(timeout=30)
        at.chat_input[0].set_value("hi")
        at.run(timeout=30)

    assert not at.exception
    assert any("hit a problem" in str(m.value) for m in at.markdown)
