"""Independent host detects real worker death without a status query."""

import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from src.research_pipeline.common import atomic_write, file_sha, load
from src.research_pipeline.host import supervise
from src.research_pipeline.models import ResearchSpec
from src.research_pipeline.store import Store

REPO = Path(__file__).resolve().parents[1]


def _run(tmp_path, identity="host-fixture", origin=None):
    output = tmp_path / "run"
    output.mkdir()
    spec = ResearchSpec(
        companies=("a_share:600348",),
        output=output,
        repo=REPO,
        store_root=tmp_path / "shared",
        budget_limit_usd=0,
        paid=True,
        origin=origin or {},
    )
    store = Store(spec.store_root)
    store.create_run(spec.to_dict(), identity)
    return output, spec, store


def test_real_readonly_sqlite_startup_still_alerts_bound_origin_once(
    tmp_path, monkeypatch
):
    from src.research_pipeline import host

    origin = {"channel": "feishu", "chat": "trusted_chat", "sender": "trusted_owner"}
    output, spec, store = _run(tmp_path, origin=origin)
    atomic_write(output / "spec.json", spec.to_dict())
    atomic_write(
        output / "run.json",
        {
            "id": "host-fixture",
            "store_root": str(store.root),
            "spec_sha256": file_sha(output / "spec.json"),
        },
    )
    real_factory = host.Store

    def readonly_factory(root):
        value = real_factory(root)

        @contextmanager
        def connection():
            db = sqlite3.connect(
                "file:" + value.db_path.as_posix() + "?mode=ro", uri=True
            )
            db.row_factory = sqlite3.Row
            try:
                yield db
            finally:
                db.close()

        value.connection = connection
        return value

    alerts = []

    class Sender:
        def emergency(self, actual, payload, trusted):
            assert actual == origin == trusted["spec"]["origin"]
            alerts.append(payload)
            return {"state": "sent"}

    monkeypatch.setattr(host, "Store", readonly_factory)
    for _ in range(2):
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            host.supervise("host-fixture", store.root, source=output, sender=Sender())
    assert len(alerts) == 1
    assert load(output / "emergency/ledger_unavailable.json")["state"] == "sent"
    assert load(output / "progress.json")["state"] == "ledger_unavailable"
    assert store.list_attempts("host-fixture") == []


def test_changed_spec_cannot_supply_lost_ledger_notification_origin(tmp_path):
    from src.research_pipeline.host import emergency_from_source

    output, spec, _store = _run(tmp_path)
    atomic_write(output / "spec.json", spec.to_dict())
    atomic_write(output / "run.json", {"id": "host-fixture", "spec_sha256": "0" * 64})
    assert (
        emergency_from_source(output, "host-fixture", sqlite3.DatabaseError("broken"))
        is None
    )
    assert not (output / "emergency").exists()


def test_real_worker_death_pauses_queue_preserves_raw_and_never_replays(tmp_path):
    output, spec, store = _run(tmp_path)
    raw = output / "response.raw"
    worker = tmp_path / "crashing_worker.py"
    worker.write_text(
        "import os,sys,time\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "from src.research_pipeline.store import Store\n"
        "from pathlib import Path\n"
        f"s=Store({str(spec.store_root)!r})\n"
        "import psutil\n"
        "assert s.claim_run('host-fixture',os.getpid(),psutil.Process().create_time())\n"
        f"s.claim_task('sent-once','host-fixture',{str(output)!r})\n"
        "with s.connection() as db:\n"
        " db.execute(\"UPDATE research_attempts SET status='sending',sent_intent=1 WHERE key='sent-once'\")\n"
        f"Path({str(raw)!r}).write_bytes(b'partial SSE unchanged')\n"
        "time.sleep(.1)\n"
        "os._exit(17)\n",
        encoding="utf-8",
    )
    assert (
        supervise(
            "host-fixture",
            spec.store_root,
            worker_command=[sys.executable, "-B", str(worker)],
            poll_seconds=0.02,
        )
        == 17
    )
    value = store.get_run("host-fixture")
    assert value["state"] == "paused" and value["requires_resume"]
    assert value["pause_reason"] == "owner_offline"
    assert value["worker_exit"]["exit_code"] == 17
    assert value["launch"]["pid"] != __import__("os").getpid()
    assert value["launch"]["create_time"] and value["launch"]["command"][-1] == str(
        worker
    )
    assert raw.read_bytes() == b"partial SSE unchanged"
    assert store.get_attempt("sent-once")["status"] == "unknown"
    events = store.events("host-fixture")
    assert sum(event["event"] == "worker_lost" for event in events) == 1
    assert len(list(output.glob("worker_launch_*.json"))) == 1
    assert value["worker_exit"]["at"]
    assert load(next(output.glob("host_exit_*.json")))["worker_lost"] is True


def test_internal_cli_host_runs_frozen_zero_model_worker_and_preserves_final_state(
    tmp_path,
):
    output, spec, store = _run(tmp_path)
    frozen = tmp_path / "frozen"
    script = frozen / "scripts/financial_pipeline.py"
    script.parent.mkdir(parents=True)
    # This is an intentionally tiny frozen worker fixture, not a claim that
    # mocked cache hits validate the entire financial pipeline.
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0,{str(REPO)!r})\n"
        "from src.research_pipeline.store import Store\n"
        f"s=Store({str(spec.store_root)!r})\n"
        "assert sys.argv[1]=='_worker'\n"
        "assert '--resuming' in sys.argv\n"
        "s.update_run('host-fixture',state='complete',cache_reused=4,model_calls=0)\n",
        encoding="utf-8",
    )
    atomic_write(
        output / "runtime_freeze.json", {"files": {str(script): file_sha(script)}}
    )
    store.update_run("host-fixture", runtime_repo=str(frozen))
    process = subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-X",
            "utf8",
            str(REPO / "scripts/financial_pipeline.py"),
            "_host",
            "--run-id",
            "host-fixture",
            "--store-root",
            str(spec.store_root),
            "--resuming",
        ],
        cwd=REPO,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = process.communicate(timeout=25)
    assert process.returncode == 0, (stdout.decode(), stderr.decode())
    value = store.get_run("host-fixture")
    assert value["state"] == "complete" and not value["requires_resume"]
    assert value["cache_reused"] == 4 and value["model_calls"] == 0
    assert value["launch"]["host_pid"] == process.pid
    assert value["launch"]["pid"] != process.pid
    assert not any(e["event"] == "worker_lost" for e in store.events("host-fixture"))
    assert not store.list_attempts("host-fixture")
