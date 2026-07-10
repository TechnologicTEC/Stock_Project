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


def test_get_transcript_prefers_supadata_when_configured():
    with patch("engine.data_sources.supadata_client.is_configured", return_value=True), \
         patch("engine.data_sources.supadata_client.get_transcript_text", return_value="body"), \
         patch("engine.data_sources.youtube_client._direct_transcript") as direct:
        assert yt.get_transcript("v") == ("ok", "body")
    direct.assert_not_called()


def test_supadata_transcript_unavailable_is_authoritative_no_captions():
    from engine.data_sources.supadata_client import TranscriptUnavailable
    with patch("engine.data_sources.supadata_client.is_configured", return_value=True), \
         patch("engine.data_sources.supadata_client.get_transcript_text",
               side_effect=TranscriptUnavailable("none")), \
         patch("engine.data_sources.youtube_client._direct_transcript") as direct:
        assert yt.get_transcript("v") == ("no_captions", None)
    direct.assert_not_called()          # don't burn a retry on a video with no captions


def test_supadata_quota_failure_falls_back_to_the_direct_path():
    from engine.data_sources.supadata_client import QuotaExceeded
    with patch("engine.data_sources.supadata_client.is_configured", return_value=True), \
         patch("engine.data_sources.supadata_client.get_transcript_text", side_effect=QuotaExceeded("429")), \
         patch("engine.data_sources.youtube_client._direct_transcript", return_value=("blocked", None)) as direct:
        assert yt.get_transcript("v") == ("blocked", None)   # -> the scan retries next run
    direct.assert_called_once()


def test_get_transcript_uses_direct_path_when_supadata_unconfigured():
    with patch("engine.data_sources.supadata_client.is_configured", return_value=False), \
         patch("engine.data_sources.youtube_client._direct_transcript", return_value=("ok", "t")) as direct:
        assert yt.get_transcript("v") == ("ok", "t")
    direct.assert_called_once()


# Cleared so the fallback-path tests can't be perturbed by a real key/proxy in .env.
_TRANSCRIPT_ENV = ("WEBSHARE_PROXY_USERNAME", "WEBSHARE_PROXY_PASSWORD", "YT_PROXY_URL",
                   "SUPADATA_API_KEY", "SUPADATA_MODE", "YOUTUBE_API_KEY")


@pytest.fixture(autouse=True)
def _clean_transcript_env(monkeypatch):
    for key in _TRANSCRIPT_ENV:
        monkeypatch.delenv(key, raising=False)


def test_latest_videos_prefers_the_official_data_api():
    videos = [{"video_id": "AAA", "title": "t", "url": "u", "published_at": None}]
    with patch("engine.data_sources.youtube_data_api.is_configured", return_value=True), \
         patch("engine.data_sources.youtube_data_api.list_uploads", return_value=videos) as api, \
         patch("engine.data_sources.youtube_client._fetch_feed") as feed:
        assert yt.latest_videos("UCxxxx") == videos
    api.assert_called_once()
    feed.assert_not_called()          # the flaky RSS feed is never touched


def test_latest_videos_falls_back_to_rss_when_data_api_fails():
    with patch("engine.data_sources.youtube_data_api.is_configured", return_value=True), \
         patch("engine.data_sources.youtube_data_api.list_uploads", side_effect=RuntimeError("quota")), \
         patch("engine.data_sources.youtube_client._fetch_feed", return_value=_FEED.encode()) as feed:
        videos = yt.latest_videos("UCxxxx")
    assert [v["video_id"] for v in videos] == ["ABC123", "DEF456"]
    feed.assert_called_once()


def test_resolve_channel_prefers_the_data_api_and_skips_scraping():
    info = {"channel_id": "UC0BGhWsIbV7Dm-lsvhdlMbA", "display_name": "ZipTrader", "handle": "@ziptrader"}
    with patch("engine.data_sources.youtube_data_api.is_configured", return_value=True), \
         patch("engine.data_sources.youtube_data_api.resolve_channel", return_value=info), \
         patch("engine.data_sources.youtube_client.requests.get") as get:
        assert yt.resolve_channel("@ZipTrader") == info
    get.assert_not_called()


def test_proxy_config_is_none_when_unset():
    assert yt._proxy_config() is None


def test_proxy_config_prefers_webshare(monkeypatch):
    from youtube_transcript_api.proxies import WebshareProxyConfig
    monkeypatch.setenv("WEBSHARE_PROXY_USERNAME", "u")
    monkeypatch.setenv("WEBSHARE_PROXY_PASSWORD", "p")
    monkeypatch.setenv("YT_PROXY_URL", "http://ignored:1")
    assert isinstance(yt._proxy_config(), WebshareProxyConfig)


def test_proxy_config_falls_back_to_generic_url(monkeypatch):
    from youtube_transcript_api.proxies import GenericProxyConfig
    monkeypatch.setenv("YT_PROXY_URL", "http://user:pw@host:8080")
    assert isinstance(yt._proxy_config(), GenericProxyConfig)


def test_transcript_fetch_passes_proxy_config_through():
    with patch("youtube_transcript_api.YouTubeTranscriptApi") as api_cls:
        api_cls.return_value.fetch.return_value = [Mock(text="hello"), Mock(text="world")]
        assert yt._fetch_transcript_text("vid") == "hello world"
    assert api_cls.call_args.kwargs["proxy_config"] is None   # unset env -> direct connection


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
