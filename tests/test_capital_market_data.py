from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from src.fundamental_embedding.capital_market_data import (
    IndexFactsheet,
    implied_equity_risk_premium,
    parse_chinabond_ten_year_yield,
    parse_csi300_factsheet,
)


def test_parse_chinabond_official_curve_table():
    content = """
    <table>
      <tr><td>曲线名称</td><td>日期</td><td>10年</td></tr>
      <tr><td>中债国债收益率曲线</td><td>2026-07-31</td><td>1.7141</td></tr>
    </table>
    """

    result = parse_chinabond_ten_year_yield(
        content, as_of=date(2026, 7, 31)
    )

    assert result == pytest.approx(0.017141)


def test_parse_csi300_official_factsheet(monkeypatch):
    class Page:
        def extract_text(self):
            return (
                "2026年7月31日 基本面 滚动市盈率 14.36 "
                "市净率 1.41 股息率 2.23%"
            )

    class Document:
        pages = [Page()]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setitem(
        __import__("sys").modules,
        "pdfplumber",
        SimpleNamespace(open=lambda _stream: Document()),
    )

    result = parse_csi300_factsheet(b"official-pdf")

    assert result.as_of == date(2026, 7, 31)
    assert result.trailing_pe == pytest.approx(14.36)
    assert result.price_to_book == pytest.approx(1.41)
    assert result.dividend_yield == pytest.approx(0.0223)


def test_forward_implied_erp_uses_pe_pb_dividend_and_explicit_scenarios():
    factsheet = IndexFactsheet(
        as_of=date(2026, 7, 31),
        trailing_pe=14.36,
        price_to_book=1.41,
        dividend_yield=0.0223,
        source_url="official",
    )

    result = implied_equity_risk_premium(factsheet, 0.017141)

    assert result.roe_proxy == pytest.approx(1.41 / 14.36)
    assert result.current_payout_ratio == pytest.approx(0.320228)
    assert result.initial_growth == pytest.approx(0.0667464)
    assert result.market_risk_premium == pytest.approx(0.0636267, abs=1e-6)
    assert result.low == pytest.approx(0.060834, abs=1e-6)
    assert result.high == pytest.approx(0.066364, abs=1e-6)
    assert [item["terminal_growth"] for item in result.scenarios] == [
        0.02,
        0.03,
        0.04,
    ]
