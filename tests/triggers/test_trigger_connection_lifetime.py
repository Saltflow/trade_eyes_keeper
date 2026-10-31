"""Read-only delivery guards must close SQLite even on early return/error."""

import sqlite3
from datetime import datetime, timezone

import pytest

from src.core.execution import ExecutionRuntime, ExecutionStore
from src.core.report_execution import runtime_path
from src.triggers import delivery
from src.triggers.runtime import TriggerRuntime


@pytest.mark.parametrize("case", ["allowed", "revoked", "invalid", "query_error"])
def test_delivery_guard_closes_its_readonly_connection(tmp_path, monkeypatch, case):
    now = datetime.now(timezone.utc)
    config = {"storage": {"data_dir": str(tmp_path)}}
    execution = ExecutionRuntime(ExecutionStore(runtime_path(config)))
    runtime = TriggerRuntime(config, execution, clock=lambda: now)
    runtime.store.create(
        "alice",
        {
            "label": "Synthetic reminder",
            "schedule": {"at": now.isoformat()},
            "actions": [{"kind": "reminder", "payload": {"chat_id": "synthetic-chat"}}],
        },
        "test",
    )
    runtime.evaluator.evaluate(None, now=now)
    runtime._submit_pending(config)
    action = runtime.store.actions("alice")[0]
    binding = execution.store.get(action["run_id"])["payload"]["binding"]
    # Synthetic run state, without starting a host or any network job.
    with execution.store.connection() as db:
        db.execute(
            "UPDATE execution_runs SET status='running' WHERE id=?", (action["run_id"],)
        )
        if case == "query_error":
            db.execute("DROP TABLE execution_runs")
    if case == "revoked":
        runtime.store.remove("alice", [1])
    if case == "invalid":
        binding = {**binding, "owner": "someone-else"}
    opened = []
    original = sqlite3.connect

    def connect(*args, **kwargs):
        connection = original(*args, **kwargs)
        opened.append(connection)  # Prevent GC from hiding a leaked connection.
        return connection

    monkeypatch.setattr(delivery.sqlite3, "connect", connect)
    result = delivery.binding_status({**config, "_trigger_context": binding})
    assert result == ("invalid" if case == "query_error" else case)
    assert len(opened) == 1
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
