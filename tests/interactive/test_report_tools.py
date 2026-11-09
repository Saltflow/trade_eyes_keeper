"""Real local report queue and receipt contracts, with all outbound work disabled."""

import hashlib
import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from src.core import report_execution
from src.core.execution import ExecutionRuntime, ExecutionStore
from src.interactive.assistant import report_tools
from src.interactive.assistant.data_tools import BusinessDataTools
from src.interactive.command_dispatcher import CommandExecutor
from src.interactive.command_parser import parse_command
from src.interactive.commands import handlers
from src.interactive.feishu_app import FeishuApp
from src.interactive.feishu_handler import handle_feishu_message
from src.interactive.telegram_bot import TelegramBot


@pytest.fixture
def reports(tmp_path, monkeypatch):
    monkeypatch.setenv("SKIP_EMAIL", "true")
    monkeypatch.setenv("SKIP_NOTIFICATIONS", "true")
    blocked = Mock(side_effect=AssertionError("External operations are disabled"))
    monkeypatch.setattr("requests.sessions.Session.request", blocked)
    monkeypatch.setattr("smtplib.SMTP", blocked)
    monkeypatch.setattr("smtplib.SMTP_SSL", blocked)
    config = {
        "stocks": ["fixture-a", "fixture-hk", "fixture-us", "fixture-sg"],
        "storage": {"data_dir": str(tmp_path / "data")},
        "email": {"enabled": True, "sender_password": "never-expose"},
        "notification": {"feishu": {"enabled": True}, "telegram": {"enabled": True}},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    runtime = ExecutionRuntime(ExecutionStore(report_execution.runtime_path(config)))
    # Keep the real queue and registration; only the worker launch is blocked.
    monkeypatch.setattr(runtime, "start_if_unowned", Mock(return_value=False))
    monkeypatch.setattr(report_tools, "get_runtime", lambda _: runtime)
    monkeypatch.setattr(handlers, "CONFIG_PATH", path)
    tools = report_tools.ReportTools(
        config, owner="owner", chat_id="chat", runtime=runtime, config_path=path
    )
    yield tools
    assert not blocked.called
    runtime.stop(drain=False)


def test_submission_keeps_whole_pool_channels_binding_and_stable_request(reports):
    first = reports.submit(request_id="feishu:event-1", force=True)
    same = reports.submit(request_id="feishu:event-1", force=True)
    other = reports.submit(request_id="feishu:event-2", force=True)
    assert first == same
    assert first["run_id"] != other["run_id"]
    assert first["request_id"] == "feishu:event-1"
    assert first["status"] == "queued"
    assert len(reports.runtime.store.list_runs()) == 2
    payload = reports.runtime.store.get(first["run_id"])["payload"]
    assert payload["force"] is True
    assert payload["config_snapshot"]["stocks"] == reports.config["stocks"]
    assert payload["config_snapshot"]["notification"] == reports.config["notification"]
    assert payload["config_snapshot"]["_report_context"] == {
        "owner": "owner",
        "chat_id": "chat",
    }
    assert "never-expose" not in json.dumps(payload)
    assert "config_snapshot" not in json.dumps(first)


def test_retry_after_committed_submission_receipt_failure_does_not_resubmit(
    reports, monkeypatch
):
    original_get = reports.get
    monkeypatch.setattr(reports, "get", Mock(side_effect=TimeoutError("reply lost")))
    with pytest.raises(TimeoutError):
        reports.submit(request_id="proposal:confirmed-1")
    run_id = reports.runtime.store.list_runs()[0]["id"]
    monkeypatch.setattr(reports, "get", original_get)
    assert reports.submit(request_id="proposal:confirmed-1")["run_id"] == run_id
    assert len(reports.runtime.store.list_runs()) == 1


@pytest.mark.parametrize("request_id", [None, "", "  ", 123])
def test_missing_request_identity_never_enqueues(reports, request_id):
    with pytest.raises(ValueError, match="request_id"):
        reports.submit(request_id=request_id)
    assert reports.runtime.store.list_runs() == []


@pytest.mark.parametrize("owner,chat", [(None, "chat"), ("", "chat"), ("owner", "")])
def test_missing_authenticated_identity_cannot_create_tools(reports, owner, chat):
    with pytest.raises(PermissionError, match="认证"):
        report_tools.ReportTools(
            reports.config, owner=owner, chat_id=chat, runtime=reports.runtime
        )


def test_receipts_deny_unbound_foreign_malformed_and_conflicting_bindings(reports):
    records = {}
    for name, snapshot in {
        "own_manual": {"_report_context": {"owner": "owner", "chat_id": "chat"}},
        "own_trigger": {"_trigger_context": {"owner": "owner", "chat_id": "chat"}},
        "unbound": {},
        "foreign_owner": {"_report_context": {"owner": "other", "chat_id": "chat"}},
        "foreign_chat": {"_trigger_context": {"owner": "owner", "chat_id": "other"}},
        "malformed": {"_report_context": None},
        "conflict": {
            "_report_context": {"owner": "owner", "chat_id": "chat"},
            "_trigger_context": {"owner": "other", "chat_id": "chat"},
        },
    }.items():
        records[name] = reports.runtime.store.submit(
            "reports.daily", {"config_snapshot": snapshot}
        )
    visible = {item["run_id"] for item in reports.runs()["items"]}
    assert visible == {records["own_manual"], records["own_trigger"]}
    for name, run_id in records.items():
        if name.startswith("own_"):
            assert reports.get(run_id)["run_id"] == run_id
        else:
            with pytest.raises(PermissionError):
                reports.get(run_id)
    with pytest.raises(PermissionError):
        reports.get("missing")
    wrong_job = reports.runtime.store.submit(
        "other.job",
        {"config_snapshot": {"_report_context": {"owner": "owner", "chat_id": "chat"}}},
    )
    with pytest.raises(PermissionError):
        reports.get(wrong_job)


def test_cross_identity_request_collision_cannot_return_someone_elses_run(reports):
    first = reports.submit(request_id="same-id")
    foreign = report_tools.ReportTools(
        reports.config,
        owner="other",
        chat_id="chat",
        runtime=reports.runtime,
        config_path=reports.config_path,
    )
    with pytest.raises(ValueError, match="different input"):
        foreign.submit(request_id="same-id")
    with pytest.raises(PermissionError):
        foreign.get(first["run_id"])
    assert len(reports.runtime.store.list_runs()) == 1


def test_list_pages_disclose_scan_limit_and_get_can_read_older_receipt(reports):
    runs = [reports.submit(request_id=f"event:{index}") for index in range(4)]
    first = reports.runs(limit=1, scan_limit=2)
    second = reports.runs(limit=1, offset=1, scan_limit=2)
    assert first["items"][0]["run_id"] == runs[3]["run_id"]
    assert second["items"][0]["run_id"] == runs[2]["run_id"]
    assert first["coverage_gaps"] == ["reports.daily:scan_limit_reached"]
    assert reports.get(runs[0]["run_id"])["run_id"] == runs[0]["run_id"]


def test_partial_and_unknown_receipts_preserve_failed_channels_without_replay(reports):
    run = reports.submit(request_id="event:partial")
    for status in ("partial", "unknown"):
        result = {
            "status": status,
            "reason": "delivery_not_confirmed",
            "deliveries": {
                "email": {
                    "status": "failed",
                    "reason": "smtp_rejected",
                    "smtp_code": 550,
                    "detail": "private@example.com",
                    "recipient": "private@example.com",
                },
                "feishu": {"status": "sent"},
                "telegram": {"status": "unknown"},
            },
        }
        with reports.runtime.store.connection() as db:
            db.execute(
                "UPDATE execution_runs SET status=?,result=?,stage=?,reason=? WHERE id=?",
                (
                    status,
                    json.dumps(result),
                    "finished",
                    "delivery_not_confirmed",
                    run["run_id"],
                ),
            )
        receipt = reports.get(run["run_id"])
        assert receipt["status"] == status
        assert receipt["stage"] == "finished"
        assert receipt["result"]["deliveries"]["email"] == {
            "status": "failed",
            "reason": "smtp_rejected",
            "smtp_code": 550,
        }
        assert "private@example.com" not in json.dumps(receipt)
        assert reports.submit(request_id="event:partial")["run_id"] == run["run_id"]
    assert len(reports.runtime.store.list_runs()) == 1


@pytest.mark.parametrize(
    "command,kind,report_id",
    [
        ("/daily", "daily", "morning_snapshot"),
        ("/brief afternoon", "brief", "afternoon_snapshot"),
    ],
)
def test_command_uses_same_submission_with_authenticated_event_id(
    reports, command, kind, report_id
):
    reply = Mock()
    executor = CommandExecutor(reply)
    for _ in range(2):
        executor.execute(
            "chat", parse_command(command), sender="owner", request_id="feishu:event-9"
        )
    runs = reports.runtime.store.list_runs()
    assert len(runs) == 1
    assert runs[0]["job_id"] == "reports." + kind
    assert runs[0]["payload"]["report_id"] == report_id
    assert runs[0]["payload"]["force"] is True
    assert runs[0]["request_id"] == "feishu:event-9"
    assert reply.call_count == 2
    assert all(runs[0]["id"] in call.args[1] for call in reply.call_args_list)
    assert reports.get(runs[0]["id"])["status"] == "queued"


def test_submission_failure_cannot_be_formatted_as_queued(reports, monkeypatch):
    monkeypatch.setattr(
        report_execution, "submit_report", Mock(side_effect=RuntimeError("queue down"))
    )
    response = handlers.handle_daily(
        sender="owner", chat_id="chat", request_id="event:failed"
    )
    assert response.startswith("❌")
    assert "请求已登记" not in response
    assert reports.runtime.store.list_runs() == []


@pytest.mark.parametrize("identified", [True, False])
def test_transport_identity_reaches_real_report_queue_or_prevents_submission(
    reports, identified
):
    settings = {
        "interactive": {
            "feishu": {
                "enabled": True,
                "app_id": "fixture",
                "app_secret": "fixture",
                "allowed_chat_ids": ["chat"],
            },
            "telegram": {
                "enabled": True,
                "bot_token": "fixture",
                "allowed_chat_ids": ["chat"],
            },
        }
    }
    app = FeishuApp(settings)
    app.send_message = Mock()
    telegram = TelegramBot(settings)
    telegram._send_message = Mock()
    event = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {
                "sender_type": "user",
                "sender_id": {"open_id": "owner"} if identified else {},
            },
            "message": {
                "chat_id": "chat",
                "message_id": "same-message",
                "content": json.dumps({"text": "/daily"}),
            },
        },
    }
    update = {
        "message": {
            "chat": {"id": "chat"},
            "message_id": 1,
            "text": "/daily",
            "from": {"id": "owner"} if identified else {},
        }
    }
    result = handle_feishu_message(app, event)
    telegram._process_update(update)
    if not identified:
        assert result == "ignored"
        assert reports.runtime.store.list_runs() == []
        return
    assert result == "accepted"
    assert handle_feishu_message(app, event) == "duplicate"
    telegram._process_update(update)
    runs = reports.runtime.store.list_runs()
    assert len(runs) == 2
    by_request = {run["request_id"]: run for run in runs}
    feishu = by_request["feishu:chat:same-message"]
    tg = by_request["telegram:chat:1"]
    assert reports.get(feishu["id"])["status"] == "queued"
    with pytest.raises(PermissionError):
        reports.get(tg["id"])
    assert tg["payload"]["config_snapshot"]["_report_context"] == {
        "owner": "telegram:owner",
        "chat_id": "chat",
    }


