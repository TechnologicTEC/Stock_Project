"""
Renders + sends the Creator Signals digest email (docs/creator-signals-plan.md).

Only fires when a scan actually extracted **new** stock mentions, so it can't be
spammy: no new video (or a video with no stocks in it) means no email. Carries
the same honesty framing as the page — a mention is not an endorsement.
"""
from __future__ import annotations

from engine import mailer

_STANCE = {"bullish": "🟢 Bullish", "bearish": "🔴 Bearish", "neutral": "⚪ Neutral", "unknown": "—"}
_DISCLAIMER = (
    "A mention is <strong>not</strong> an endorsement — the creator may be bearish about a stock — and the "
    "screener score is an explainable, educational signal, <strong>not financial advice</strong>."
)
_TD = 'style="padding:6px 10px;border-bottom:1px solid #eee"'
_TH = 'style="padding:6px 10px;text-align:left;border-bottom:2px solid #ddd;font-size:12px;color:#666"'


def build_subject(videos: list[dict]) -> str:
    total = sum(len(v["mentions"]) for v in videos)
    plural = "s" if total != 1 else ""
    if len(videos) == 1:
        return f"{videos[0]['creator']}: {total} new stock mention{plural}"
    return f"Creator Signals: {len(videos)} new videos, {total} stock mention{plural}"


def _row(m: dict) -> str:
    score = f"{m['screener_score']:.0f}/100" if m.get("screener_score") is not None else "—"
    return (
        f"<tr><td {_TD}><strong>{m['ticker']}</strong></td>"
        f"<td {_TD}>{m.get('company_name') or '—'}</td>"
        f"<td {_TD}>{_STANCE.get(m.get('stance'), '—')}</td>"
        f"<td {_TD}>{score}</td>"
        f"<td {_TD}>{m.get('recommendation') or '—'}</td></tr>"
    )


def build_html(videos: list[dict]) -> str:
    parts = ['<div style="font-family:system-ui,Segoe UI,Arial,sans-serif;max-width:640px">',
             '<h2 style="margin-bottom:4px">Creator Signals</h2>']
    for v in videos:
        parts.append(f'<h3 style="margin-bottom:2px"><a href="{v["url"]}">{v["title"]}</a></h3>')
        parts.append(f'<p style="margin-top:0;color:#666;font-size:13px">{v["creator"]}</p>')
        parts.append('<table style="border-collapse:collapse;width:100%"><tr>'
                     f'<th {_TH}>Ticker</th><th {_TH}>Company</th><th {_TH}>Creator\'s take</th>'
                     f'<th {_TH}>Screener</th><th {_TH}>Rating</th></tr>')
        parts.extend(_row(m) for m in v["mentions"])
        parts.append("</table>")
    parts.append(f'<p style="margin-top:24px;font-size:12px;color:#777">{_DISCLAIMER}</p></div>')
    return "".join(parts)


def send_digest(videos: list[dict]) -> bool:
    """Email the digest for `videos` (those with >=1 mention). Returns False —
    never raises — when there's nothing to report, email isn't configured, or the
    send fails; an email problem must never break the scan."""
    videos = [v for v in videos if v.get("mentions")]
    if not videos or not mailer.is_configured():
        return False
    try:
        return mailer.send(build_subject(videos), build_html(videos))
    except Exception as exc:
        print(f"  digest email FAILED: {type(exc).__name__}: {exc}", flush=True)
        return False
