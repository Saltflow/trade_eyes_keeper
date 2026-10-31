"""Report adapters for the shared executor; no report business lives here.

Public CLI, scheduler and Bot calls enqueue the same jobs. The internal worker
calls ``main.run_*`` directly and writes a receipt, so it cannot submit itself.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

from .build_info import get_build_info
from .config_store import DEFAULT_CONFIG_PATH, ConfigStore
from .execution import JobSpec, ModuleSpec, RunResult, get_runtime
from .runtime_files import atomic_write_json

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPORT_JOB_IDS = {"daily": "reports.daily", "brief": "reports.brief"}
_SECRET_NAMES = ("secret", "password", "api_key", "webhook")
_PROGRESS_INTERVAL_SECONDS = 10


def _secret_field(key):
    name = str(key).lower()
    return (
        name == "token"
        or name.endswith("_token")
        or any(word in name for word in _SECRET_NAMES)
    )


def _settings_snapshot(value):
    if isinstance(value, dict):
        return {
            key: _settings_snapshot(child)
            for key, child in value.items()
            if not _secret_field(key)
        }
    if isinstance(value, list):
        return [_settings_snapshot(child) for child in value]
    return value


def _restore_secrets(snapshot, current):
    """Only credential fields are refreshed; business settings stay pinned."""
    if isinstance(snapshot, dict):
        current = current if isinstance(current, dict) else {}
        restored = {
            key: _restore_secrets(child, current.get(key))
            for key, child in snapshot.items()
        }
        restored.update(
            {key: value for key, value in current.items() if _secret_field(key)}
        )
        return restored
    if isinstance(snapshot, list):
        current = current if isinstance(current, list) else []
        return [
            _restore_secrets(child, current[index] if index < len(current) else None)
            for index, child in enumerate(snapshot)
        ]
    return snapshot


def runtime_path(config: dict) -> Path:
    """Use the existing conversation database, including isolated profiles."""
    directory = Path((config.get("storage", {}) or {}).get("data_dir", "data"))
    if not directory.is_absolute():
        directory = PROJECT_ROOT / directory
    return directory / "runtime" / "feishu_assistant.sqlite3"


def _write_json(path: Path, payload: dict) -> None:
    atomic_write_json(path, payload, allow_nan=False)


class _ReportCleanupPending(RuntimeError):
    """Retain the exact cleanup objects when child containment is unproven."""

    def __init__(self, state, error):
        super().__init__(type(error).__name__)
        self.state = state
        self.error_type = type(error).__name__


def _snapshot_report_children(process, state) -> None:
    """Retry only through the original parent object while its Popen is live."""
    parent = state["parent"]
    if parent is None or type(process.poll()) is int:
        raise RuntimeError("report_tree_enumeration_unverified")
    birth = parent.create_time()
    if type(birth) not in (int, float) or birth != birth:  # noqa: PLR0124 - Reject NaN birth evidence.
        raise RuntimeError("report_parent_birth_unverified")
    if state["parent_birth"] is None:
        state["parent_birth"] = birth
    if birth != state["parent_birth"] or not parent.is_running():
        raise RuntimeError("report_parent_identity_changed")
    for child in parent.children(recursive=True):
        state["descendants"].setdefault(child, None)
        child_birth = child.create_time()
        if type(child_birth) not in (int, float) or child_birth != child_birth:  # noqa: PLR0124 - Reject NaN.
            raise RuntimeError("report_descendant_birth_unverified")
        state["descendants"][child] = child_birth
    if (
        type(process.poll()) is int
        or parent.create_time() != state["parent_birth"]
        or not parent.is_running()
    ):
        raise RuntimeError("report_tree_enumeration_unverified")
    state["enumeration_complete"] = True


def _known_report_descendant_exited(child, birth, api) -> bool:
    try:
        running = child.is_running()
        if running is False:
            return True
        if running is not True or birth is None:
            return False
        observed_birth = child.create_time()
        if type(observed_birth) not in (int, float) or observed_birth != observed_birth:  # noqa: PLR0124 - Reject NaN.
            return False
        if observed_birth != birth:
            return True  # The original identity is gone; never act on its PID.
        exit_code = child.wait(timeout=0)
        return exit_code is None or type(exit_code) is int
    except api.NoSuchProcess:
        return True
    except BaseException:  # noqa: BLE001 - Unreadable state is not exit evidence.
        return False


def _report_cleanup_exited(process, state) -> bool:
    if not state["enumeration_complete"]:
        return False
    if not state.get("direct_exit_known", False):
        try:
            if type(process.poll()) is not int:
                return False
            state["direct_exit_known"] = True
        except BaseException:  # noqa: BLE001 - Preserve unproven ownership.
            return False
    return all(
        _known_report_descendant_exited(child, birth, state["api"])
        for child, birth in state["descendants"].items()
    )


def _stop_child(process) -> None:
    """Return only after the direct child and all discovered identities exit."""
    state = {
        "api": None,
        "parent": None,
        "parent_birth": None,
        "descendants": {},
        "enumeration_complete": False,
    }
    try:
        import psutil

        state["api"] = psutil
        state["parent"] = parent = psutil.Process(process.pid)
        _snapshot_report_children(process, state)
        descendants = list(state["descendants"])
        for child in reversed(descendants):
            try:
                if (
                    child.is_running()
                    and child.create_time() == state["descendants"][child]
                ):
                    child.terminate()
            except psutil.NoSuchProcess:
                pass
        process.terminate()
        _gone, alive = psutil.wait_procs([*descendants, parent], timeout=5)
        for child in alive:
            if child is parent:
                process.kill()
            elif child in state["descendants"]:
                try:
                    if (
                        child.is_running()
                        and child.create_time() == state["descendants"][child]
                    ):
                        child.kill()
                except psutil.NoSuchProcess:
                    pass
            else:
                raise RuntimeError("unowned_cleanup_process")
        try:
            exit_code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            exit_code = process.wait(timeout=5)
        if type(exit_code) is int:
            state["direct_exit_known"] = True
        if not _report_cleanup_exited(process, state):
            raise TimeoutError("report_cleanup_not_confirmed")
    except BaseException as error:  # Carry exact cleanup ownership.
        raise _ReportCleanupPending(state, error) from error


def _stop_child_until_exit(process, context) -> None:
    try:
        _stop_child(process)
        return
    except _ReportCleanupPending as error:
        state, error_type = error.state, error.error_type
    except BaseException as error:  # noqa: BLE001 - Unknown enumeration stays held.
        state = {
            "api": None,
            "parent": None,
            "parent_birth": None,
            "descendants": {},
            "enumeration_complete": False,
        }
        error_type = type(error).__name__
    stage = (
        "report_cleanup_waiting_for_exit"
        if state["enumeration_complete"]
        else "report_cleanup_unverified_tree"
    )
    notice_pending = False
    try:
        context.interrupt(stage)
    except BaseException:  # noqa: BLE001 - Retry a failed diagnostic, never release.
        notice_pending = True
    try:
        context.progress(stage)
    except BaseException:  # noqa: BLE001, S110 - Diagnostic failure cannot release.
        pass
    try:
        logger.error("Report cleanup pending (%s); retaining ownership", error_type)
    except BaseException:  # noqa: BLE001, S110 - Diagnostic failure cannot release.
        pass
    from threading import Event

    pause, retry_waits = Event(), 0
    while True:
        if notice_pending and retry_waits >= 4:
            retry_waits = 0
            try:
                context.interrupt(stage)
            except BaseException:  # noqa: BLE001, S110 - Bounded diagnostic retry.
                pass
            else:
                notice_pending = False
        if not state["enumeration_complete"] and state["parent"] is not None:
            try:
                _snapshot_report_children(process, state)
            except BaseException:  # noqa: BLE001, S110 - No inferred enumeration.
                pass
        if _report_cleanup_exited(process, state):
            return
        if not state.get("direct_exit_known", False):
            try:
                if type(process.wait(timeout=0.25)) is int:
                    state["direct_exit_known"] = True
            except BaseException:  # noqa: BLE001, S110 - Wait failure is not an exit.
                pass
        if _report_cleanup_exited(process, state):
            return
        # A private Event cannot already be cancelled, and its bounded wait
        # avoids busy writes even if a diagnostic clock or sleep is faulty.
        pause.wait(0.25)
        retry_waits += 1


def _report_handler(payload: dict, ctx) -> RunResult:
    """Keep the claim and concurrency key until the actual child has ended."""
    kind = payload["kind"]
    if kind not in REPORT_JOB_IDS:
        return RunResult("failed", "invalid_report_kind")
    snapshot = payload.get("config_snapshot", {})
    if "_trigger_context" in snapshot:
        from ..triggers.delivery import binding_status

        state = binding_status(snapshot, run_id=ctx.run_id)
        if state != "allowed":
            return RunResult(
                "skipped" if state == "revoked" else "failed",
                "trigger_revoked" if state == "revoked" else "trigger_binding_invalid",
            )
    directory = Path(payload["artifact_root"]) / ctx.run_id
    request_path = directory / "request.json"
    result_path = directory / "result.json"
    phase_path = directory / "phase.json"
    log_path = directory / "worker.log"
    _write_json(request_path, payload)
    command = [
        sys.executable,
        "-X",
        "utf8",
        str(PROJECT_ROOT / "main.py"),
        "--report-worker",
        kind,
        "--report-payload",
        str(request_path),
        "--report-result",
        str(result_path),
    ]
    # A report can mutate a reference portfolio or start channel delivery. No
    # abandoned report may be automatically rerun merely for lacking a receipt.
    if not ctx.mark_side_effect("report_starting"):
        return RunResult("unknown", "report_start_not_authorized")
    # Worker phase.json keeps immediate diagnostic detail. Persist at most one
    # such observation per interval; start intent and final result stay FULL.
    next_progress = time.monotonic() + _PROGRESS_INTERVAL_SECONDS
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    with log_path.open("a", encoding="utf-8") as output:
        process = subprocess.Popen(
            command,
            cwd=str(PROJECT_ROOT),
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        previous_phase = None
        try:
            while process.poll() is None:
                if ctx.cancel_event.is_set():
                    _stop_child_until_exit(process, ctx)
                    return RunResult(
                        "unknown",
                        "report_interrupted_after_start",
                        artifacts={
                            "log": str(log_path),
                            "exit_code": process.returncode,
                        },
                    )
                try:
                    phase = json.loads(phase_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    phase = None
                if (
                    phase
                    and phase != previous_phase
                    and time.monotonic() >= next_progress
                ):
                    ctx.progress(str(phase.get("stage", "report_running")))
                    previous_phase = phase
                    next_progress = time.monotonic() + _PROGRESS_INTERVAL_SECONDS
                try:
                    # The OS wakes this wait as soon as the child exits. A
                    # fixed sleep adds up to 250 ms to every completed report.
                    process.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    pass
            exit_code = process.wait()
        except BaseException:
            _stop_child_until_exit(process, ctx)
            raise
    artifacts = {"log": str(log_path), "exit_code": exit_code}
    try:
        receipt = json.loads(result_path.read_text(encoding="utf-8"))
        if not isinstance(receipt, dict) or "status" not in receipt:
            raise ValueError("report result must explicitly state its status")
        result = RunResult.from_dict(receipt)
    except (OSError, ValueError, TypeError, KeyError):
        return RunResult(
            "unknown", "report_worker_receipt_missing", artifacts=artifacts
        )
    if exit_code != report_exit_code(result):
        return RunResult(
            "unknown", "report_worker_receipt_exit_mismatch", artifacts=artifacts
        )
    return RunResult(
        result.status,
        result.reason,
        artifacts={
            **(result.artifacts or {}),
            **artifacts,
            "receipt": str(result_path),
        },
        deliveries=result.deliveries,
    )


def register_report_jobs(
    runtime, config_path: Path = DEFAULT_CONFIG_PATH, *, config=None
) -> None:
    """Register once; updates to report settings travel in each job snapshot."""
    del config_path  # Path is carried in the immutable submission payload.
    previous = runtime.module_status("reports")
    version = (
        hashlib.sha256(
            json.dumps(_settings_snapshot(config), sort_keys=True).encode()
        ).hexdigest()[:16]
        if config is not None
        else (previous or {}).get("version", "1")
    )
    module = ModuleSpec(
        id="reports",
        version=version,
        enabled=bool(previous["enabled"]) if previous else True,
        jobs=tuple(
            JobSpec(
                id=job_id,
                handler=_report_handler,
                lane="report",
                concurrency_key="reports",
                timeout_seconds=7200,
                recovery="unknown",
                starting_side_effect="report_starting",
            )
            for job_id in REPORT_JOB_IDS.values()
        ),
    )
    if not runtime.is_registered("reports"):
        runtime.register(module)
    elif previous and previous["version"] != version:
        runtime.apply(module)


def submit_report(
    config: dict,
    *,
    kind: str = "daily",
    report_id: str = "morning_snapshot",
    force: bool = False,
    source: str = "manual",
    request_id: str | None = None,
    wait: bool = False,
    runtime=None,
    config_path: Path = DEFAULT_CONFIG_PATH,
    timeout: float | None = None,
):
    """Submit without starting a competing consumer when the service is live."""
    if kind not in REPORT_JOB_IDS:
        raise ValueError("report kind must be daily or brief")
    runtime = runtime or get_runtime(runtime_path(config))
    register_report_jobs(runtime, config_path, config=config)
    payload = build_report_payload(
        config,
        runtime,
        kind=kind,
        report_id=report_id,
        force=force,
        config_path=config_path,
    )
    run = runtime.submit(
        REPORT_JOB_IDS[kind],
        payload,
        request_id=request_id or f"{source}:{uuid4().hex}",
        source=source,
    )
    run_id = run if isinstance(run, str) else run["id"]
    own_runtime = False
    if not runtime.running and not runtime.store.has_live_owner():
        # Atomically avoid competing with a service or another one-shot CLI.
        # A non-waiting adapter uses the same executor, never another daemon.
        activated = runtime.start_if_unowned()
        own_runtime = bool(wait and activated)
    if not wait:
        return run
    try:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            # Another one-shot CLI may be the current owner and exit after its
            # own report. A waiter must take over if that owner goes away.
            if not runtime.running and not runtime.store.has_live_owner():
                own_runtime = runtime.start_if_unowned() or own_runtime
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise TimeoutError(run_id)
            try:
                return runtime.wait(run_id, timeout=min(1.0, remaining or 1.0))
            except TimeoutError:
                if deadline is not None and time.monotonic() >= deadline:
                    raise
    finally:
        if own_runtime:
            runtime.stop(drain=True)


def build_report_payload(
    config,
    runtime,
    *,
    kind="daily",
    report_id="morning_snapshot",
    force=False,
    config_path=DEFAULT_CONFIG_PATH,
):
    """Prepare the normal report request for atomic enqueue by a durable caller."""
    if kind not in REPORT_JOB_IDS:
        raise ValueError("report kind must be daily or brief")
    return {
        "kind": kind,
        "report_id": str(report_id),
        "force": bool(force),
        "config_path": str(Path(config_path).resolve()),
        "config_snapshot": _settings_snapshot(config),
        "artifact_root": str(runtime.store.path.parent / "reports"),
    }


def report_exit_code(result: RunResult) -> int:
    return 0 if result.status in {"succeeded", "skipped"} else 1


def run_report_worker(
    kind: str, payload_path: Path, result_path: Path, *, application=None
) -> int:
    """Internal child entry: call business functions directly, never enqueue."""
    get_build_info()  # Freeze the child identity before loading business code.
    phase_path = result_path.with_name("phase.json")
    try:
        payload = json.loads(Path(payload_path).read_text(encoding="utf-8"))
        if payload.get("kind") != kind or kind not in REPORT_JOB_IDS:
            raise ValueError("worker kind does not match queued request")
        credentials = ConfigStore(Path(payload["config_path"])).load_runtime()
        config = _restore_secrets(payload["config_snapshot"], credentials)
        if "_trigger_context" in config:
            # Current allowlist and enabled flags can revoke a queued personal
            # destination. Only report business settings stay frozen.
            config["interactive"] = credentials.get("interactive", {})
            from ..triggers.delivery import binding_status

            if binding_status(config) != "allowed":
                result = RunResult("skipped", "trigger_revoked")
                _write_json(result_path, result.to_dict())
                return report_exit_code(result)
        if application is None:
            import main as application

        application.setup_logging(config)

        def progress(stage: str) -> None:
            _write_json(phase_path, {"stage": stage, "observed_at": time.time()})

        progress("report_business_start")
        if kind == "daily":
            result = application.run_daily_task(
                force=bool(payload.get("force")), config=config, progress=progress
            )
        else:
            result = application.run_brief_report(
                payload.get("report_id", "morning_snapshot"),
                force=bool(payload.get("force")),
                config=config,
                progress=progress,
            )
        if not isinstance(result, RunResult):
            result = RunResult("unknown", "report_business_receipt_missing")
    except Exception as exc:
        logger.exception("Report worker failed")
        result = RunResult("failed", f"worker:{type(exc).__name__}")
    _write_json(Path(result_path), result.to_dict())
    return report_exit_code(result)