def _record_result(reports, result):
    run = reports.submit(request_id="event:archive")
    with reports.runtime.store.connection() as db:
        db.execute(
            "UPDATE execution_runs SET status=?,result=? WHERE id=?",
            (result.get("status", "unknown"), json.dumps(result), run["run_id"]),
        )
    return run["run_id"]


@pytest.mark.parametrize("location", ["email", "piece", "artifact"])
def test_owned_report_links_exact_archive_and_business_tools_read_body(
    reports, location
):
    archive = reports.root / "data/email_archive/actual.html"
    archive.parent.mkdir(parents=True)
    archive.write_text(
        "<html><body>真实归档正文 dividend evidence 01339</body></html>",
        encoding="utf-8",
    )
    unrelated = archive.with_name("same_day_other.html")
    unrelated.write_text("<body>must not guess this</body>", encoding="utf-8")
    receipt = {
        "status": "partial",
        "deliveries": {
            "email": {"status": "failed", "reason": "smtp_rejected"},
            "feishu": {"status": "sent"},
        },
    }
    if location == "email":
        receipt["deliveries"]["email"]["archive"] = str(archive)
        receipt["deliveries"]["email"]["archive_sha256"] = hashlib.sha256(
            archive.read_bytes()
        ).hexdigest()
    elif location == "piece":
        receipt["deliveries"]["email"]["pieces"] = [
            {
                "status": "failed",
                "archive": str(archive),
                "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            },
            {
                "status": "failed",
                "archive": str(archive),
                "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            },
        ]
    else:
        receipt["artifacts"] = {
            "report_html": str(archive),
            "report_html_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        }
    run_id = _record_result(reports, receipt)
    result = reports.get(run_id)
    assert result["status"] == "partial"
    assert result["result"]["deliveries"]["email"]["status"] == "failed"
    assert len(result["archives"]) == 1
    assert result["coverage_gaps"] == []
    linked = result["archives"][0]
    assert linked["source"] == "data/email_archive/actual.html"
    assert linked["data_id"].startswith("file_")
    assert "same_day_other" not in json.dumps(result)
    tools = BusinessDataTools(reports.root)
    content = tools.read(linked["data_id"])
    assert "dividend evidence 01339" in json.dumps(content, ensure_ascii=False)
    assert content["sha256"] == linked["sha256"]
    foreign = report_tools.ReportTools(
        reports.config,
        owner="other",
        chat_id="chat",
        runtime=reports.runtime,
        config_path=reports.config_path,
    )
    with pytest.raises(PermissionError):
        foreign.get(run_id)


def test_historical_report_without_path_reports_unknown_association(reports):
    archive = reports.root / "data/email_archive/plausible_same_day.html"
    archive.parent.mkdir(parents=True)
    archive.write_text("<body>not explicitly linked</body>", encoding="utf-8")
    run_id = _record_result(
        reports, {"status": "succeeded", "deliveries": {"email": {"status": "sent"}}}
    )
    result = reports.get(run_id)
    assert result["archives"] == []
    assert result["coverage_gaps"] == ["archive_reference_missing"]
    assert not (reports.root / "data/dataset_catalog/catalog.sqlite3").exists()


@pytest.mark.parametrize(
    "location,gap",
    [
        ("missing", "archive_file_missing"),
        ("escape", "archive_path_traversal"),
        ("outside", "archive_outside_project"),
        ("private", "archive_outside_trusted_roots"),
        ("policy", "archive_outside_catalog_policy"),
    ],
)
def test_unsafe_or_missing_archive_yields_gap_without_catalog_registration(
    reports, tmp_path, location, gap
):
    if location == "missing":
        value = "data/email_archive/missing.html"
    elif location == "escape":
        value = "data/email_archive/../../outside.html"
    elif location == "outside":
        outside = tmp_path.parent / (tmp_path.name + "-external")
        outside.mkdir()
        path = outside / "report.html"
        path.write_text("<body>outside catalog root</body>", encoding="utf-8")
        reports.config["email"]["archive_dir"] = str(outside)
        value = str(path)
    elif location == "private":
        path = reports.root / "config/private.html"
        path.parent.mkdir()
        path.write_text("<body>private</body>", encoding="utf-8")
        value = str(path)
    else:
        path = reports.root / "data/custom_archives/report.html"
        path.parent.mkdir(parents=True)
        path.write_text("<body>unsupported directory</body>", encoding="utf-8")
        reports.config["email"]["archive_dir"] = "data/custom_archives"
        value = str(path)
    run_id = _record_result(reports, {"deliveries": {"email": {"archive": value}}})
    result = reports.get(run_id)
    assert result["archives"] == []
    assert result["coverage_gaps"] == [gap]
    assert not (reports.root / "data/dataset_catalog/catalog.sqlite3").exists()


@pytest.mark.parametrize("linked", ["hardlink", "symlink_marker"])
def test_linked_archives_are_rejected_by_catalog_boundary(reports, monkeypatch, linked):
    archive = reports.root / "data/email_archive/report.html"
    archive.parent.mkdir(parents=True)
    if linked == "hardlink":
        original = reports.root / "original.html"
        original.write_text("<body>hardlinked bytes</body>", encoding="utf-8")
        os.link(original, archive)
    else:
        archive.write_text("<body>marked linked path</body>", encoding="utf-8")
        original_is_symlink = Path.is_symlink
        monkeypatch.setattr(
            Path,
            "is_symlink",
            lambda path: path == archive.parent or original_is_symlink(path),
        )
    run_id = _record_result(
        reports,
        {
            "deliveries": {
                "email": {
                    "archive": str(archive),
                    "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                }
            }
        },
    )
    result = reports.get(run_id)
    assert result["archives"] == []
    assert result["coverage_gaps"] == ["archive_unavailable_or_unsafe"]
    assert not (reports.root / "data/dataset_catalog/catalog.sqlite3").exists()


def test_archive_registration_failure_does_not_claim_readable_file(
    reports, monkeypatch
):
    archive = reports.root / "reports/custom_archive/report.html"
    archive.parent.mkdir(parents=True)
    archive.write_text("<body>already saved</body>", encoding="utf-8")
    reports.config["email"]["archive_dir"] = str(archive.parent)
    run_id = _record_result(
        reports,
        {
            "deliveries": {
                "email": {
                    "archive": str(archive),
                    "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                }
            }
        },
    )
    monkeypatch.setattr(
        report_tools.DatasetCatalog,
        "index_paths",
        lambda *_: {"files": [{"status": "failed"}]},
    )
    result = reports.get(run_id)
    assert result["archives"] == []
    assert result["coverage_gaps"] == ["archive_registration_failed"]
    assert archive.read_text(encoding="utf-8") == "<body>already saved</body>"


def test_paginated_list_does_not_index_archives_for_skipped_rows(reports, monkeypatch):
    runs = [reports.submit(request_id=f"page:{index}") for index in range(4)]
    archive_projection = Mock(return_value=([], ["archive_reference_missing"]))
    monkeypatch.setattr(reports, "_archives", archive_projection)
    page = reports.runs(limit=1, offset=2)
    assert page["items"][0]["run_id"] == runs[1]["run_id"]
    assert archive_projection.call_count == 1


@pytest.mark.parametrize("refresh_catalog", [False, True])
def test_report_archive_digest_rejects_changed_body_even_after_catalog_refresh(
    reports, refresh_catalog
):
    archive = reports.root / "data/email_archive/history.html"
    archive.parent.mkdir(parents=True)
    archive.write_text("<body>original report</body>", encoding="utf-8")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    run_id = _record_result(
        reports,
        {
            "status": "succeeded",
            "deliveries": {
                "email": {
                    "status": "sent",
                    "archive": str(archive),
                    "archive_sha256": digest,
                }
            },
        },
    )
    original = reports.get(run_id)
    assert original["archives"][0]["sha256"] == digest
    archive.write_text("<body>replacement report</body>", encoding="utf-8")
    if refresh_catalog:
        report_tools.DatasetCatalog(reports.root).index_paths(
            ["data/email_archive/history.html"]
        )
    result = reports.get(run_id)
    assert result["status"] == "succeeded"
    assert result["result"]["deliveries"]["email"]["status"] == "sent"
    assert result["archives"] == []
    assert result["coverage_gaps"] == ["archive_content_changed"]


def test_old_receipt_without_digest_is_not_certified_by_current_file_or_index(reports):
    archive = reports.root / "data/email_archive/legacy.html"
    archive.parent.mkdir(parents=True)
    archive.write_text("<body>unverified current bytes</body>", encoding="utf-8")
    report_tools.DatasetCatalog(reports.root).index_paths(
        ["data/email_archive/legacy.html"]
    )
    run_id = _record_result(
        reports,
        {
            "status": "succeeded",
            "deliveries": {"email": {"status": "sent", "archive": str(archive)}},
        },
    )
    result = reports.get(run_id)
    assert result["archives"] == []
    assert result["coverage_gaps"] == ["archive_version_unverified"]
    assert result["result"]["deliveries"]["email"]["status"] == "sent"


def test_archive_verification_streams_bytes_instead_of_reading_entire_file(
    reports, monkeypatch
):
    archive = reports.root / "reports/large_archive.html"
    archive.parent.mkdir(parents=True)
    content = b"<body>" + b"synthetic archived evidence " * 80_000 + b"</body>"
    digest = hashlib.sha256(content).hexdigest()
    archive.write_bytes(content)
    run_id = _record_result(
        reports,
        {
            "status": "succeeded",
            "deliveries": {
                "email": {
                    "status": "sent",
                    "archive": str(archive),
                    "archive_sha256": digest,
                }
            },
        },
    )
    monkeypatch.setattr(
        Path, "read_bytes", Mock(side_effect=AssertionError("unbounded file read"))
    )
    result = reports.get(run_id)
    assert result["archives"][0]["sha256"] == digest
    assert result["coverage_gaps"] == []
