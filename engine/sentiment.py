"""
Sentiment scoring (Section 6.2) — a single, stable interface the rest of the
app depends on, so the model behind it is an implementation detail.

    score_text(text) -> float in [-1.0, +1.0]
        -1 = strongly negative, 0 = neutral, +1 = strongly positive

The model is **FinBERT** (`ProsusAI/finbert`), a finance-tuned classifier that
outputs three probabilities — positive / negative / neutral — which we collapse
to a single scalar as P(positive) − P(negative). Two deliberate choices:

1. `import torch`/`transformers` happens *inside* `_pipeline()`, not at module
   top, and the pipeline is built once, lazily, on first real use. So importing
   this module is cheap, and callers/tests that never actually score text never
   pay the ~2 GB model-load cost. Tests patch `score_text` directly and stay
   torch-free (the News/Earnings logic is what they're testing, not FinBERT).

2. Callers only ever see a plain float in [-1, 1] — never FinBERT's raw output
   shape — so swapping the model variant later (or dropping to VADER/quantized
   FinBERT for a smaller host) is a change confined to this one file.
"""
from __future__ import annotations

from functools import lru_cache

_MODEL = "ProsusAI/finbert"


@lru_cache(maxsize=1)
def _pipeline():
    """Build the FinBERT classifier once. torch/transformers are imported here
    (not at module import) so this module stays lightweight until first use."""
    from transformers import pipeline  # heavy import, deferred on purpose

    return pipeline("text-classification", model=_MODEL, top_k=None)


def score_text(text: str) -> float:
    """Sentiment of a headline/snippet, in [-1, 1] (P(pos) − P(neg)). Empty or
    whitespace-only text scores 0.0 (neutral) without loading the model."""
    if not text or not text.strip():
        return 0.0
    raw = _pipeline()(text[:512])  # FinBERT's max sequence is 512 tokens
    # With top_k=None, transformers returns either [[{...}, ...]] (4.x) or
    # [{...}, ...] (5.x) for a single input — normalize both to a flat list.
    results = raw[0] if raw and isinstance(raw[0], list) else raw
    scores = {r["label"].lower(): r["score"] for r in results}
    return round(scores.get("positive", 0.0) - scores.get("negative", 0.0), 4)


def is_available() -> bool:
    """True if the sentiment model can actually be loaded (transformers + torch
    installed and the model reachable). Lets callers degrade gracefully —
    showing headlines without scores — instead of erroring when the optional
    heavyweight deps aren't present."""
    try:
        _pipeline()
        return True
    except Exception:
        return False
