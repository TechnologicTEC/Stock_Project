"""
engine/creator_digest.py — subject/HTML rendering and the "only email when
there's something new" rule. mailer is mocked; no email leaves the process.
"""
from unittest.mock import patch

from engine import creator_digest


def _video(mentions=None, title="5 Stocks", creator="ZipTrader"):
    default = [{"ticker": "NVDA", "company_name": "NVIDIA", "stance": "bullish",
                "screener_score": 80.0, "recommendation": "Buy", "confidence": 0.9}]
    return {"video_id": "AAA", "title": title, "url": "https://youtu.be/AAA", "creator": creator,
            "published_at": None, "mentions": default if mentions is None else mentions}


def test_subject_for_single_and_multiple_videos():
    assert creator_digest.build_subject([_video()]) == "ZipTrader: 1 new stock mention"
    assert creator_digest.build_subject([_video(), _video()]) == "Creator Signals: 2 new videos, 2 stock mentions"


def test_html_has_ticker_score_link_and_disclaimer():
    html = creator_digest.build_html([_video()])
    assert "NVDA" in html and "80/100" in html and "Buy" in html
    assert "https://youtu.be/AAA" in html and "5 Stocks" in html
    assert "endorsement" in html and "not financial advice" in html   # "not" is bolded inside the sentence


def test_no_email_when_nothing_to_report_or_email_unconfigured():
    with patch("engine.creator_digest.mailer.is_configured", return_value=True), \
         patch("engine.creator_digest.mailer.send") as send:
        assert creator_digest.send_digest([_video(mentions=[])]) is False   # no mentions
    send.assert_not_called()

    with patch("engine.creator_digest.mailer.is_configured", return_value=False), \
         patch("engine.creator_digest.mailer.send") as send:
        assert creator_digest.send_digest([_video()]) is False              # email not set up
    send.assert_not_called()


def test_sends_when_configured_and_there_are_mentions():
    with patch("engine.creator_digest.mailer.is_configured", return_value=True), \
         patch("engine.creator_digest.mailer.send", return_value=True) as send:
        assert creator_digest.send_digest([_video()]) is True
    subject, html = send.call_args.args
    assert "ZipTrader" in subject and "NVDA" in html


def test_send_failure_is_swallowed_so_the_scan_survives():
    with patch("engine.creator_digest.mailer.is_configured", return_value=True), \
         patch("engine.creator_digest.mailer.send", side_effect=RuntimeError("smtp down")):
        assert creator_digest.send_digest([_video()]) is False
