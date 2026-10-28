"""Every daily channel shows the same events and truthful readiness status."""

from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from src.notification.daily_pdf import generate_daily_pdf
from src.notification.feishu_notifier import FeishuNotifier
from src.notification.report_builders import (
    build_daily_event_rows,
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
        announcements={"601088": [{
            "date": "2026-10-21", "title": "权益分派实施公告",
            "url": "https://example.invalid/notice?a=1&b=2",
        }]},
        placements={},
        dividend_events=[{
            "code": "601088", "status": "未除权", "ex_date": "2026-10-26",
            "payment_date": "2026-10-26", "cash_per_share": 0.98,
            "currency": "CNY",
        }],
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
        session, [], None, {"a_share": {"total_return": 8.0}},
        session.get_all_dataframe(), chart_deviation_timeline=lambda *a, **k: None,
        pick_best_anchor=lambda *_: None, get_server_info=lambda: {"hostname": "test"},
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
        session, [], None, {}, session.get_all_dataframe(),
        chart_deviation_timeline=lambda *a, **k: None,
        pick_best_anchor=lambda *_: None, get_server_info=dict,
    )
    assert len(commands) == (2 if final_pass_fails else 3)
    assert "-no-pdf" in commands[0] and "-no-pdf" in commands[1]
    if not final_pass_fails:
        assert commands[2][0] == "xdvipdfmx"
    assert result == (None if final_pass_fails else b"%PDF-final-pass")


@pytest.mark.parametrize("status", ["blocked", "failed", "not_run"])
def test_pdf_cannot_claim_valid_strategy_when_market_not_ready(
    monkeypatch, session, status
):
    session.signal_scan.group_outcomes = {"hk": {"status": status}}
    tex = pdf_source(monkeypatch, session)
    assert "数据未就绪" in tex and "港股：" + status.replace("_", r"\_") in tex
    assert "策略有效" not in tex


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
    method = "_build_report_sections" if kind == "feishu" else "_build_daily_sections"
    monkeypatch.setattr(notifier, method, lambda *_a, **_kw: [])
    if kind == "feishu":
        monkeypatch.setattr(
            "src.notification.feishu_notifier._build_ref_portfolio_markdown",
            lambda *_: "",
        )
    else:
        monkeypatch.setattr(notifier, "_build_ref_portfolio_text", lambda *_: "")
    notifier.send_daily_report_from_session(session)
    body = "\n".join(row[1] for row in sent)
    assert "权益分派实施公告" in body and "派息 2026-10-26" in body
    sent.clear()
    notifier.send_from_session(session)
    assert not any("派息 2026-10-26" in row[1] for row in sent)


def test_event_rows_keep_finite_missing_amounts_and_safe_links(session):
    session.placements = {"000001": {"issue_num": 1e8, "issue_price": None}}
    rows = build_daily_event_rows(
        session.announcements, session.placements, session.get_all_dataframe(),
        session.dividend_events,
    )
    assert len(rows) == 3
    assert rows[0]["kind"] == "未除权"
    assert "1.00亿股" in rows[1]["title"]
    assert "定增价 —" in rows[1]["title"]
    assert rows[2]["url"] == "https://example.invalid/notice?a=1&b=2"


def test_readiness_reuses_execution_contract_for_missing_and_failed_data(session):
    session.config["stocks"].append({"code": "00700"})
    session.errors = ["source unavailable"]
    result = report_data_readiness(session)
    assert result["status"] == "not_ready"
    assert result["missing_codes"] == ["00700"]
    assert result["error_count"] == 1
