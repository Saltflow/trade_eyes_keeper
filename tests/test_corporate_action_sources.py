"""Distribution evidence never turns an unexplained factor into guessed cash."""

from datetime import date
from unittest.mock import Mock

import pytest

from src.data.corporate_action_sources import (
    SinaCorporateActionProvider,
    parse_sina_share_bonus_html,
    reconcile_corporate_actions,
)
from src.data.market_history import CorporateAction, corporate_action_issues

START, END = date(2018, 1, 1), date(2026, 9, 24)


def _html(rows=(), rights=()):
    def render(values):
        return "<tr>" + "".join(f"<td>{v}</td>" for v in values) + "</tr>"

    return (
        '<table id="sharebonus_1">'
        "<tr><th>公告日期 分红方案(每10股) 送股 转增 派息(税前) 除权除息日</th></tr>"
        + "".join(render(row) for row in rows)
        + '</table><table id="sharebonus_2">'
        "<tr><th>配股 公告日期 配股价格 除权日</th></tr>"
        + "".join(render(row) for row in rights)
        + "</table>"
    )


def _byd_row(**changes):
    # Live Sina 002594 implementation row, corroborated by HKEX 2025-047.
    columns = {
        "announced": "2025-07-22",
        "stock": "8",
        "reserve": "12",
        "cash": "39.74",
        "status": "实施",
        "ex": "2025-07-29",
        "record": "2025-07-28",
        "listed": "--",
        "detail": "查看",
    }
    columns.update(changes)
    return list(columns.values())


def _parse(rows=(), **kwargs):
    return parse_sina_share_bonus_html(_html(rows, **kwargs), "002594", START, END)


def _factor(code="002594", ex_date=date(2025, 7, 29)):
    return CorporateAction(
        code=code,
        action_type="adjustment_factor_change",
        ex_date=ex_date,
        raw_adjustment_factor=0.3,
        source="baostock_adjust_factor",
        diagnostics=["action_composition_not_in_factor_feed"],
    )


def test_real_byd_cash_and_stock_row_keeps_units_and_actual_notice_date():
    (action,) = _parse([_byd_row()])

    assert action.cash_per_share == pytest.approx(3.974)
    assert action.share_multiplier == 3.0
    assert action.published_at == date(2025, 7, 22)
    assert action.record_date == date(2025, 7, 28)
    assert action.ex_date == date(2025, 7, 29)
    assert action.payable_date is None
    assert action.action_type == "cash_and_stock_dividend"
    assert "sina_cash_per_10=39.74" in action.diagnostics
    assert action.source_url.endswith("stockid/002594.phtml")
    assert corporate_action_issues([action]) == ()


def test_real_implemented_cash_notice_is_used_and_pending_plan_is_ignored():
    pending = _byd_row(status="预案", announced="2025-03-28", ex="--", record="--")
    row = _byd_row(
        stock="0",
        reserve="0",
        cash="3.58",
        announced="2026-07-24",
        ex="2026-07-31",
        record="2026-07-30",
    )
    (action,) = _parse([pending, row])
    assert action.cash_per_share == pytest.approx(0.358)
    assert action.share_multiplier == 1.0
    assert action.published_at == date(2026, 7, 24)


def test_valid_empty_tables_do_not_manufacture_actions():
    assert _parse() == []
    assert _parse([_byd_row(stock="0", reserve="0", cash="0")]) == []


@pytest.mark.parametrize(
    "changes",
    [
        {"announced": "--"},
        {"announced": "2025-07-30"},
        {"ex": "--"},
        {"record": "2025-07-30"},
        {"cash": "--"},
        {"cash": "nan"},
        {"cash": "inf"},
        {"cash": "-1"},
        {"stock": "--"},
        {"reserve": "bad"},
    ],
)
def test_invalid_or_incomplete_implemented_rows_are_rejected(changes):
    with pytest.raises(ValueError):
        _parse([_byd_row(**changes)])


@pytest.mark.parametrize(
    "html",
    ["<html>blocked</html>", '<table id="sharebonus_1">changed schema</table>'],
)
def test_missing_or_changed_page_structure_is_not_no_dividends(html):
    with pytest.raises(ValueError):
        parse_sina_share_bonus_html(html, "002594", START, END)


def test_same_day_duplicate_deduplicates_but_conflicting_amount_is_rejected():
    (action,) = _parse([_byd_row(), _byd_row()])
    assert action.cash_per_share == pytest.approx(3.974)
    with pytest.raises(ValueError, match="conflicting distributions"):
        _parse([_byd_row(), _byd_row(cash="40")])


def test_matching_revised_notice_never_backdates_the_final_record():
    (action,) = _parse([_byd_row(announced="2025-07-21"), _byd_row()])
    assert action.published_at == date(2025, 7, 22)


def test_rights_issue_in_requested_window_is_rejected():
    rights = [["2025-07-22", "1", "9.29", "1000", "2025-07-29"]]
    with pytest.raises(ValueError, match="unsupported rights issue"):
        _parse([_byd_row()], rights=rights)


