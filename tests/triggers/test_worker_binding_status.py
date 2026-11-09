"""Worker classification follows the committed binding, including later changes."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from src.core import report_execution as reports
from src.core.execution import ExecutionStore, RunResult
from src.triggers import TriggerEvaluator, TriggerStore
from src.triggers.delivery import binding_status


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    blocked = Mock(side_effect=AssertionError("real transport forbidden"))
    for target in (
        "requests.sessions.Session.request",
        "smtplib.SMTP",
        "smtplib.SMTP_SSL",
        "socket.create_connection",
        "socket.socket.connect",
    ):
        monkeypatch.setattr(target, blocked)
    monkeypatch.setattr(reports, "get_build_info", dict)
    monkeypatch.setattr(
        reports,
        "submit_report",
        Mock(side_effect=AssertionError("worker must not enqueue")),
    )
    yield
    blocked.assert_not_called()


@pytest.fixture(params=["daily", "brief"])
def route(request, tmp_path):
    kind = request.param
    config = {
        "storage": {"data_dir": str(tmp_path)},
        "stocks": ["601088", "00728", "VOO"],
    }
    profile = tmp_path / "config.yaml"
    profile.write_text(yaml.safe_dump(config), encoding="utf-8")
    store = TriggerStore(reports.runtime_path(config))
    execution = ExecutionStore(store.path)
    now = datetime(2026, 11, 1, 2, tzinfo=timezone.utc)
    store.create(
        "owner",
        {
            "schedule": {"at": now.isoformat()},
            "actions": [{"kind": kind, "payload": {"chat_id": "chat"}}],
        },
        "worker-test",
    )
    TriggerEvaluator(store).evaluate(now=now)
    with store.transaction() as db:
        action = store.claim_pending(db=db)[0]
        binding = {
            "action_id": action["id"],
            "owner": "owner",
            "chat_id": "chat",
            "number": 1,
            "revision": 1,
            "kind": kind,
            "config_path": str(profile),
        }
        routed = {**config, "_trigger_context": binding}
        payload = reports.build_report_payload(
            routed,
            SimpleNamespace(store=execution),
            kind=kind,
            force=True,
            config_path=profile,
        )
        run_id = execution.submit("reports." + kind, payload, db=db)
        store.mark_submitted(action["id"], run_id, db=db)
        db.execute("UPDATE execution_runs SET status='running' WHERE id=?", (run_id,))
    assert binding_status(routed, run_id=run_id) == "allowed"
    request_path, result_path = tmp_path / "request.json", tmp_path / "result.json"
    request_path.write_text(json.dumps(payload), encoding="utf-8")
    return SimpleNamespace(
        kind=kind,
        store=store,
        action=action,
        run_id=run_id,
        routed=routed,
        payload=payload,
        request=request_path,
        result=result_path,
    )


@pytest.mark.parametrize(
    "state,status,reason,exit_code",
    [
        ("allowed", "succeeded", "accepted", 0),
        ("revoked", "skipped", "trigger_revoked", 0),
        ("invalid", "failed", "trigger_binding_invalid", 1),
    ],
)
def test_worker_rechecks_real_binding_and_matches_parent(
    route, state, status, reason, exit_code
):
    with route.store.transaction() as db:
        if state == "invalid":
            db.execute(
                "UPDATE execution_runs SET job_id='reports.broken' WHERE id=?",
                (route.run_id,),
            )
        elif state == "revoked":
            db.execute(
                "UPDATE trigger_actions SET cancel_requested=1 WHERE id=?",
                (route.action["id"],),
            )
    assert binding_status(route.routed, run_id=route.run_id) == state
    application = Mock()
    expected = RunResult(
        "succeeded", "accepted", deliveries={"email": {"status": "sent"}}
    )
    application.run_daily_task.return_value = expected
    application.run_brief_report.return_value = expected
    assert (
        reports.run_report_worker(
            route.kind, route.request, route.result, application=application
        )
        == exit_code
    )
    receipt = json.loads(route.result.read_text(encoding="utf-8"))
    assert (receipt["status"], receipt["reason"]) == (status, reason)
    called = (
        application.run_daily_task
        if route.kind == "daily"
        else application.run_brief_report
    )
    other = (
        application.run_brief_report
        if route.kind == "daily"
        else application.run_daily_task
    )
    other.assert_not_called()
    if state == "allowed":
        called.assert_called_once()
        assert called.call_args.kwargs["force"] is True
        assert called.call_args.kwargs["config"]["stocks"] == route.routed["stocks"]
        assert receipt["deliveries"] == expected.deliveries
    else:
        called.assert_not_called()
        application.setup_logging.assert_not_called()
        parent = reports._report_handler(
            route.payload, SimpleNamespace(run_id=route.run_id)
        )
        assert (parent.status, parent.reason) == (status, reason)
        assert not route.result.with_name("phase.json").exists()
