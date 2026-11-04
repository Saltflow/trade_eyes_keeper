"""Every daily channel shows the same events and truthful readiness status."""

from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from src.notification.daily_pdf import generate_daily_pdf
from src.notification.email_notifier import EmailNotifier
from src.notification.feishu_notifier import FeishuNotifier
from src.notification.report_builders import (
    build_daily_event_rows,
    group_daily_event_rows,
    report_data_readiness,
)
from src.notification.telegram_notifier import TelegramNotifier


@pytest.fixture
def session():
    frame = pd.DataFrame([{"stock_code": "601088", "stock_name": "中国神华"}])
    return SimpleNamespace(
        config={"stocks": ["601088"]},
        stocks_data={"601088": object()},
        errors=[],
        evaluation_reports={},
        reference_portfolio_status={},
        announcements={
            "601088": [
                {
                    "date": "2026-10-21",
                    "title": "权益分派实施公告",
                    "url": "https://example.invalid/notice?a=1&b=2",
                }
            ]
        },
        placements={},
        dividend_events=[
            {
                "code": "601088",
                "status": "未除权",
                "ex_date": "2026-10-26",
                "payment_date": "2026-10-26",
                "cash_per_share": 0.98,
                "currency": "CNY",
            }
        ],
        signal_scan=SimpleNamespace(alerts=[], group_outcomes={}),
        get_all_dataframe=lambda: frame,
        get_alerts_as_dicts=list,
    )


def pdf_source(monkeypatch, session):
    monkeypatch.setattr(
        "src.notification.daily_pdf._converter_probe_preamble", lambda: ""
    )
    captured = []

    def compile_stub(args, **_kwargs):
        path = Path(args[-1])
        if args[0] == "xelatex":
            captured.append(path.read_text("utf-8"))
            path.with_suffix(".xdv").write_bytes(b"current-pass XDV")
        else:
            assert args[0] == "xdvipdfmx"
            Path(args[args.index("-o") + 1]).write_bytes(b"%PDF-unit-test")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("subprocess.run", compile_stub)
    result = generate_daily_pdf(
        session,
        session.get_all_dataframe(),
        get_server_info=lambda: {"hostname": "test"},
    )
    assert result == b"%PDF-unit-test"
    return captured[-1]


def test_pdf_shows_implementation_notice_and_payment_dates(monkeypatch, session):
    tex = pdf_source(monkeypatch, session)
    assert "权益分派实施公告" in tex
    assert "除权 2026-10-26" in tex and "派息 2026-10-26" in tex
    assert "CNY 0.9800" in tex
    assert "\\VAR{" not in tex
    assert "\r" not in tex


def test_pdf_includes_hk_announcements_alongside_a_shares(monkeypatch, session):
    session.announcements["00700"] = [
        {
            "date": "2026-10-21",
            "title": "截至六月三十日止六個月業績公佈",
            "url": "https://www1.hkexnews.hk/listedco/notice_c.pdf",
            "exchange": "hkex",
        }
    ]
    tex = pdf_source(monkeypatch, session)
    assert "权益分派实施公告" in tex
    assert "截至六月三十日止六個月業績公佈" in tex
    assert "00700" in tex


@pytest.mark.parametrize("final_pass_fails", [False, True])
def test_pdf_keeps_cross_reference_pass_without_duplicate_pdf_conversion(
    monkeypatch, session, final_pass_fails
):
    monkeypatch.setattr(
        "src.notification.daily_pdf._converter_probe_preamble", lambda: ""
    )
    commands = []

    def compile_stub(args, **_kwargs):
        commands.append(args)
        directory = Path(args[-1]).parent
        if len(commands) == 1:
            (directory / "report.aux").write_text("resolved-references", "utf-8")
        else:
            assert (directory / "report.aux").read_text("utf-8") == (
                "resolved-references"
            )
        failed = len(commands) == 2 and final_pass_fails
        if args[0] == "xelatex" and not failed:
            (directory / "report.xdv").write_bytes(b"current-pass XDV")
        elif args[0] == "xdvipdfmx":
            assert "-q" in args and "-E" in args
            Path(args[args.index("-o") + 1]).write_bytes(b"%PDF-final-pass")
        return SimpleNamespace(returncode=int(failed), stderr="fixture failure")

    monkeypatch.setattr("subprocess.run", compile_stub)
    result = generate_daily_pdf(
        session,
        session.get_all_dataframe(),
        get_server_info=dict,
    )
    assert len(commands) == (2 if final_pass_fails else 3)
    assert "-no-pdf" in commands[0] and "-no-pdf" in commands[1]
    if not final_pass_fails:
        assert commands[2][0] == "xdvipdfmx"
    assert result == (None if final_pass_fails else b"%PDF-final-pass")


