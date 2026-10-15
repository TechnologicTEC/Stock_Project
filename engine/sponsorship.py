"""
Which of a video's stocks were presented in a paid ("sponsored") segment.

ZipTrader closes most videos with "our sponsored segment on <company>, ticker
symbol <X>" — 40 of 54 scanned videos by Oct 2026 — and extraction recorded
those stocks as ordinary mentions, usually bullish: VNRX was sponsored three
times in six weeks, which alone would have cleared creator_conviction's bar.
Tane, 16 Oct 2026: sponsored segments don't count toward the queue. They are
kept and labelled (VideoMention.sponsored), and every mention count leaves them
out (creator_signals.mention_leaderboard / mention_history / ticker_stance).

Why ask the model as well as match the announcement: captions garble the very
ticker being announced — "G O" for GORO, "M OD" for MOD, "and cy" for ONCY,
"S N O A" for SNOA — and the company name comes through mangled too. The model
reads the segment in context; the text match catches the plain ones, and is
all there is when no model is configured.

A transcript with no sponsorship wording at all is never sent to the model.
"""
from __future__ import annotations

import json
import logging
import re

from engine import ticker_extraction

logger = logging.getLogger(__name__)

# Wording that can introduce paid content. Deliberately broad: a false hint
# ("a company that sponsors the home team") costs one model call that answers
# "none"; a missed one counts an advert as a view.
_HINT = re.compile(r"(?i)sponsor|paid promotion|paid partnership|compensated by|"
                   r"in partnership with|brought to you by")

_PROMPT = (
    "You are given the transcript of a stock-market YouTube video and a list of stocks "
    "found in it. Some videos contain a SPONSORED or paid segment, where the creator is "
    "paid to present a company (for example \"our sponsored segment on <company>, ticker "
    "symbol <X>\"). Captions often garble the ticker in that announcement — split into "
    "letters or cut short, e.g. \"G O\" for GORO, \"GO AI\" for GOAI, \"S N O A\" for SNOA — and "
    "the company names in the list may themselves be wrong or out of date, so match on the "
    "ticker's letters as well as on the company being presented. Return ONLY a JSON array of "
    "the tickers, from the list given, "
    "that are the subject of a sponsored or paid segment — not stocks merely discussed "
    "elsewhere in the video. If none, return []."
)

# How far after "sponsored segment" the text match looks. The announcement is
# one sentence — "...on <company>, ticker symbol <X> on the NASDAQ" — and on the
# Oct 2026 backfill every name or ticker it found sat inside 120 characters;
# reading further only reaches whatever the video says next.
_WINDOW = 120

# What the model is shown: the passages around each sponsorship hint, not the
# whole transcript. Given all 20-35k characters it missed 5 of 34 segments on
# the Oct 2026 backfill (AMSS, GOAI, BIOM, GORO, SNA) — the segment got lost.
_EXCERPT_BEFORE = 300
_EXCERPT_AFTER = 2500


def has_hint(transcript: str | None) -> bool:
    return bool(transcript and _HINT.search(transcript))


def _excerpts(transcript: str) -> str:
    """The passages around every sponsorship hint, merged where they overlap."""
    spans: list[tuple[int, int]] = []
    for hit in _HINT.finditer(transcript):
        lo, hi = max(0, hit.start() - _EXCERPT_BEFORE), hit.end() + _EXCERPT_AFTER
        if spans and lo <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(spans[-1][1], hi))
        else:
            spans.append((lo, hi))
    return "\n...\n".join(transcript[lo:hi] for lo, hi in spans)


def _ask_model(transcript: str, mentions) -> set[str]:
    from engine import chat_llm

    listing = "\n".join(f"{m.ticker}: {m.company_name or ''}" for m in mentions)
    # Hold the client in a name for the length of the call. Chained as
    # `_client().models...`, the client can be collected mid-request and closes
    # its connection: "Cannot send a request, as the client has been closed".
    client = chat_llm._client()
    resp = client.models.generate_content(
        model=chat_llm._model(),
        contents=[{"role": "user", "parts": [{"text": (
            f"{_PROMPT}\n\nSTOCKS:\n{listing}\n\nTRANSCRIPT EXCERPTS:\n{_excerpts(transcript)}")}]}],
        config={"response_mime_type": "application/json", "temperature": 0},
    )
    data = json.loads((getattr(resp, "text", None) or "[]").strip())
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), [])
    return {str(t).upper().strip() for t in (data if isinstance(data, list) else [])}


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _text_match(transcript: str, mentions) -> set[str]:
    """A mention whose ticker, as a whole word, or company name appears just
    after a 'sponsored segment' announcement. Names are compared with spaces
    squashed out, so 'gold group mining' still finds 'Goldgroup Mining'."""
    found = set()
    for hit in re.finditer(r"(?i)sponsored segment", transcript):
        raw = transcript[hit.end(): hit.end() + _WINDOW]
        # Tickers only as CAPITALS, and never one that is also a word: on the
        # Oct 2026 backfill "on the NASDAQ now" matched NOW (ServiceNow) in 4
        # videos. Captions write a spoken ticker in capitals (VNRX, AMSS).
        caps, squashed = set(re.findall(r"\b[A-Z0-9]{3,}\b", raw)), _squash(raw)
        for m in mentions:
            name = _squash(re.sub(r"(?i)\b(inc|corp|corporation|ltd|limited|holdings?|plc|co)\b\.?",
                                  "", m.company_name or ""))
            ticker = m.ticker.upper()
            if (len(name) >= 4 and name in squashed) or (
                    ticker in caps and ticker not in ticker_extraction._STOPWORDS):
                found.add(m.ticker)
    return found


def sponsored_tickers(transcript: str | None, mentions) -> set[str]:
    """The subset of `mentions`' tickers presented in a paid segment.

    Raises ticker_extraction.TransientExtractionError when the model failed for
    a retryable reason, so the caller defers the whole video rather than store
    its mentions unclassified — the same rule extraction follows.
    """
    mentions = list(mentions or ())
    if not mentions or not has_hint(transcript):
        return set()
    wanted = {m.ticker.upper() for m in mentions}
    # Either check is enough. The text match is exact when the announcement
    # names the ticker or company plainly (AMSS); the model reads the garbled
    # ones (GO AI, S N O A). Erring towards "sponsored" only ever leaves an
    # advert uncounted.
    found = _text_match(transcript, mentions)
    if ticker_extraction._llm_available():
        try:
            found |= _ask_model(transcript, mentions)
        except Exception as exc:
            if ticker_extraction._is_transient(exc):
                raise ticker_extraction.TransientExtractionError(str(exc)) from exc
            # A non-transient failure (bad SDK, bad JSON) leaves the text match to
            # it — loudly, because a model that quietly never runs looks exactly
            # like one that found nothing.
            logger.warning("Sponsorship check: model failed (%s: %s); using the "
                           "announcement text alone", type(exc).__name__, exc)
    return found & wanted
