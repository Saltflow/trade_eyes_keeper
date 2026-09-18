import json

import pandas as pd
import pytest
import requests

from src.data.etf_valuation import ETFValuationResolver
from src.data.web_crawler import StockWebCrawler


class _Response:
    def __init__(self, *, text="", content=b"", payload=None):
        self.text = text
        self.content = content
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _qq_line(symbol, pe=None, pb=None):
    fields = [""] * 60
    if pe is not None:
        fields[39] = str(pe)
    if pb is not None:
        fields[58 if symbol.startswith("hk") else 51 if symbol.startswith("us") else 46] = str(pb)
    return f'v_{symbol}="{"~".join(fields)}";\n'


def test_csi_etf_uses_weighted_constituent_pe_pb(monkeypatch):
    weights = pd.DataFrame(
        [[None, None, None, None, "600001", None, None, None, None, 60.0],
         [None, None, None, None, "000001", None, None, None, None, 40.0]]
    )

    def fake_get(url, **kwargs):
        if "closeweight" in url:
            return _Response(content=b"fixture")
        assert "qt.gtimg.cn" in url
        return _Response(
            text=_qq_line("sh600001", 10.0, 2.0)
            + _qq_line("sz000001", 20.0, 4.0)
        )

    monkeypatch.setattr("src.data.etf_valuation.pd.read_excel", lambda _: weights)
    result = ETFValuationResolver(http_get=fake_get).resolve("512810")

    assert result is not None
    assert result["pe_ratio"] == pytest.approx(12.5)
    assert result["pb_ratio"] == pytest.approx(2.5)
    assert "中证官方成分权重" in result["valuation_source"]


def test_low_constituent_coverage_never_claims_etf_valuation(monkeypatch):
    weights = pd.DataFrame(
        [[None, None, None, None, "600001", None, None, None, None, 60.0],
         [None, None, None, None, "000001", None, None, None, None, 40.0]]
    )

    def fake_get(url, **kwargs):
        if "closeweight" in url:
            return _Response(content=b"fixture")
        return _Response(text=_qq_line("sh600001", 10.0, 2.0))

    monkeypatch.setattr("src.data.etf_valuation.pd.read_excel", lambda _: weights)
    assert ETFValuationResolver(http_get=fake_get).resolve("512810") is None


def test_vanguard_sp500_components_fill_us_etf_pe_pb():
    def fake_get(url, **kwargs):
        if "vanguard" in url:
            return _Response(
                payload={
                    "fund": {
                        "entity": [
                            {"ticker": "AAA", "percentWeight": "60"},
                            {"ticker": "BBB", "percentWeight": "40"},
                        ]
                    }
                }
            )
        return _Response(
            text=_qq_line("usAAA", 10.0, 2.0) + _qq_line("usBBB", 20.0, 4.0)
        )

    result = ETFValuationResolver(http_get=fake_get).resolve("VOO")

    assert result is not None
    assert result["pe_ratio"] == pytest.approx(12.5)
    assert result["pb_ratio"] == pytest.approx(2.5)
    assert "Vanguard官方成分股" in result["valuation_source"]


def test_yahoo_fund_component_yields_are_converted_to_multiples():
    payload = {
        "quoteSummary": {
            "result": [
                {
                    "summaryDetail": {"trailingPE": {"raw": 25.0}},
                    "topHoldings": {
                        "equityHoldings": {
                            "priceToEarnings": {"raw": 0.04},
                            "priceToBook": {"raw": 0.20},
                        }
                    },
                }
            ]
        }
    }
    script = json.dumps({"body": json.dumps(payload)})

    result = ETFValuationResolver(
        http_get=lambda url, **kwargs: _Response(
            text=f'<script type="application/json">{script}</script>'
        )
    ).resolve("QQQ")

    assert result is not None
    assert result["pe_ratio"] == pytest.approx(25.0)
    assert result["pb_ratio"] == pytest.approx(5.0)


def test_yahoo_block_uses_nasdaq100_daily_aggregate_fallback():
    def fake_get(url, **kwargs):
        if "finance.yahoo.com" in url:
            raise requests.HTTPError("403")
        return _Response(
            text=(
                "<html><body>Nasdaq-100 P/E ratio: 31.4 "
                "Trailing P/E 31.4 Price/Book 8.7</body></html>"
            )
        )

    result = ETFValuationResolver(http_get=fake_get).resolve("QQQ")

    assert result is not None
    assert result["pe_ratio"] == pytest.approx(31.4)
    assert result["pb_ratio"] == pytest.approx(8.7)
    assert "Nasdaq-100公开汇总" in result["valuation_source"]


def test_nikkei_uses_latest_official_per_and_pbr_rows():
    def fake_get(url, **kwargs):
        value = "17.19" if "list=per" in url else "1.86"
        return _Response(
            text=(
                "<table><tr><th>date</th></tr>"
                f"<tr><td>2026.09.18</td><td>{value}</td><td>ignored</td></tr>"
                "</table>"
            )
        )

    result = ETFValuationResolver(http_get=fake_get).resolve("513520")

    assert result is not None
    assert result["pe_ratio"] == pytest.approx(17.19)
    assert result["pb_ratio"] == pytest.approx(1.86)


def test_nikkei_block_uses_public_daily_table_fallback():
    def fake_get(url, **kwargs):
        if "indexes.nikkei.co.jp" in url:
            raise requests.HTTPError("403")
        return _Response(
            text=(
                '<table><trclass="hide"><td>9/17</td><td>64,136.25</td>'
                '<td>213.25</td><td>17.16</td><td>1.88</td></trclass></table>'
            )
        )

    result = ETFValuationResolver(http_get=fake_get).resolve("513520")

    assert result is not None
    assert result["pe_ratio"] == pytest.approx(17.16)
    assert result["pb_ratio"] == pytest.approx(1.88)
    assert "日经公开日度表" in result["valuation_source"]


def test_gold_and_reit_are_explicitly_not_company_valuations():
    resolver = ETFValuationResolver(http_get=lambda *args, **kwargs: None)

    assert resolver.resolve("518660") is None
    assert resolver.resolve("508091") is None


@pytest.mark.parametrize(
    ("code", "market", "pb_index", "pb_value"),
    [("00700", "hk", 58, 2.93), ("GOOG", "us", 51, 6.79)],
)
def test_qq_hk_and_us_use_their_real_pb_field(monkeypatch, code, market, pb_index, pb_value):
    fields = [""] * 60
    fields[39] = "20"
    fields[46] = "company English name"
    fields[pb_index] = str(pb_value)
    text = f'v_{market}{code}="{"~".join(fields)}";'
    monkeypatch.setattr(
        "src.data.web_crawler.requests.get", lambda *args, **kwargs: _Response(text=text)
    )

    result = StockWebCrawler(config={}).fetch_valuation_data(code)

    assert result["pe_ratio"] == 20.0
    assert result["pb_ratio"] == pb_value
