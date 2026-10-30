"""Only version rendering; no quotes, strategy runs, SMTP or Feishu connections."""

from types import SimpleNamespace

import pandas as pd

from src.core import build_info
from src.notification import report_builders
from src.notification.email_notifier import EmailNotifier


def test_daily_html_keeps_visible_and_escaped_build_label(monkeypatch):
    notifier = EmailNotifier({})
    label = "v1.18.0 · aaaaaaa · 构建 12345678"
    monkeypatch.setattr(
        notifier,
        "_get_server_info",
        lambda: {
            "release_label": label,
            "hostname": "<untrusted>",
            "ip_address": "local",
        },
    )
    body = notifier._build_mobile_daily_email_body([], pd.DataFrame())
    assert "<strong>版本 " + label + "</strong>" in body
    assert "&lt;untrusted&gt;" in body
    assert "{release_label}" not in body


def test_brief_renders_same_shared_version_without_os_or_provider_probes(monkeypatch):
    label = "v1.18.0 · aaaaaaa · 构建 12345678"
    monkeypatch.setattr(build_info, "build_label", lambda: label)
    monkeypatch.setattr(
        report_builders, "get_brief_dataframe", lambda _: pd.DataFrame()
    )
    notifier = EmailNotifier({})
    monkeypatch.setattr(notifier, "_build_ref_portfolio_html", lambda *_: "")
    _, body = notifier._prepare_brief_report(SimpleNamespace(), {"label": "简报"})
    assert "版本 " + label in body
    assert "{release_label}" not in body


def test_report_worker_freezes_version_before_calling_business(tmp_path, monkeypatch):
    import json

    from src.core import report_execution as reports
    from src.core.execution import RunResult

    events = []
    monkeypatch.setattr(reports, "get_build_info", lambda: events.append("version"))
    monkeypatch.setattr(reports.ConfigStore, "load_runtime", lambda _: {})
    application = SimpleNamespace(
        setup_logging=lambda _: None,
        run_daily_task=lambda **_: events.append("business") or RunResult(),
    )
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps({"kind": "daily", "config_path": "ignored", "config_snapshot": {}})
    )
    assert (
        reports.run_report_worker(
            "daily", request, tmp_path / "result.json", application=application
        )
        == 0
    )
    assert events == ["version", "business"]


def test_service_status_publishes_loaded_build_not_mutable_disk_state(
    tmp_path, monkeypatch
):
    from src.core import runtime_service as runtime

    current = {"version": "1.18.0", "verified": True, "state": "verified"}
    monkeypatch.setattr(runtime, "get_build_info", lambda: dict(current))
    service = runtime.RuntimeService(
        {"storage": {"data_dir": str(tmp_path)}}, scheduling=False
    )
    current["version"] = "next version on disk"
    service._write_status("stopped")
    assert (
        runtime.read_service_status(service.config)["build_info"]["version"] == "1.18.0"
    )


def test_bot_help_uses_same_verified_identity(monkeypatch):
    from src.interactive.commands import handlers

    label = "v1.18.0 · aaaaaaa · 构建 12345678"
    monkeypatch.setattr(build_info, "build_label", lambda: label)
    assert "<code>" + label + "</code>" in handlers.handle_help()


def test_pdf_renders_the_same_build_and_consumes_template_placeholder(monkeypatch):
    from src.notification import daily_pdf

    label = "v1.18.0 · aaaaaaa · 构建 12345678"
    captured = []

    def compile_stub(path):
        captured.append(path.read_text("utf-8"))
        return b"%PDF-unit-fixture"

    monkeypatch.setattr(daily_pdf, "_compile_pdf", compile_stub)
    session = SimpleNamespace(config={"stocks": []})
    result = daily_pdf.generate_daily_pdf(
        session,
        [],
        None,
        {},
        pd.DataFrame(),
        chart_deviation_timeline=lambda *a, **k: None,
        pick_best_anchor=lambda *_: None,
        get_server_info=lambda: {"hostname": "fixture", "release_label": label},
    )
    assert result == b"%PDF-unit-fixture"
    assert label in captured[0] and "\\VAR{release_label}" not in captured[0]


def test_deployment_hook_uses_explicit_pushed_commit_not_remote_head(monkeypatch):
    import shlex

    import pytest

    import ci_cd_deploy as deploy

    monkeypatch.setattr(deploy, "REMOTE_DIR", "/root/path with spaces")
    args = shlex.split(deploy._build_release_manifest_command("a" * 40))
    assert args[:3] == ["cd", "/root/path with spaces", "&&"]
    assert args[-3:] == ["--write", "--commit", "a" * 40]
    assert "git" not in args
    with pytest.raises(ValueError):
        deploy._build_release_manifest_command("master;unexpected")