def test_pdf_escapes_untrusted_announcement_tex(monkeypatch, session):
    session.announcements["601088"][0]["title"] = r"5% & {\input{ignored}}"
    tex = pdf_source(monkeypatch, session)
    assert r"\input{ignored}" not in tex
    assert r"5\% \& \{\textbackslash{}input\{ignored\}\}" in tex


@pytest.mark.parametrize("kind", ["feishu", "telegram"])
def test_daily_messages_include_events_but_alert_only_keeps_scope(
    monkeypatch, session, kind
):
    notifier = FeishuNotifier({}) if kind == "feishu" else TelegramNotifier({})
    sent = []

    def capture(title, body, **_kwargs):
        sent.append((title, body))
        return {"status": "sent"}

    monkeypatch.setattr(notifier, "_send", capture)
    notifier.send_daily_report_from_session(session)
    body = "\n".join(row[1] for row in sent)
    assert "权益分派实施公告" in body and "派息 2026-10-26" in body
    sent.clear()
    notifier.send_from_session(session)
    assert any("派息 2026-10-26" in row[1] for row in sent)


def test_event_rows_keep_finite_missing_amounts_and_safe_links(session):
    session.placements = {"000001": {"issue_num": 1e8, "issue_price": None}}
    rows = build_daily_event_rows(
        session.announcements,
        session.placements,
        session.get_all_dataframe(),
        session.dividend_events,
    )
    assert len(rows) == 3
    assert rows[0]["kind"] == "未除权"
    placement = next(row for row in rows if row["kind"] == "未解禁定增")
    assert "1.00亿股" in placement["title"]
    assert "定增价 —" in placement["title"]
    assert (
        next(row for row in rows if row["url"])["url"]
        == "https://example.invalid/notice?a=1&b=2"
    )


@pytest.mark.parametrize("channel", ["rows", "email", "pdf", "feishu", "telegram"])
def test_pending_items_stay_pinned_before_categorized_notices(
    monkeypatch, session, tmp_path, channel
):
    session.created_at = "2026-11-04"
    session.dividend_events.append(
        {
            "code": "601088",
            "status": "已除权未派息",
            "ex_date": "2026-09-01",
            "payment_date": "2026-11-06",
            "cash_per_share": 0.1234,
        }
    )
    session.placements = {"601088": {"unlock_date": "2028-01-10"}}
    for title in ("重大资产重组公告", "第三季度报告", "股份回购公告"):
        session.announcements["601088"].append(
            {
                "date": "2026-11-03",
                "title": title,
                "url": "https://example.invalid/" + title,
            }
        )
    expected = [
        "已除权未派息",
        "未除权",
        "未解禁定增",
        "重组并购与重大事项",
        "财报业绩",
        "分红派息",
        "股权融资与解禁",
    ]
    frame = session.get_all_dataframe()
    if channel == "rows":
        rows = build_daily_event_rows(
            session.announcements,
            session.placements,
            frame,
            session.dividend_events,
            as_of=session.created_at,
        )
        assert [group["category"] for group in group_daily_event_rows(rows)] == expected
        assert [row["kind"] for row in rows[:3]] == expected[:3]
        return
    if channel == "email":
        body = EmailNotifier({"email": {"archive_dir": str(tmp_path)}})
        body = body._build_daily_events_section(
            session.announcements,
            session.placements,
            frame,
            session.dividend_events,
            report_as_of=session.created_at,
        )
    elif channel == "pdf":
        body = pdf_source(monkeypatch, session)
    else:
        notifier = FeishuNotifier({}) if channel == "feishu" else TelegramNotifier({})
        sent = []
        monkeypatch.setattr(
            notifier,
            "_send",
            lambda title, body, **kwargs: sent.append(body) or {"status": "sent"},
        )
        notifier.send_daily_report_from_session(session)
        body = "\n".join(sent)
    positions = [body.index(label) for label in expected]
    assert positions == sorted(positions)
    assert "0.1234" in body and "2028-01-10" in body
    assert all(item["title"] in body for item in session.announcements["601088"])


def test_readiness_reuses_execution_contract_for_missing_and_failed_data(session):
    session.config["stocks"].append({"code": "00700"})
    session.errors = ["source unavailable"]
    result = report_data_readiness(session)
    assert result["status"] == "not_ready"
    assert result["missing_codes"] == ["00700"]
    assert result["error_count"] == 1
