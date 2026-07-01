from unittest.mock import patch

from engine import sentiment


def _fake_finbert(pos, neg, neu):
    """A stand-in for the FinBERT pipeline: called with text, returns the
    transformers 'top_k' shape — a list holding one list of label/score dicts."""
    return lambda text: [[
        {"label": "positive", "score": pos},
        {"label": "negative", "score": neg},
        {"label": "neutral", "score": neu},
    ]]


def test_score_text_empty_or_blank_is_neutral_without_loading_model():
    # no _pipeline patch — proves empty text never touches the model
    assert sentiment.score_text("") == 0.0
    assert sentiment.score_text("   ") == 0.0
    assert sentiment.score_text(None) == 0.0


def test_score_text_collapses_finbert_probabilities_to_pos_minus_neg():
    with patch("engine.sentiment._pipeline", return_value=_fake_finbert(0.7, 0.1, 0.2)):
        assert sentiment.score_text("Company beats earnings") == 0.6  # 0.7 - 0.1


def test_score_text_negative_headline_scores_below_zero():
    with patch("engine.sentiment._pipeline", return_value=_fake_finbert(0.05, 0.85, 0.10)):
        assert sentiment.score_text("Shares plunge on fraud probe") == -0.8


def test_is_available_true_when_pipeline_builds():
    with patch("engine.sentiment._pipeline", return_value=_fake_finbert(0.3, 0.3, 0.4)):
        assert sentiment.is_available() is True


def test_is_available_false_when_deps_missing():
    with patch("engine.sentiment._pipeline", side_effect=RuntimeError("No module named 'torch'")):
        assert sentiment.is_available() is False
