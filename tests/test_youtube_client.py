"""
engine/data_sources/youtube_client.py — channel feed parsing (with retry) and
transcript status classification. Network + youtube-transcript-api are mocked.
"""
from unittest.mock import Mock, patch

import pytest

from engine.data_sources import youtube_client as yt

_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>yt:video:ABC123</id>
    <yt:videoId>ABC123</yt:videoId>
    <title>5 Stocks To Buy Heavy</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=ABC123"/>
    <published>2026-07-08T12:00:00+00:00</published>
  </entry>
  <entry>
    <id>yt:video:DEF456</id>
    <title>Is The Market Crashing?</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=DEF456"/>
    <published>2026-07-07T09:30:00+00:00</published>
  </entry>
</feed>"""


def _resp(status, body=b""):
    return Mock(status_code=status, content=body)


def test_latest_videos_parses_feed():
    with patch("engine.data_sources.youtube_client.requests.get", return_value=_resp(200, _FEED.encode())):
        vids = yt.latest_videos("UCxxxx")
    assert [v["video_id"] for v in vids] == ["ABC123", "DEF456"]
    assert vids[0]["title"] == "5 Stocks To Buy Heavy"
    assert vids[0]["url"].endswith("v=ABC123")
    assert vids[0]["published_at"].year == 2026 and vids[0]["published_at"].tzinfo is not None


def test_latest_videos_retries_then_succeeds():
    seq = [_resp(500), _resp(200, _FEED.encode())]
    with patch("engine.data_sources.youtube_client.requests.get", side_effect=seq) as g, \
         patch("engine.data_sources.youtube_client.time.sleep"):
        vids = yt.latest_videos("UCxxxx")
    assert g.call_count == 2 and len(vids) == 2


def test_latest_videos_raises_after_all_retries_fail():
    with patch("engine.data_sources.youtube_client.requests.get", return_value=_resp(404)), \
         patch("engine.data_sources.youtube_client.time.sleep"):
        with pytest.raises(RuntimeError, match="404"):
            yt.latest_videos("UCxxxx", retries=2)


def test_get_transcript_ok_and_empty():
    with patch("engine.data_sources.youtube_client._fetch_transcript_text", return_value="hello world"):
        assert yt.get_transcript("v") == ("ok", "hello world")
    with patch("engine.data_sources.youtube_client._fetch_transcript_text", return_value="   "):
        assert yt.get_transcript("v") == ("no_captions", None)


def test_get_transcript_classifies_errors():
    class TranscriptsDisabled(Exception):
        pass

    class IpBlocked(Exception):
        pass

    with patch("engine.data_sources.youtube_client._fetch_transcript_text", side_effect=TranscriptsDisabled()):
        assert yt.get_transcript("v") == ("no_captions", None)          # caption-absent → no_captions
    with patch("engine.data_sources.youtube_client._fetch_transcript_text", side_effect=IpBlocked()):
        assert yt.get_transcript("v") == ("blocked", None)              # name hints a block
    with patch("engine.data_sources.youtube_client._fetch_transcript_text",
               side_effect=RuntimeError("HTTP 429 too many requests")):
        assert yt.get_transcript("v") == ("blocked", None)             # message hints a block
    with patch("engine.data_sources.youtube_client._fetch_transcript_text", side_effect=ValueError("weird")):
        assert yt.get_transcript("v") == ("error", None)               # anything else → error


_CHANNEL_HTML = (
    '<html><head>'
    '<link rel="canonical" href="https://www.youtube.com/channel/UC0BGhWsIbV7Dm-lsvhdlMbA">'
    '<meta property="og:title" content="ZipTrader">'
    '</head><body>"canonicalBaseUrl":"/@ZipTrader"</body></html>'
)


_CHANNEL_FEED = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<feed xmlns="http://www.w3.org/2005/Atom">'
    '<title>ZipTrader</title>'
    '<author><name>ZipTrader</name>'
    '<uri>https://www.youtube.com/channel/UC0BGhWsIbV7Dm-lsvhdlMbA</uri></author>'
    "</feed>"
).encode()


def test_channel_info_reads_name_from_the_feed():
    with patch("engine.data_sources.youtube_client._fetch_feed", return_value=_CHANNEL_FEED):
        info = yt.channel_info("UC0BGhWsIbV7Dm-lsvhdlMbA")
    assert info == {"channel_id": "UC0BGhWsIbV7Dm-lsvhdlMbA", "display_name": "ZipTrader"}


def test_resolve_from_bare_channel_id_never_scrapes_the_html_page():
    with patch("engine.data_sources.youtube_client._fetch_feed", return_value=_CHANNEL_FEED), \
         patch("engine.data_sources.youtube_client.requests.get") as get:
        info = yt.resolve_channel("UC0BGhWsIbV7Dm-lsvhdlMbA")
    assert info["channel_id"] == "UC0BGhWsIbV7Dm-lsvhdlMbA" and info["display_name"] == "ZipTrader"
    get.assert_not_called()          # the bot-protected page is never touched


def test_resolve_from_channel_url_never_scrapes_the_html_page():
    with patch("engine.data_sources.youtube_client._fetch_feed", return_value=_CHANNEL_FEED), \
         patch("engine.data_sources.youtube_client.requests.get") as get:
        info = yt.resolve_channel("https://www.youtube.com/channel/UC0BGhWsIbV7Dm-lsvhdlMbA")
    assert info["channel_id"] == "UC0BGhWsIbV7Dm-lsvhdlMbA"
    get.assert_not_called()


def test_resolve_from_handle_reads_the_page_then_names_it_from_the_feed():
    with patch("engine.data_sources.youtube_client.requests.get",
               return_value=Mock(status_code=200, text=_CHANNEL_HTML)), \
         patch("engine.data_sources.youtube_client._fetch_feed", return_value=_CHANNEL_FEED):
        info = yt.resolve_channel("@ZipTrader")
    assert info["channel_id"] == "UC0BGhWsIbV7Dm-lsvhdlMbA"
    assert info["display_name"] == "ZipTrader" and info["handle"] == "@ZipTrader"


def test_resolve_from_handle_when_page_is_blocked_gives_actionable_error():
    # e.g. the SSLEOFError YouTube returns to datacenter IPs.
    with patch("engine.data_sources.youtube_client.requests.get", side_effect=OSError("SSLEOFError")):
        with pytest.raises(ValueError, match="blocked the handle lookup"):
            yt.resolve_channel("@ZipTrader")


def test_resolve_raises_when_page_has_no_channel_id():
    with patch("engine.data_sources.youtube_client.requests.get",
               return_value=Mock(status_code=200, text="<html>nothing here</html>")):
        with pytest.raises(ValueError, match="Couldn't find a channel id"):
            yt.resolve_channel("@nobody")
