"""
engine/data_sources/edgar_client: which 8-K counts as the earnings release.
Network is mocked; only the selection and the atom parsing are exercised.
"""
from types import SimpleNamespace
from unittest.mock import patch

from engine.data_sources import edgar_client


def test_only_a_results_of_operations_8k_is_an_earnings_filing():
    assert edgar_client.is_earnings_filing({"items": "items 2.02 and 9.01"})
    assert not edgar_client.is_earnings_filing({"items": "items 1.01 and 9.01"})
    assert not edgar_client.is_earnings_filing({"items": "items 12.02"})
    assert edgar_client.is_earnings_filing({"items": None})          # EDGAR didn't list them


def test_a_deal_announcement_is_not_shown_as_the_earnings_release():
    """ADI's newest 8-K with an EX-99.1 (Nov 2026) was an acquisition."""
    filings = [
        {"filing_date": "2026-09-09", "href": "deal-index", "items": "items 1.01 and 9.01"},
        {"filing_date": "2026-08-20", "href": "results-index", "items": "items 2.02 and 9.01"},
    ]
    with patch.object(edgar_client, "get_company_filings", return_value=filings), \
         patch.object(edgar_client, "_find_ex99_document_url", side_effect=lambda idx: f"{idx}/ex99.htm"), \
         patch.object(edgar_client, "_document_text", return_value="Revenue grew 10%."):
        release = edgar_client.get_8k_press_release("0000006281")
    assert release["filing_date"] == "2026-08-20"
    assert release["url"] == "results-index/ex99.htm"


def test_the_item_list_is_read_from_the_filing_feed():
    atom = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry>
      <title>8-K</title><content><filing-date>2026-08-20</filing-date>
      <filing-href>https://x/index.htm</filing-href><filing-type>8-K</filing-type>
      <items-desc>items 2.02 and 9.01</items-desc></content></entry></feed>"""
    with patch.object(edgar_client, "_throttled_get", return_value=SimpleNamespace(text=atom)):
        filings = edgar_client.get_company_filings("0000006281", "8-K")
    assert filings[0]["items"] == "items 2.02 and 9.01"
