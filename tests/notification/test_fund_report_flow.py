"""Fund source metadata reaches the ordinary shared report and all transports."""

import json
import socket
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest
from zoneinfo import ZoneInfo

from src.data import announcement_fetcher
from src.data.announcement_fetcher import AnnouncementFetcher
from src.notification.daily_pdf import generate_daily_pdf
from src.notification.email_notifier import EmailNotifier
from src.notification.feishu_notifier import FeishuNotifier
from src.notification.manager import NotifierManager
from src.notification.report_builders import build_daily_event_rows
from src.notification.telegram_notifier import TelegramNotifier

NOW = datetime(2026, 11, 3, 12, tzinfo=ZoneInfo("Asia/Shanghai"))


class Response:
    def __init__(self, value):
        self.value = value
        self.content = json.dumps(value, ensure_ascii=False).encode()

    def json(self):
        return self.value

    def raise_for_status(self):
        pass


@pytest.fixture
def source(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("fund report tests must not access the network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    clock = Mock(wraps=datetime)
    clock.now.return_value = NOW
    monkeypatch.setattr(announcement_fetcher, "datetime", clock)
    fetcher = AnnouncementFetcher({})
    categories = []

    def get(url, **kwargs):
        if url.endswith("fund_stock.json"):
            return Response(
                {"stockList": [{"code": "159655", "orgId": "fund-manager"}]}
            )
        assert kwargs["params"]["sqlId"] == "COMMON_PL_JJXX_JJGG_NEW_L"
        code = kwargs["params"]["SECURITY_CODE"]
        rows = [
            {
                "SECURITY_CODE": code,
                "SSEDATE": day,
                "TITLE": title,
                "URL": f"/disclosure/fund/announcement/c/new/{code}_{i}.pdf",
                "ORG_BULLETIN_TYPE_DESC": "季度报告" if i == 0 else "其他",
                "BULLETIN_TYPE_DESC": "定期报告",
            }
            for i, (day, title) in enumerate(
                [
                    ("2026-10-27", f"合成{code}基金2026年第3季度报告"),
                    ("2026-11-02", "基金招募说明书更新"),
                    ("2026-11-02", "基金2026年中期报告提示性公告"),
                    ("2026-03-27", "基金2025年年度报告"),
                ]
            )
        ]
        return Response({"result": rows, "pageHelp": {"pageNo": 1, "total": len(rows)}})

    def post(url, **kwargs):
        data = kwargs["data"]
        categories.append(data["category"])
        assert data["column"] == "fund" and data["stock"] == "159655,fund-manager"
        return Response(
            {
                "announcements": [
                    {
                        "secCode": "159655",
                        "announcementId": "fund-q3",
                        "announcementTitle": "合成159655基金2026年第三季度报告",
                        "announcementTime": datetime(
                            2026, 10, 27, tzinfo=ZoneInfo("Asia/Shanghai")
                        ).timestamp()
                        * 1000,
                        "adjunctUrl": "finalpage/2026-10-27/unit.PDF",
                    }
                ],
                "totalAnnouncement": 1,
                "totalRecordNum": 0,
                "totalpages": 0,
                "hasMore": False,
            }
        )

    monkeypatch.setattr(
        fetcher,
        "_authority_http",
        lambda: SimpleNamespace(get=get, post=post, categories=categories),
    )
    fetcher._us_announcements = Mock()
    fetcher._us_announcements.fetch.return_value = [
        {
            "stock_code": "VOO",
            "exchange": "sec",
            "date": "2026-08-27",
            "title": "N-CSRS · 基金半年度股东报告",
            "type": "N-CSRS",
            "url": "https://www.sec.gov/Archives/edgar/data/1/unit.htm",
        }
    ]
    return fetcher


def test_public_report_fetch_includes_fund_quarters_and_keeps_scope(
    source, monkeypatch
):
    monkeypatch.setattr(
        source,
        "_enrich_announcements",
        lambda *a: pytest.fail("fund report metadata must not call an LLM"),
    )
    monkeypatch.setattr(
        source,
        "_fetch_from_sse_backup",
        lambda *a: pytest.fail("funds must not use stock pages"),
    )
    result = source.get_recent_important_announcements(
        [508091, "510880", "159655", "VOO"], 7, 420
    )
    assert set(result) == {"508091", "510880", "159655", "VOO"}
    assert all(len(result[code]) == 1 for code in ("508091", "510880", "159655"))
    assert all(
        "季度报告" in result[code][0]["title"]
        for code in ("508091", "510880", "159655")
    )
    assert (
        result["VOO"] == []
    )  # Real disclosure date remains outside the ordinary window.
    assert source._authority_http().categories == [
        "category_ndbg_jjgg;category_bndbg_jjgg;category_jdbg_jjgg;"
    ]


def test_strict_fund_window_uses_the_same_official_adapter(source):
    states = source.fetch_announcements_status(
        ["508091", "159655"], date(2026, 10, 27), date(2026, 11, 3), observed_at=NOW
    )
    assert states["508091"]["available"] and states["508091"]["source"] == "sse"
    assert states["159655"]["available"] and states["159655"]["source"] == "cninfo"
    assert any("季度报告" in item["title"] for item in states["508091"]["items"])


def test_source_failure_remains_unavailable_without_stock_fallback(source, monkeypatch):
    def fail(*a, **k):
        raise ValueError("official directory incomplete")

    monkeypatch.setattr(source, "_authority_http", lambda: SimpleNamespace(get=fail))
    state = source.fetch_announcements_status(
        ["508091"], date(2026, 10, 27), date(2026, 11, 3), observed_at=NOW
    )["508091"]
    assert not state["available"] and state["items"] == []
    assert "incomplete" in state["error"]


@pytest.mark.parametrize("form", ["N-CSR", "N-CSR/A", "N-CSRS", "N-CSRS/A"])
def test_fund_shareholder_forms_are_financials_and_keep_published_cutoff(form):
    notices = {
        "VOO": [{"type": form, "title": form + " · 基金股东报告", "date": "2026-08-27"}]
    }
    assert build_daily_event_rows(notices, {}, None, as_of=NOW, config={}) == []
    rows = build_daily_event_rows(
        notices,
        {},
        None,
        as_of=NOW,
        config={"report": {"events": {"announcement_lookback_months": 3}}},
    )
    assert rows[0]["category_label"] == "财报业绩"


def test_public_source_to_shared_session_reaches_email_pdf_and_both_bots(
    source, monkeypatch, tmp_path
):
    codes = ["508091", "510880", "159655"]
    notices = source.get_recent_important_announcements(codes, 7, 420)
    config = {
        "stocks": codes,
        "report": {
            "strategy_display": "returns_only",
            "technical_signals": {"enabled": False},
        },
        "email": {"archive_dir": str(tmp_path)},
        "notification": {
            channel: {"enabled": True} for channel in ("email", "feishu", "telegram")
        },
    }
    frame = pd.DataFrame(
        [{"stock_code": code, "stock_name": "合成基金" + code} for code in codes]
    )
    session = SimpleNamespace(
        config=config,
        announcements=notices,
        placements={},
        dividend_events=[],
        created_at=NOW,
        stocks_data={code: object() for code in codes},
        errors=[],
        get_all_dataframe=lambda: frame,
        get_alerts_as_dicts=list,
    )
    tex = []

    def compile_pdf(path):
        tex.append(path.read_text(encoding="utf8"))
        return b"%PDF-offline-compiler-fixture"

    monkeypatch.setattr("src.notification.daily_pdf._compile_pdf", compile_pdf)
    pdf = generate_daily_pdf(
        session,
        frame,
        get_server_info=dict,
    )
    assert pdf and all(notices[code][0]["title"] in tex[0] for code in codes)
    bodies = {channel: [] for channel in ("email", "feishu", "telegram")}

    def send_email(self, subject, body, **kwargs):
        bodies["email"].append(body)
        assert kwargs["pdf_bytes"] == pdf
        return {"status": "sent"}

    monkeypatch.setattr(EmailNotifier, "_generate_daily_pdf", lambda *a: pdf)
    monkeypatch.setattr(EmailNotifier, "_send_email", send_email)
    for cls, channel in ((FeishuNotifier, "feishu"), (TelegramNotifier, "telegram")):

        def capture(self, title, body, _channel=channel, **kwargs):
            bodies[_channel].append(body)
            return {"status": "sent"}

        monkeypatch.setattr(cls, "_send", capture)
    receipt = NotifierManager(config).send_daily_report_from_session(session)
    assert receipt["status"] == "succeeded"
    assert set(receipt["deliveries"]) == set(bodies)
    for channel, parts in bodies.items():
        body = "\n".join(parts)
        assert "财报业绩" in body, channel
        for code in codes:
            assert notices[code][0]["title"] in body, channel
            assert notices[code][0]["url"] in body, channel
