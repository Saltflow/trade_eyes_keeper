from __future__ import annotations

import json
from datetime import date

import pytest

from src.instruments.models import InstrumentType
from src.instruments.vanguard_provider import VanguardFundProfileProvider


class _Response:
    def __init__(self, text: str, url: str):
        self.text = text
        self.content = text.encode("utf-8")
        self.url = url

    def raise_for_status(self) -> None:
        return None


class _Http:
    def get(self, url, **kwargs):
        if "portfolio-holding" in url:
            return _Response(
                json.dumps(
                    {
                        "asOfDate": "2026-06-30T00:00:00-04:00",
                        "fund": {
                            "entity": [
                                {
                                    "ticker": "AAA",
                                    "longName": "Company A",
                                    "percentWeight": "7.50",
                                },
                                {
                                    "ticker": "BBB",
                                    "longName": "Company B",
                                    "percentWeight": "5.25",
                                },
                            ]
                        },
                    }
                ),
                url,
            )
        profile = {
            "fundProfile": {
                "ticker": "VOO",
                "longName": "Vanguard S&P 500 ETF",
                "shortName": "S&P 500 ETF",
                "type": "Domestic Stock - General",
                "expenseRatio": "0.0300",
                "expenseRatioAsOfDate": "2026-04-28T00:00:00-04:00",
                "isInternalFund": True,
                "isExternalFund": False,
            }
        }
        return _Response(
            '<script id="fundProfileData" type="application/json">'
            f"{json.dumps(profile)}</script>",
            url,
        )


def test_vanguard_official_profile_and_dated_holdings():
    provider = VanguardFundProfileProvider({}, http=_Http())
    payload = provider.fetch(
        "VOO",
        date(2026, 7, 31),
        InstrumentType.INDEX_ETF,
    )
    fund = payload.fund
    assert payload.metadata["name"] == "Vanguard S&P 500 ETF"
    assert fund.issuer == "Vanguard"
    assert fund.tracking_index == "S&P 500"
    assert fund.expense_ratio.value == pytest.approx(0.03)
    assert fund.expense_ratio.as_of == date(2026, 4, 28)
    assert fund.holdings_as_of == date(2026, 6, 30)
    assert [holding.code for holding in fund.top_holdings] == ["AAA", "BBB"]
    assert [holding.weight for holding in fund.top_holdings] == pytest.approx(
        [0.075, 0.0525]
    )
    assert all(
        holding.source == "vanguard_official_holdings"
        for holding in fund.top_holdings
    )


def test_vanguard_rejects_future_holdings():
    provider = VanguardFundProfileProvider({}, http=_Http())
    payload = provider.fetch(
        "VOO",
        date(2026, 6, 1),
        InstrumentType.INDEX_ETF,
    )
    assert payload.fund is not None
    assert payload.fund.top_holdings == []
    assert any(
        attempt["source"] == "vanguard_official_holdings"
        and attempt["status"] == "failed"
        and "after evaluation date" in attempt["reason"]
        for attempt in payload.attempts

    )

class _ExternalFundHttp(_Http):
    def get(self, url, **kwargs):
        profile = {
            "fundProfile": {
                "ticker": "QQQ",
                "longName": "Invesco QQQ Trust",
                "shortName": "Invesco QQQ Trust",
                "isInternalFund": False,
                "isExternalFund": True,
            }
        }
        return _Response(
            '<script id="fundProfileData" type="application/json">'
            f"{json.dumps(profile)}</script>",
            url,
        )


def test_vanguard_does_not_claim_external_fund_profiles():
    provider = VanguardFundProfileProvider({}, http=_ExternalFundHttp())
    payload = provider.fetch(
        "QQQ",
        date(2026, 7, 31),
        InstrumentType.INDEX_ETF,
    )
    assert payload.fund is None
    assert payload.metadata == {}
    assert payload.attempts[-1]["status"] == "unavailable"
    assert "not an internal Vanguard fund" in payload.attempts[-1]["reason"]