def test_old_rights_issue_outside_requested_window_does_not_hide_dividend():
    rights = [["2013-08-23", "1.74", "9.29", "17666100000", "2013-09-05"]]
    assert len(_parse([_byd_row()], rights=rights)) == 1


def test_provider_uses_one_bounded_http_read_and_real_page_encoding():
    response = Mock(content=_html([_byd_row()]).encode("gb18030"))
    http = Mock()
    http.get.return_value = response
    provider = SinaCorporateActionProvider(http=http)
    (action,) = provider.fetch("002594", START, END)
    assert action.cash_per_share == pytest.approx(3.974)
    http.get.assert_called_once_with(action.source_url, timeout=20.0)
    response.raise_for_status.assert_called_once()


def test_same_day_complete_distribution_explains_factor_without_double_payment():
    supplementary = _parse([_byd_row()])
    factor = _factor()
    original = factor.json()
    cash = CorporateAction(
        code="002594",
        action_type="cash_dividend",
        ex_date=factor.ex_date,
        cash_per_share=3.974,
        source="yahoo_chart_dividends",
    )
    split = CorporateAction(
        code="002594",
        action_type="stock_split",
        ex_date=factor.ex_date,
        share_multiplier=3,
        source="yahoo_chart_splits",
    )

    (action,) = reconcile_corporate_actions([factor, cash, split], supplementary)

    assert action.cash_per_share == pytest.approx(3.974)
    assert action.share_multiplier == 3
    assert action.raw_adjustment_factor == factor.raw_adjustment_factor
    assert action.published_at == date(2025, 7, 22)
    assert corporate_action_issues([action]) == ()
    assert factor.json() == original
    assert cash.published_at is None


@pytest.mark.parametrize(
    "code,day", [("000333", date(2025, 7, 29)), ("002594", date(2025, 7, 28))]
)
def test_different_code_or_date_does_not_discharge_unexplained_factor(code, day):
    result = reconcile_corporate_actions([_factor(code, day)], _parse([_byd_row()]))
    assert len(result) == 2
    assert any(
        "unresolved corporate action factor" in x
        for x in corporate_action_issues(result)
    )


def test_incomplete_share_evidence_keeps_factor_unresolved():
    partial = _parse([_byd_row()])[0]
    partial.share_multiplier = None
    result = reconcile_corporate_actions([_factor()], [partial])
    assert any(a.action_type == "adjustment_factor_change" for a in result)
    assert corporate_action_issues(result)


@pytest.mark.parametrize(
    "change",
    [{"cash_per_share": 4.0}, {"share_multiplier": 2.0}, {"rights_price": 8.0}],
)
def test_conflicting_or_rights_action_is_rejected_not_erased(change):
    original = _parse([_byd_row()])[0]
    for name, value in change.items():
        setattr(original, name, value)
    with pytest.raises(ValueError):
        reconcile_corporate_actions([_factor(), original], _parse([_byd_row()]))


def test_declared_source_precision_preserves_more_precise_baostock_cash():
    row = _byd_row(
        stock="0",
        reserve="0",
        cash="30.9777",
        announced="2024-07-20",
        ex="2024-07-29",
        record="2024-07-26",
    )
    source = _parse([row])
    original = CorporateAction(
        code="002594",
        action_type="cash_dividend",
        ex_date=date(2024, 7, 29),
        cash_per_share=3.097772,
        published_at=date(2024, 3, 27),
        source="baostock_dividend",
        payable_date=date(2024, 7, 29),
    )
    (action,) = reconcile_corporate_actions([original], source)
    assert action.cash_per_share == 3.097772
    assert action.published_at == date(2024, 7, 20)
    assert action.payable_date == date(2024, 7, 29)
    assert any(x.startswith("source_rounding_match:") for x in action.diagnostics)


@pytest.mark.parametrize("cash", [3.097776, 3.098, 30.9777])
def test_rounding_boundary_and_real_amount_conflict_are_rejected(cash):
    source = _parse([_byd_row(stock="0", reserve="0", cash="30.9777")])
    original = source[0].copy(update={"cash_per_share": cash, "source": "baostock"})
    with pytest.raises(ValueError, match="conflicting dividend cash"):
        reconcile_corporate_actions([original], source)


def test_real_midea_half_digit_rounding_keeps_precise_original_cash():
    source = _parse([_byd_row(stock="0", reserve="0", cash="16.0058")])
    original = source[0].copy(update={"cash_per_share": 1.600585, "source": "baostock"})
    result = reconcile_corporate_actions([original], source)
    assert result[0].cash_per_share == 1.600585
    assert any(
        item.startswith("source_rounding_match:") for item in result[0].diagnostics
    )


def test_terse_integer_cash_cell_cannot_hide_a_significant_amount_difference():
    source = _parse([_byd_row(stock="0", reserve="0", cash="4")])
    original = source[0].copy(update={"cash_per_share": 0.449, "source": "baostock"})
    with pytest.raises(ValueError, match="conflicting dividend cash"):
        reconcile_corporate_actions([original], source)
