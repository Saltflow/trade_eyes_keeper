from datetime import date

import pytest

from src.instruments.fund_providers import EastmoneyFundProfileProvider
from src.instruments.models import InstrumentType


class _Response:
    def __init__(self, text, url):
        self.text = text
        self.content = text.encode("utf-8")
        self.url = url

    def raise_for_status(self):
        return None


class _Http:
    def get(self, url, **kwargs):
        if "jbgk_" in url:
            return _Response(
                """
                <table>
                  <tr><th>基金全称</th><td>测试指数基金</td></tr>
                  <tr><th>基金类型</th><td>指数型-股票</td></tr>
                  <tr><th>净资产规模</th><td>12.50亿元（截止至：2026年06月30日）</td></tr>
                  <tr><th>基金管理人</th><td>测试基金公司</td></tr>
                  <tr><th>管理费率</th><td>0.15%（每年）</td></tr>
                  <tr><th>跟踪标的</th><td>测试指数</td></tr>
                </table>
                """,
                url,
            )
        if "pingzhongdata" in url:
            return _Response(
                'var Data_netWorthTrend = ['
                '{"x":1782777600000,"y":1.20},'
                '{"x":1782864000000,"y":1.25}'
                "];",
                url,
            )
        return _Response(
            """
            <h4>2026年2季度股票投资明细 截止至：2026-06-30</h4>
            <table><thead><tr>
              <th>序号</th><th>股票代码</th><th>股票名称</th>
              <th>最新价</th><th>涨跌幅</th><th>相关资讯</th>
              <th>占净值比例</th>
            </tr></thead><tbody>
              <tr><td>1</td><td>600000</td><td>测试公司A</td>
                  <td></td><td></td><td></td><td>6.50%</td></tr>
              <tr><td>2</td><td>000001</td><td>测试公司B</td>
                  <td></td><td></td><td></td><td>4.00%</td></tr>
            </tbody></table>
            """,
            url,
        )


def test_mainland_fund_profile_parses_facts_nav_and_holdings():
    provider = EastmoneyFundProfileProvider({}, http=_Http())
    payload = provider.fetch(
        "510300",
        date(2026, 7, 31),
        InstrumentType.INDEX_ETF,
    )
    fund = payload.fund
    assert payload.metadata["name"] == "测试指数基金"
    assert fund.issuer == "测试基金公司"
    assert fund.tracking_index == "测试指数"
    assert fund.aum.value == pytest.approx(1_250_000_000)
    assert fund.expense_ratio.value == pytest.approx(0.15)
    assert fund.nav_per_unit.value == pytest.approx(1.25)
    assert fund.nav_per_unit.as_of == date(2026, 7, 1)
    assert [holding.code for holding in fund.top_holdings] == ["600000", "000001"]
    assert [holding.weight for holding in fund.top_holdings] == pytest.approx(
        [0.065, 0.04]
    )
    assert fund.holdings_as_of == date(2026, 6, 30)
