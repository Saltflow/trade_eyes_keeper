"""Report presentation contracts; synthetic fixtures never contact a provider."""

from copy import deepcopy
from types import SimpleNamespace

import pandas as pd
import pytest
from bs4 import BeautifulSoup

from src.notification.email_notifier import EmailNotifier
from src.notification.report_builders import (
    build_daily_event_rows,
    build_daily_events_text,
)
from src.notification.report_policy import (
    PRICE_COLORS,
    price_movement,
    return_curves_html,
)


def notice(title, date="2026-11-02", **extra):
    return {
        "title": title,
        "date": date,
        "url": f"https://example.test/{title}",
        **extra,
    }


def test_categories_market_instrument_order_and_source_priority():
    frame = pd.DataFrame([{"stock_code": "00700", "stock_name": "腾讯控股"}])
    announcements = {
        "00700": [
            notice("翌日披露報表", type="翌日披露報表 - [股份購回]"),
            notice("年度股息"),
            notice("年度報告"),
        ],
        "601088": [
            notice("年度分红实施公告"),
            notice("重大资产重组"),
            notice("季度报告"),
            notice("董事会公告", type="股份回购"),
        ],
        "AAPL": [notice("annual results", type="10-K"), notice("something else")],
    }
    rows = build_daily_event_rows(announcements, {}, frame, as_of="2026-11-02")
    assert [(row["category"], row["market"], row["code"]) for row in rows] == [
        (0, "a_share", "601088"),
        (1, "a_share", "601088"),
        (1, "hk", "00700"),
        (1, "us", "AAPL"),
        (2, "a_share", "601088"),
        (2, "hk", "00700"),
        (3, "a_share", "601088"),
        (4, "hk", "00700"),
        (5, "us", "AAPL"),
    ]
    assert next(row for row in rows if row["code"] == "00700")["name"] == "腾讯控股"


def test_routine_forms_do_not_inherit_high_priority_from_incidental_metadata():
    announcements = {
        "01339": [
            notice("關於召開2026年第三季度業績說明會的公告", type="季度業績"),
            notice("臨時股東會之投票結果", type="股息 / 持續關連交易"),
            notice("2026年10月产销快报"),
        ]
    }
    rows = build_daily_event_rows(announcements, {}, None, as_of="2026-11-02")
    assert [row["category"] for row in rows] == [1, 4, 4]


def test_calendar_cutoff_timezone_missing_date_and_pending_items_are_non_mutating():
    announcements = {
        "601088": [
            notice("旧年报", "2024-02-28"),
            notice("边界年报", "2024-02-29"),
            notice("年度分红", "2024-02-28", published_at="2024-02-28T16:30:00Z"),
            notice("无法确认日期", "invalid"),
            notice("2023年年度报告", "2024-03-29"),
        ]
    }
    original = deepcopy(announcements)
    placements = {"601088": {"unlock_date": "2028-01-10", "issue_price": 8.3}}
    pending = [
        {
            "code": "601088",
            "status": "已除权未派息",
            "ex_date": "2023-01-01",
            "payment_date": "2024-04-01",
        }
    ]
    config = {"report": {"events": {"announcement_lookback_months": 1}}}
    rows = build_daily_event_rows(
        announcements,
        placements,
        None,
        pending,
        as_of="2024-03-31T19:00:00+08:00",
        config=config,
    )
    titles = "|".join(row["title"] for row in rows)
    assert "旧年报" not in titles and "边界年报" in titles
    assert "年度分红" in titles and "2023年年度报告" in titles
    assert "2028-01-10" in titles and "2023-01-01" in titles
    assert next(row for row in rows if row["title"] == "无法确认日期")["date"] == ""
    assert announcements == original
    longer = {"report": {"events": {"announcement_lookback_months": 2}}}
    assert any(
        row["title"] == "旧年报"
        for row in build_daily_event_rows(
            announcements, {}, None, as_of="2024-03-31", config=longer
        )
    )


def test_routine_merge_preserves_every_link_across_email_and_bot(tmp_path):
    notices = [
        notice(
            "翌日披露報表", date=f"2026-11-0{day}", url=f"https://example.test/{day}"
        )
        for day in (1, 2)
    ]
    notices += [notice("证券变动月报表"), notice("股东会通函"), notice("董事会通函")]
    announcements = {"09999": notices}
    frame = pd.DataFrame([{"stock_code": "09999", "stock_name": "网易"}])
    config = {"email": {"archive_dir": str(tmp_path)}}
    rows = build_daily_event_rows(announcements, {}, frame, as_of="2026-11-02")
    assert len(rows) == 4
    merged = next(row for row in rows if len(row.get("members", [])) == 2)
    assert [member["date"] for member in merged["members"]] == [
        "2026-11-02",
        "2026-11-01",
    ]
    html = EmailNotifier(config)._build_daily_events_section(
        announcements, {}, frame, report_as_of="2026-11-02"
    )
    soup = BeautifulSoup(html, "html.parser")
    assert len(soup.select(".event-routine")) == 1 and "2份" in soup.get_text()
    assert len(soup.select(".event-instrument")) == 1 and "网易" in soup.get_text()
    assert {link["href"] for link in soup.select("a")} == {
        item["url"] for item in notices
    }
    session = SimpleNamespace(
        config=config,
        announcements=announcements,
        placements={},
        created_at="2026-11-02",
    )
    text = build_daily_events_text(session, frame)
    assert "例行披露 · 港股" in text and all(item["url"] in text for item in notices)


@pytest.mark.parametrize(
    "close,previous,direction,change",
    [
        (10.2, 10, "up", "+2.00%"),
        (9.8, 10, "down", "-2.00%"),
        (10, 10, "flat", "+0.00%"),
        (10.00001, 10, "up", "+0.00%"),
        (10, None, "unknown", "涨跌未知"),
        (10, 0, "unknown", "涨跌未知"),
        (float("nan"), 10, "unknown", "涨跌未知"),
    ],
)
def test_unrounded_movement_is_used_in_real_daily_cells(
    tmp_path, close, previous, direction, change
):
    assert price_movement(close, previous) == (direction, change)
    notifier = EmailNotifier({"email": {"archive_dir": str(tmp_path)}})
    row = {
        "group": "a_share",
        "code": "601088",
        "name": "中国神华",
        "roe": 0,
        "entry": {"open": 10, "close": close, "prev_close": previous},
    }
    soup = BeautifulSoup(notifier._build_daily_watchlist_section([row]), "html.parser")
    cell = soup.select_one(".current-quote")
    assert PRICE_COLORS[direction] in cell["style"] and cell.strong is not None
    assert cell.select_one(".price-change").get_text() == change


def test_daily_curve_caption_has_no_duplicate_sparkline_but_brief_keeps_it():
    payload = {
        "strategy_id": "ke_threshold_allocation",
        "markets": {
            "a_share": {
                "dates": ["2026-01-01", "2026-01-02"],
                "values": [100, 110],
                "initial_capital": 100,
                "coverage": {"status": "complete"},
            },
        },
    }
    daily = return_curves_html(payload, "cid:chart002", compact=True)
    assert daily.count("<img") == 1 and "<h3>" not in daily and "<pre" not in daily
    assert not any(char in daily for char in "▁▂▃▄▅▆▇█")
    assert "美股：暂不可用" in daily and "数据截至 2026-01-02" in daily
    assert "▁" in return_curves_html(payload)
