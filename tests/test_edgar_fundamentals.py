from datetime import date
from unittest.mock import patch

from engine.data_sources import edgar_fundamentals as ef


def _facts():
    """A synthetic companyfacts payload covering every mess the parser handles:
    tag drift, a YTD row that must be dropped, a restatement, a non-periodic
    form, and a balance-sheet (instantaneous) metric."""
    return {
        "facts": {
            "us-gaap": {
                # Revenue under the OLD tag (Q1) and NEW tag (Q1 comparative + Q2 + a YTD row)
                "Revenues": {
                    "units": {"USD": [
                        {"start": "2022-01-01", "end": "2022-03-31", "val": 100, "form": "10-Q", "filed": "2022-05-01"},
                    ]}
                },
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {"USD": [
                        # same Q1 end, but filed a year later as a comparative — must lose to the earlier filing
                        {"start": "2022-01-01", "end": "2022-03-31", "val": 100, "form": "10-Q", "filed": "2023-05-01"},
                        {"start": "2022-04-01", "end": "2022-06-30", "val": 110, "form": "10-Q", "filed": "2022-08-01"},
                        # a 6-month YTD row sharing the Q2 end — must be dropped (not quarterly)
                        {"start": "2022-01-01", "end": "2022-06-30", "val": 210, "form": "10-Q", "filed": "2022-08-01"},
                    ]}
                },
                "NetIncomeLoss": {
                    "units": {"USD": [
                        {"start": "2022-01-01", "end": "2022-03-31", "val": 20, "form": "10-Q", "filed": "2022-05-01"},
                        # a restatement of the same quarter, filed later — earliest-filed should win
                        {"start": "2022-01-01", "end": "2022-03-31", "val": 22, "form": "10-Q/A", "filed": "2022-11-01"},
                        # a non-periodic form must be ignored entirely
                        {"start": "2022-01-01", "end": "2022-03-31", "val": 999, "form": "8-K", "filed": "2022-04-15"},
                    ]}
                },
                # Balance-sheet metric: instantaneous, no `start`
                "StockholdersEquity": {
                    "units": {"USD": [
                        {"end": "2022-03-31", "val": 500, "form": "10-Q", "filed": "2022-05-01"},
                        {"end": "2022-06-30", "val": 520, "form": "10-Q", "filed": "2022-08-01"},
                    ]}
                },
            }
        }
    }


def test_series_coalesces_tags_and_keeps_earliest_filed():
    series = ef.pit_series_from_facts(_facts())
    revenue = series["revenue"]
    assert [r["end"] for r in revenue] == ["2022-03-31", "2022-06-30"]
    q1 = revenue[0]
    assert q1["value"] == 100.0
    assert q1["filed"] == "2022-05-01"  # the earlier filing, not the 2023 comparative


def test_series_drops_ytd_rows_for_flow_metrics():
    revenue = ef.pit_series_from_facts(_facts())["revenue"]
    # the 6-month YTD value of 210 must not appear; only the two true quarters do
    assert all(r["value"] != 210.0 for r in revenue)
    assert {r["value"] for r in revenue} == {100.0, 110.0}


def test_series_takes_earliest_filed_over_restatements_and_ignores_non_periodic_forms():
    net_income = ef.pit_series_from_facts(_facts())["net_income"]
    assert len(net_income) == 1
    assert net_income[0]["value"] == 20.0            # original, not the 22 restatement
    assert net_income[0]["filed"] == "2022-05-01"    # and definitely not the 8-K's 999


def test_series_handles_instantaneous_balance_metrics():
    equity = ef.pit_series_from_facts(_facts())["equity"]
    assert [(e["end"], e["value"]) for e in equity] == [("2022-03-31", 500.0), ("2022-06-30", 520.0)]


# --------------------------------------------------------------------------
# known_as_of — the look-ahead guard
# --------------------------------------------------------------------------

def test_known_as_of_excludes_facts_filed_after_the_as_of_date():
    series = ef.pit_series_from_facts(_facts())

    # On 2022-07-01, Q1 (filed 2022-05-01) is public but Q2 (filed 2022-08-01) is NOT yet.
    snap = ef.known_as_of(series, date(2022, 7, 1))
    assert snap["revenue"]["end"] == "2022-03-31"
    assert snap["revenue"]["value"] == 100.0

    # By 2022-09-01 both are public — take the most recent period.
    later = ef.known_as_of(series, date(2022, 9, 1))
    assert later["revenue"]["end"] == "2022-06-30"
    assert later["revenue"]["value"] == 110.0


def test_known_as_of_omits_metrics_with_nothing_filed_yet():
    series = ef.pit_series_from_facts(_facts())
    # Nothing was filed until 2022-05-01, so on 2022-04-01 there's no revenue yet.
    assert "revenue" not in ef.known_as_of(series, date(2022, 4, 1))


# --------------------------------------------------------------------------
# Fetch + cache wrappers (network mocked)
# --------------------------------------------------------------------------

def test_get_pit_fundamentals_fetches_via_edgar_and_caches():
    calls = {"n": 0}

    def fake_facts(cik):
        calls["n"] += 1
        return _facts()

    with patch("engine.data_sources.edgar_fundamentals.edgar_client.get_cik_for_ticker", return_value="0000320193"), \
         patch("engine.data_sources.edgar_fundamentals.edgar_client.get_company_facts", side_effect=fake_facts):
        first = ef.get_pit_fundamentals("AAPL")
        second = ef.get_pit_fundamentals("AAPL")  # served from cache

    assert calls["n"] == 1
    assert first["revenue"][0]["value"] == 100.0
    assert second == first


def test_pit_snapshot_returns_point_in_time_values():
    with patch("engine.data_sources.edgar_fundamentals.edgar_client.get_cik_for_ticker", return_value="0000320193"), \
         patch("engine.data_sources.edgar_fundamentals.edgar_client.get_company_facts", return_value=_facts()):
        snap = ef.pit_snapshot("AAPL", date(2022, 7, 1))

    assert snap["revenue"]["value"] == 100.0
    assert snap["net_income"]["value"] == 20.0
    assert snap["equity"]["value"] == 500.0


def test_get_pit_fundamentals_empty_for_unknown_ticker():
    with patch("engine.data_sources.edgar_fundamentals.edgar_client.get_cik_for_ticker", return_value=None):
        assert ef.get_pit_fundamentals("NOTATICKER") == {}
