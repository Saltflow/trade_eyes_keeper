"""Report entrypoints share execution; all business and transports are isolated."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from src.core import report_execution as reports
from src.core.execution import ExecutionRuntime, ExecutionStore, RunResult
from src.core.schedule_manager import ScheduleManager
from src.interactive.commands import handlers
from src.notification.manager import NotifierManager


@pytest.fixture
def profile(tmp_path):
    config = {
        "stocks": ["601088", "00728", "VOO"],
        "storage": {"data_dir": str(tmp_path / "data")},
        "scheduler": {"timezone": "Asia/Shanghai", "run_time": "19:00"},
        "email": {"sender_password": "yaml-test-secret"},
        "notification": {"feishu": {"webhook_url": "https://secret.invalid/hook"}},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return config, path


def test_runtime_path_uses_existing_profile_conversation_database(profile):
    config, _ = profile
    assert reports.runtime_path(config) == (
        Path(config["storage"]["data_dir"]) / "runtime" / "feishu_assistant.sqlite3"
    )


def test_settings_snapshot_pins_business_without_credentials(profile):
    config, _ = profile
    snapshot = reports._settings_snapshot(config)
    serialized = json.dumps(snapshot)
    assert "yaml-test-secret" not in serialized
    assert "secret.invalid" not in serialized
    assert snapshot["stocks"] == config["stocks"]
    current = {
        **config,
        "stocks": ["NEWER"],
        "email": {"sender_password": "new-secret"},
    }
    restored = reports._restore_secrets(snapshot, current)
    assert restored["stocks"] == config["stocks"]
    assert restored["email"]["sender_password"] == "new-secret"


def test_snapshot_retains_token_budgets_while_credentials_refresh():
    before = {"max_tokens": 100, "token_budget": 200, "access_token": "secret-old"}
    after = {"max_tokens": 300, "token_budget": 400, "access_token": "secret-new"}
    restored = reports._restore_secrets(reports._settings_snapshot(before), after)
    assert restored == {
        "max_tokens": 100,
        "token_budget": 200,
        "access_token": "secret-new",
    }


def test_acknowledged_incomplete_report_is_partial_and_keeps_delivery_receipt():
    import main

    session = SimpleNamespace(
        session_id="coverage-test",
        config={"stocks": ["601088", "00728"]},
        stocks_data={"601088": object()},
        errors=[],
        signal_scan=None,
    )
    result = main._report_delivery_result(
        {
            "status": "succeeded",
            "reason": "delivery_acknowledged",
            "deliveries": {"email": {"status": "sent"}},
        },
        session,
    )
    assert result.status == "partial"
    assert result.reason.startswith("data_not_ready")
    assert result.artifacts["data_readiness"]["missing_codes"] == ["00728"]
    assert result.deliveries["email"]["status"] == "sent"


def test_standalone_uses_same_executor_and_persists_result(profile, monkeypatch):
    config, path = profile
    received = []

    def execute(payload, context):
        received.append((payload, context.run_id))
        return RunResult("succeeded", artifacts={"report": "controlled"})

    monkeypatch.setattr(reports, "_report_handler", execute)
    record = reports.submit_report(
        config,
        kind="brief",
        report_id="afternoon_snapshot",
        force=True,
        wait=True,
        source="cli",
        config_path=path,
        timeout=5,
    )
    assert record["status"] == "succeeded"
    assert received[0][0]["force"] is True
    assert received[0][0]["report_id"] == "afternoon_snapshot"
    assert received[0][0]["config_snapshot"]["stocks"] == config["stocks"]
    assert "yaml-test-secret" not in json.dumps(record)
    assert reports.get_runtime(reports.runtime_path(config)).running is False


def test_schedule_request_dedupes_but_distinct_manual_requests_do_not(
    profile, monkeypatch
):
    config, path = profile
    runtime = ExecutionRuntime(ExecutionStore(reports.runtime_path(config)))
    runtime.store.has_live_owner = lambda: True
    monkeypatch.setattr(reports, "_report_handler", lambda *_: RunResult())
    common = {"config_path": path, "runtime": runtime}
    first = reports.submit_report(config, request_id="schedule:daily:fixed", **common)
    same = reports.submit_report(config, request_id="schedule:daily:fixed", **common)
    manual_a = reports.submit_report(config, source="bot", **common)
    manual_b = reports.submit_report(config, source="bot", **common)
    assert first == same
    assert len({first, manual_a, manual_b}) == 3
    runtime.disable("reports")
    reports.register_report_jobs(runtime)
    assert runtime.module_status("reports")["enabled"] == 0


def test_live_external_owner_is_not_competed_with(profile):
    config, path = profile
    runtime = Mock()
    runtime.running = False
    runtime.is_registered.return_value = True
    runtime.module_status.return_value = {"enabled": True, "version": "previous"}
    runtime.store.path = reports.runtime_path(config)
    runtime.store.has_live_owner.return_value = True
    runtime.submit.return_value = "external-run"
    runtime.wait.return_value = {"id": "external-run", "status": "succeeded"}
    reports.submit_report(config, config_path=path, runtime=runtime, wait=True)
    runtime.start_if_unowned.assert_not_called()
    runtime.stop.assert_not_called()


def test_waiter_takes_over_after_previous_one_shot_owner_exits(profile):
    config, path = profile
    runtime = Mock()
    runtime.running = False
    runtime.is_registered.return_value = True
    runtime.module_status.return_value = {"enabled": True, "version": "previous"}
    runtime.store.path = reports.runtime_path(config)
    runtime.store.has_live_owner.side_effect = [True, True, False]
    runtime.submit.return_value = "waiting-run"
    runtime.wait.side_effect = [TimeoutError(), {"status": "succeeded"}]
    reports.submit_report(config, config_path=path, runtime=runtime, wait=True)
    runtime.start_if_unowned.assert_called_once()
    runtime.stop.assert_called_once_with(drain=True)


def test_report_concurrency_hold_covers_actual_business_end(profile, monkeypatch):
    config, path = profile
    started = threading.Event()
    release = threading.Event()
    calls = []

    def execute(payload, _context):
        calls.append(payload["kind"])
        started.set()
        assert release.wait(5)
        return RunResult()

    monkeypatch.setattr(reports, "_report_handler", execute)
    runtime = ExecutionRuntime(
        ExecutionStore(reports.runtime_path(config)), lane_workers={"report": 2}
    )
    reports.register_report_jobs(runtime)
    first = reports.submit_report(config, runtime=runtime, config_path=path)
    assert started.wait(5)
    second = reports.submit_report(
        config, kind="brief", runtime=runtime, config_path=path
    )
    try:
        runtime.tick()
        assert runtime.get_run(first)["status"] == "running"
        assert runtime.get_run(second)["status"] == "queued"
        assert calls == ["daily"]
        release.set()
        assert runtime.wait(first, 5)["status"] == "succeeded"
        assert runtime.wait(second, 5)["status"] == "succeeded"
    finally:
        release.set()
        runtime.stop(drain=True)


@pytest.mark.parametrize(
    "receipt,exit_code,status,reason",
    [
        ({"status": "succeeded"}, 0, "succeeded", ""),
        (
            {"status": "skipped", "reason": "market_closed"},
            0,
            "skipped",
            "market_closed",
        ),
        ({"status": "failed"}, 1, "failed", ""),
        ({"status": "partial"}, 1, "partial", ""),
        ({"status": "unknown"}, 1, "unknown", ""),
        ({}, 0, "unknown", "report_worker_receipt_missing"),
        ({"status": "succeeded"}, 1, "unknown", "report_worker_receipt_exit_mismatch"),
        (None, 0, "unknown", "report_worker_receipt_missing"),
    ],
)
def test_child_requires_explicit_matching_receipt(
    tmp_path, monkeypatch, receipt, exit_code, status, reason
):
    context = SimpleNamespace(
        run_id="test-run",
        progress=Mock(),
        mark_side_effect=Mock(),
        cancel_event=threading.Event(),
    )
    commands = []

    def spawn(command, **_kwargs):
        commands.append(command)
        if receipt is not None:
            result_path = Path(command[command.index("--report-result") + 1])
            reports._write_json(result_path, receipt)
        return SimpleNamespace(poll=lambda: exit_code, wait=lambda: exit_code)

    monkeypatch.setattr(reports.subprocess, "Popen", spawn)
    result = reports._report_handler(
        {"kind": "daily", "artifact_root": str(tmp_path)}, context
    )
    assert (result.status, result.reason) == (status, reason)
    assert "--report-worker" in commands[0]
    assert "--once" not in commands[0]
    context.mark_side_effect.assert_called_once()


def test_child_is_reaped_on_cancellation(tmp_path, monkeypatch):
    context = SimpleNamespace(
        run_id="cancel",
        progress=Mock(),
        mark_side_effect=Mock(),
        cancel_event=threading.Event(),
    )
    context.cancel_event.set()
    process = SimpleNamespace(poll=lambda: None, returncode=15)
    monkeypatch.setattr(reports.subprocess, "Popen", lambda *_a, **_k: process)
    stop = Mock()
    monkeypatch.setattr(reports, "_stop_child", stop)
    result = reports._report_handler(
        {"kind": "daily", "artifact_root": str(tmp_path)}, context
    )
    assert result.status == "unknown"
    stop.assert_called_once_with(process)


def test_completed_child_wakes_report_wait_without_a_poll_sleep(tmp_path, monkeypatch):
    context = SimpleNamespace(
        run_id="completion-wakeup",
        progress=Mock(),
        mark_side_effect=Mock(),
        cancel_event=threading.Event(),
    )
    context.cancel_event.wait = Mock(
        side_effect=AssertionError("completed child must wake the report waiter")
    )
    waits = []

    class Child:
        returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            waits.append(timeout)
            self.returncode = 0
            return 0

    def spawn(command, **_kwargs):
        context.mark_side_effect.assert_called_once_with("report_starting")
        result_path = Path(command[command.index("--report-result") + 1])
        reports._write_json(result_path, {"status": "succeeded"})
        return Child()

    monkeypatch.setattr(reports.subprocess, "Popen", spawn)
    result = reports._report_handler(
        {"kind": "daily", "artifact_root": str(tmp_path)}, context
    )
    assert result.status == "succeeded"
    assert waits == [0.25, None]
    context.cancel_event.wait.assert_not_called()
    context.progress.assert_not_called()


def test_report_wait_timeout_still_honors_cancellation(tmp_path, monkeypatch):
    context = SimpleNamespace(
        run_id="cancel-during-wait",
        progress=Mock(),
        mark_side_effect=Mock(),
        cancel_event=threading.Event(),
    )

    class Child:
        returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            assert timeout == 0.25
            context.cancel_event.set()
            raise reports.subprocess.TimeoutExpired("controlled-child", timeout)

    process = Child()
    monkeypatch.setattr(reports.subprocess, "Popen", lambda *_a, **_k: process)
    stop = Mock()
    monkeypatch.setattr(reports, "_stop_child", stop)
    result = reports._report_handler(
        {"kind": "daily", "artifact_root": str(tmp_path)}, context
    )
    assert (result.status, result.reason) == (
        "unknown",
        "report_interrupted_after_start",
    )
    stop.assert_called_once_with(process)


def test_report_wait_failure_reaps_child_and_preserves_failure(tmp_path, monkeypatch):
    context = SimpleNamespace(
        run_id="wait-failure",
        progress=Mock(),
        mark_side_effect=Mock(),
        cancel_event=threading.Event(),
    )
    failure = OSError("controlled child wait failure")
    process = SimpleNamespace(
        poll=lambda: None,
        wait=Mock(side_effect=failure),
    )
    monkeypatch.setattr(reports.subprocess, "Popen", lambda *_a, **_k: process)
    stop = Mock()
    monkeypatch.setattr(reports, "_stop_child", stop)
    with pytest.raises(OSError) as caught:
        reports._report_handler(
            {"kind": "daily", "artifact_root": str(tmp_path)}, context
        )
    assert caught.value is failure
    stop.assert_called_once_with(process)


@pytest.mark.parametrize("kind", ["daily", "brief"])
def test_internal_worker_calls_business_directly(profile, tmp_path, monkeypatch, kind):
    import main

    config, path = profile
    monkeypatch.setattr(main, "setup_logging", lambda _config: None)
    daily = Mock(return_value=RunResult("succeeded"))
    brief = Mock(return_value=RunResult("succeeded"))
    monkeypatch.setattr(main, "run_daily_task", daily)
    monkeypatch.setattr(main, "run_brief_report", brief)
    monkeypatch.setattr(
        reports, "submit_report", Mock(side_effect=AssertionError("recursive submit"))
    )
    payload = tmp_path / "request.json"
    result = tmp_path / "result.json"
    reports._write_json(
        payload,
        {
            "kind": kind,
            "report_id": "custom",
            "force": True,
            "config_path": str(path),
            "config_snapshot": reports._settings_snapshot(config),
        },
    )
    assert reports.run_report_worker(kind, payload, result) == 0
    called = daily if kind == "daily" else brief
    assert called.call_args.kwargs["force"] is True
    assert called.call_args.kwargs["config"]["stocks"] == config["stocks"]
    assert json.loads(result.read_text())["status"] == "succeeded"


@pytest.mark.parametrize("flag,kind", [("--once", "daily"), ("--brief", "brief")])
def test_public_cli_submits_and_propagates_failure(profile, monkeypatch, flag, kind):
    import main

    config, path = profile
    monkeypatch.setattr(main, "load_config", lambda *_: config)
    monkeypatch.setattr(main, "setup_logging", lambda _config: Mock())
    submit = Mock(return_value={"status": "partial"})
    monkeypatch.setattr(reports, "submit_report", submit)
    assert main.main([flag, "--config", str(path)]) == 1
    assert submit.call_args.kwargs["kind"] == kind
    assert submit.call_args.kwargs["wait"] is True


def test_bot_queues_forced_report_without_popen(profile, monkeypatch):
    _config, path = profile
    monkeypatch.setattr(handlers, "CONFIG_PATH", path)
    submit = Mock(return_value="queued")
    monkeypatch.setattr(reports, "submit_report", submit)
    assert "已触发" in handlers.handle_daily()
    assert "已触发" in handlers.handle_brief("afternoon_snapshot")
    assert len(submit.call_args_list) == 2
    for call in submit.call_args_list:
        assert call.kwargs["force"] is True
        assert call.kwargs["source"] == "bot"


def test_scheduled_callbacks_only_submit_and_obsolete_generation_is_ignored(
    profile, monkeypatch
):
    config, path = profile
    submit = Mock(return_value="scheduled")
    monkeypatch.setattr(reports, "submit_report", submit)
    manager = ScheduleManager(config, path, execution_runtime="shared")
    callback = manager._job_runner("daily", ["--once"])
    assert callback() == "scheduled"
    first = submit.call_args.kwargs
    assert first["runtime"] == "shared"
    assert first["request_id"].startswith("schedule:daily:")
    assert callback() == "scheduled"
    assert submit.call_args.kwargs["request_id"] == first["request_id"]
    manager.stop()
    assert callback() is None
    assert submit.call_count == 2


@pytest.mark.parametrize(
    "a,b,status",
    [
        (None, None, "unknown"),
        ({"status": "sent"}, {"status": "sent"}, "succeeded"),
        ({"status": "sent"}, {"status": "failed"}, "partial"),
        ({"status": "sent"}, None, "partial"),
        ({"status": "failed"}, {"status": "failed"}, "failed"),
        ({"status": "skipped"}, {"status": "skipped"}, "skipped"),
    ],
)
def test_notification_aggregation_uses_channel_receipts(a, b, status):
    manager = NotifierManager({})
    manager.email = SimpleNamespace(send_daily_report_from_session=lambda _s: a)
    manager.feishu = SimpleNamespace(send_daily_report_from_session=lambda _s: b)
    result = manager.send_daily_report_from_session(object())
    assert result["status"] == status
    assert set(result["deliveries"]) == {"email", "feishu"}


def test_unclassified_transport_exception_is_unknown_not_safe_to_retry():
    manager = NotifierManager({})
    manager.email = SimpleNamespace(
        send_from_session=Mock(side_effect=TimeoutError("read after send"))
    )
    assert manager.send_from_session(object())["status"] == "unknown"


def test_business_exception_and_missing_stock_data_are_not_success(monkeypatch):
    import main

    monkeypatch.setattr(main, "SessionManager", Mock(side_effect=ValueError("broken")))
    assert main.run_daily_task(force=True, config={}).status == "failed"
    assert main.run_brief_report(force=True, config={}).status == "failed"


def test_missing_delivery_receipt_is_unknown_not_skip():
    import main

    session = SimpleNamespace(session_id="controlled")
    assert main._report_delivery_result(None, session).status == "unknown"
    assert (
        main._report_delivery_result(None, session, "trigger_not_met").status
        == "skipped"
    )
