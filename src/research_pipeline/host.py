"""Independent background child supervision; never restarts paid work.

The host survives a worker crash and records it without waiting for a status
query. Host or machine death itself still needs external OS supervision.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil

from .common import atomic_write, file_sha, load, now
from .models import ResearchSpec
from .observability import FeishuOriginSender, ProgressPublisher, redact, run_spec
from .store import Store

FINALIZED = {
    "complete",
    "completed",
    "partial",
    "failed",
    "cancelled",
    "budget_stop",
    "awaiting_approval",
    "stopped",
    "user_stopped",
    "source_blocked",
    "auth_blocked",
    "ledger_blocked",
    "funds_blocked",
}


def _identity(pid: int) -> dict:
    try:
        process = psutil.Process(pid)
        return {
            "pid": pid,
            "create_time": process.create_time(),
            "observed_command": process.cmdline(),
        }
    except psutil.NoSuchProcess:
        return {
            "pid": pid,
            "create_time": None,
            "identity_unavailable_after_exit": True,
        }


def _claim_host(store: Store, run_id: str) -> dict:
    identity = _identity(os.getpid())
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT data FROM research_runs WHERE id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError("research_run_not_found")
        run = json.loads(row[0])
        previous = run.get("host_owner") or {}
        if previous.get("pid") and previous.get("create_time"):
            try:
                process = psutil.Process(previous["pid"])
                if abs(process.create_time() - previous["create_time"]) < 0.01:
                    raise ValueError("research_host_already_alive")
            except psutil.NoSuchProcess:
                pass
        launch = run.get("launch") or {}
        if launch.get("pid"):
            try:
                process = psutil.Process(launch["pid"])
                if (
                    launch.get("create_time") is None
                    or abs(process.create_time() - launch["create_time"]) < 0.01
                ):
                    raise ValueError("research_worker_still_alive")
            except psutil.NoSuchProcess:
                pass
        if run.get("state") in FINALIZED or run.get("requires_resume"):
            raise ValueError("research_host_requires_explicit_resume")
        run.update(host_owner=identity, host_heartbeat_at=now(), updated_at=now())
        db.execute(
            "UPDATE research_runs SET data=? WHERE id=?",
            (json.dumps(run, ensure_ascii=False), run_id),
        )
    return run


def _settle_exit(
    store: Store, run_id: str, launch: dict, exit_code: int
) -> tuple[dict, bool]:
    """Pause an unfinished generation atomically; never overwrite a newer run."""
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT data FROM research_runs WHERE id=?", (run_id,)
        ).fetchone()
        run = json.loads(row[0])
        if (run.get("launch") or {}).get("host_generation") != launch[
            "host_generation"
        ]:
            return run, False
        lost = run.get("state") not in FINALIZED
        run.update(
            worker_exit={"exit_code": exit_code, "at": now(), "pid": launch["pid"]},
            host_heartbeat_at=now(),
            host_owner=None,
            updated_at=now(),
        )
        if lost:
            run.update(
                state="paused",
                requires_resume=True,
                pause_reason=run.get("pause_reason")
                if run.get("requires_resume") and run.get("pause_reason")
                else "owner_offline",
                owner_pid=None,
                owner_birth=None,
                error=run.get("error")
                or f"worker_exited_without_final_state: {exit_code}",
            )
        db.execute(
            "UPDATE research_runs SET data=? WHERE id=?",
            (json.dumps(run, ensure_ascii=False), run_id),
        )
    return run, lost


def supervise(
    run_id: str,
    store_root: Path,
    *,
    resuming: bool = False,
    worker_command: list[str] | None = None,
    poll_seconds: float = 0.5,
    sender=None,
    source: Path | None = None,
) -> int:
    """Spawn one frozen worker and monitor its real process exit.

    ``worker_command``/``sender`` are trusted host test boundaries, unavailable
    in the CLI or Pi tool schema. No restart, model API or recovery is performed.
    """
    try:
        store = Store(store_root)
        run = _claim_host(store, run_id)
    except (sqlite3.Error, OSError) as error:
        emergency_from_source(source, run_id, error, sender=sender)
        raise
    spec = run_spec(run)
    output = Path(spec["output"]).resolve()
    output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    if sender is None and spec.get("origin", {}).get("channel") == "feishu":
        sender = FeishuOriginSender(
            Path(spec.get("credential_root") or spec["repo"]), store
        )
    publisher = ProgressPublisher(store, run_id, output, sender=sender)
    process = None
    try:
        if worker_command is None:
            from .engine import _verify_runtime

            root = _verify_runtime(run)
            command = [
                sys.executable,
                "-B",
                "-X",
                "utf8",
                str(root / "scripts/financial_pipeline.py"),
                "_worker",
                "--run-id",
                run_id,
                "--store-root",
                str(store.root),
            ]
            if resuming:
                command.append("--resuming")
        else:
            root = Path(spec["repo"]).resolve()
            command = list(worker_command)
        options = (
            {"creationflags": subprocess.CREATE_NO_WINDOW}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        with (output / f"worker_stdout_{stamp}.log").open("xb") as stdout, (
            output / f"worker_stderr_{stamp}.log"
        ).open("xb") as stderr:
            process = subprocess.Popen(
                command,
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                **options,
            )
        launch = {
            **_identity(process.pid),
            "command": command,
            "started_at": now(),
            "automatic_restart": False,
            "host_pid": os.getpid(),
            "host_generation": stamp,
        }
        atomic_write(output / f"worker_launch_{stamp}.json", launch, exclusive=True)
        store.update_run(run_id, launch=launch)
        publisher.event(
            "worker_started",
            worker_pid=process.pid,
            host_pid=os.getpid(),
            host_generation=stamp,
            resuming=resuming,
        )
        last_heartbeat = 0.0
        while process.poll() is None:
            if time.monotonic() - last_heartbeat >= 5:
                store.update_run(run_id, host_heartbeat_at=now())
                publisher.publish()
                last_heartbeat = time.monotonic()
            time.sleep(max(0.01, min(float(poll_seconds), 5)))
        exit_code = process.wait()
        settled, lost = _settle_exit(store, run_id, launch, exit_code)
        if lost:
            # Turn sent intents into durable unknown receipts without replay.
            store.reconcile()
        atomic_write(
            output / f"host_exit_{stamp}.json",
            {
                "at": now(),
                "worker_pid": process.pid,
                "exit_code": exit_code,
                "state": settled.get("state"),
                "worker_lost": lost,
            },
            exclusive=True,
        )
        publisher.event(
            "worker_lost" if lost else "worker_exited",
            worker_pid=process.pid,
            exit_code=exit_code,
            cause=f"worker_exit_without_final_state:{stamp}"
            if lost
            else "worker_finalized",
            requires_explicit_resume=lost,
            automatic_restart=False,
        )
        return exit_code if exit_code >= 0 else 128 - exit_code
    except Exception as error:
        # A live worker must be made unable to dispatch before the host fails.
        # Core store failure is not hidden; the worker's own ledger gates remain.
        try:
            current = store.get_run(run_id)
            fields = {
                "requires_resume": True,
                "pause_reason": "host_error",
                "host_error": redact(str(error)),
            }
            if current.get("state") not in FINALIZED:
                fields.update(
                    state="paused", error="host_error: " + str(redact(str(error)))
                )
            store.update_run(run_id, **fields)
            publisher.event(
                "run_failed",
                cause="host_error",
                error=redact(str(error)),
                worker_pid=process.pid if process else None,
                automatic_restart=False,
            )
        except Exception as persistence_error:  # noqa: BLE001 - preserve alert when ledger IO itself fails
            publisher.emergency(persistence_error, spec=spec)
        raise
    finally:
        publisher.close()


def emergency_from_source(source, run_id, error, *, sender=None):
    """Only a previously accepted, SHA-bound spec may supply a lost-ledger origin."""
    if source is None:
        return None
    output = Path(source).resolve()
    output = output.parent if output.is_file() else output
    try:
        identity = load(output / "run.json")
        spec_path = output / "spec.json"
        if (
            identity.get("id") != run_id
            or not identity.get("spec_sha256")
            or file_sha(spec_path) != identity["spec_sha256"]
        ):
            return None
        spec = ResearchSpec.from_dict(load(spec_path))
        if spec.output.resolve() != output:
            return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if sender is None and spec.origin.get("channel") == "feishu":
        sender = FeishuOriginSender(spec.credential_root, None)
    publisher = ProgressPublisher(None, run_id, output, sender=sender)
    try:
        return publisher.emergency(error, spec=spec)
    finally:
        publisher.close()
