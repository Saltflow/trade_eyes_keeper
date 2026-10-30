"""An isolated FQ scheduler host with heartbeat and supervised job outcomes."""

from __future__ import annotations

import copy
import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from ..core.config_store import DEFAULT_CONFIG_PATH, load_config
from ..core.runtime_service import (
    RuntimeService,
    read_service_status,
    runtime_directory,
)
from ..core.schedule_manager import ScheduleManager

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKER_SCRIPT = PROJECT_ROOT / "scripts" / "run_fundamental_quant_worker.py"
TASKS = {"fundamental_quant": "daily", "fundamental_quant_weekly": "weekly"}


def _process_alive(pid, started_at=None):
    """None means access could not be established, never a healthy process."""
    import psutil

    if type(pid) is not int or pid <= 0:
        return False
    try:
        process = psutil.Process(pid)
        if (
            started_at
            and process.create_time()
            > datetime.fromisoformat(started_at).timestamp() + 5
        ):
            return False  # PID was reused after this recorded host/job started.
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except (psutil.AccessDenied, ValueError):
        return None


def worker_config(config: dict) -> dict:
    """Disable every legacy job and inbound Bot in an isolated runtime copy."""
    result = copy.deepcopy(config)
    root = Path(
        result.get("fundamental_quant", {}).get("root", "data/fundamental_quant")
    )
    if not root.is_absolute():
        root = PROJECT_ROOT / root
    result.setdefault("storage", {})["data_dir"] = str(root / "worker")
    result["scheduler"] = {
        "timezone": "Asia/Shanghai",
        "daily_enabled": False,
        "optimize_enabled": False,
        "run_on_startup": False,
        "brief_reports": [],
    }
    for bot in ("telegram", "feishu"):
        result.setdefault("interactive", {}).setdefault(bot, {})["enabled"] = False
    return result


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_jobs(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("jobs"), dict):
            return payload
    except FileNotFoundError:
        return {"jobs": {}, "updated_at": None}
    except (OSError, ValueError, TypeError):
        pass
    return {"jobs": {}, "updated_at": None, "error_type": "JobStatusUnreadable"}


def read_worker_status(config: dict) -> dict:
    scoped = worker_config(config)
    result = read_service_status(scoped)
    directory = runtime_directory(scoped)
    jobs = _read_jobs(directory / "job_status.json")
    host_alive = _process_alive(result.get("pid"), result.get("started_at"))
    result["ready"] = result["ready"] and host_alive is True
    current = jobs["jobs"].get("daily", {})
    child_alive = (
        _process_alive(current.get("pid"), current.get("started_at"))
        if current.get("state") == "running"
        else False
    )
    if current.get("state") == "running" and (
        host_alive is False or child_alive is False
    ):
        current["recorded_state"] = "running"
        current["state"] = "orphaned" if child_alive is True else "interrupted"
        current["error_type"] = (
            "HostProcessMissing" if host_alive is False else "ChildProcessMissing"
        )
    try:
        progress = json.loads((directory / "progress.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        progress = {}
    if progress.get("pid") != current.get("pid"):
        progress = {}
    result.update(
        {
            "scope": "fundamental_quant_only",
            "runtime_directory": str(directory),
            "scheduler_ready": result["ready"],
            "jobs": jobs["jobs"],
            "job_status_error": jobs.get("error_type"),
            "progress_summary": {
                "state": "running" if result["ready"] else "unavailable",
                "host_alive": host_alive,
                "child_alive": child_alive,
                "last_heartbeat": result.get("updated_at"),
                "job_state": current.get("state"),
                "error_type": current.get("error_type"),
                "phase": progress.get("phase"),
                "company_id": progress.get("company_id"),
                "updated_at": progress.get("updated_at"),
            },
            "research_ready": jobs["jobs"]
            .get("daily", {})
            .get("result", {})
            .get("research_status")
            in {"ready", "accepted"},
        }
    )
    return result


def execute_job(config: dict, job: str) -> dict:
    """Called in a supervised child; emits only bounded, credential-free summaries."""
    if job not in {"daily", "weekly"}:
        raise ValueError("unknown FQ worker job")
    settings = config.get("fundamental_quant", {})
    if not settings.get("enabled", False) or not settings.get("schedule", {}).get(
        "enabled", False
    ):
        return {"status": "blocked", "error_codes": ["fundamental_schedule_disabled"]}
    if job == "weekly" and not settings.get("notifications", {}).get("enabled", False):
        return {
            "status": "blocked",
            "error_codes": ["fundamental_notifications_disabled"],
        }
    from .service import FundamentalQuantService

    service = FundamentalQuantService(config)
    result = service.run() if job == "daily" else service.notify_weekly()
    if not isinstance(result, dict):
        raise TypeError("FQ job did not return a structured result")
    companies = result.get("companies", [])
    return {
        "status": "blocked"
        if result.get("status") in {"blocked", "unavailable", "failed", "incomplete"}
        else "completed",
        "research_status": result.get("status"),
        "companies": len(companies) or len(result.get("outcomes", [])),
        "stages": dict(Counter(str(c.get("stage", "unknown")) for c in companies)),
        "error_codes": sorted(
            {
                str(e.get("code", "unknown"))
                for e in [
                    *result.get("errors", []),
                    *(
                        e
                        for outcome in result.get("outcomes", [])
                        for e in outcome.get("errors", [])
                    ),
                ]
                if isinstance(e, dict)
            }
        ),
        "pending_notifications": result.get("pending_notifications"),
    }


class FundamentalScheduleManager(ScheduleManager):
    """Reuse configured FQ cron specs while supervising their complete lifecycle."""

    def __init__(self, config: dict, config_path: Path, *, process_factory=None):
        super().__init__(config, config_path=config_path)
        self.directory = runtime_directory(config)
        self.job_status_path = self.directory / "job_status.json"
        self._jobs_lock = threading.RLock()
        self._jobs = _read_jobs(self.job_status_path)["jobs"]
        self._children = {}
        self._stopping = threading.Event()
        self._process_factory = process_factory or subprocess.Popen

    def start(self) -> None:
        if self.scheduler.running:
            return
        # The host lock is held by RuntimeService here. Old PID records are
        # evidence only; never terminate an unowned PID after a host restart.
        for job, state in list(self._jobs.items()):
            if isinstance(state, dict) and state.get("state") in {
                "starting",
                "running",
            }:
                self._record(
                    job,
                    {
                        **state,
                        "state": "interrupted",
                        "finished_at": _utcnow(),
                        "error_type": "PreviousWorkerEnded",
                    },
                )
        super().start()

    def _record(self, job: str, state: dict) -> None:
        with self._jobs_lock:
            self._jobs[job] = copy.deepcopy(state)
            _atomic_json(
                self.job_status_path, {"updated_at": _utcnow(), "jobs": self._jobs}
            )
            history = self.directory / "job_history.jsonl"
            with history.open("a", encoding="utf-8") as output:
                output.write(
                    json.dumps(
                        {"job": job, **state}, ensure_ascii=False, allow_nan=False
                    )
                    + "\n"
                )

    @staticmethod
    def _terminate(process) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def _job_runner(self, task_id: str, cli_args: list[str]):
        if task_id not in TASKS:
            raise ValueError("isolated worker refuses non-FQ tasks")
        job = TASKS[task_id]

        def run():
            with self._jobs_lock:
                if self._stopping.is_set() or job in self._children:
                    return
                self._children[job] = None
            run_id = uuid4().hex
            state = {
                "run_id": run_id,
                "state": "starting",
                "started_at": _utcnow(),
                "finished_at": None,
                "pid": None,
            }
            process = None
            try:
                self._record(job, state)
                result_path = self.directory / "job_results" / f"{run_id}.json"
                result_path.parent.mkdir(parents=True, exist_ok=True)
                log_path = self.directory / f"{job}.log"
                command = [
                    sys.executable,
                    str(WORKER_SCRIPT),
                    "--config",
                    str(self.config_path.resolve()),
                    "--job",
                    job,
                    "--run-id",
                    run_id,
                    "--result",
                    str(result_path),
                ]
                env = {
                    **os.environ,
                    "PYTHONUTF8": "1",
                    "PYTHONIOENCODING": "utf-8",
                    "FQ_RUN_ID": run_id,
                }
                options = (
                    {"creationflags": subprocess.CREATE_NO_WINDOW}
                    if os.name == "nt"
                    else {"start_new_session": True}
                )
                with log_path.open("a", encoding="utf-8") as output:
                    process = self._process_factory(
                        command,
                        cwd=str(PROJECT_ROOT),
                        env=env,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        **options,
                    )
                with self._jobs_lock:
                    self._children[job] = process
                state.update(state="running", pid=process.pid, log_path=str(log_path))
                self._record(job, state)
                configured = self.config.get("fundamental_quant", {}).get(
                    "max_run_seconds", 10800
                )
                grace = self.config.get("fundamental_quant", {}).get(
                    "shutdown_grace_seconds", 120
                )
                if type(grace) is not int or grace < 0:
                    raise ValueError("invalid shutdown_grace_seconds")
                timeout = (
                    configured + grace
                    if type(configured) is int and configured > 0
                    else 10920
                )
                deadline = time.monotonic() + timeout
                while process.poll() is None:
                    if self._stopping.wait(1):
                        self._terminate(process)
                        state.update(state="cancelled", error_type="WorkerStopping")
                        break
                    if time.monotonic() >= deadline:
                        self._terminate(process)
                        state.update(state="failed", error_type="JobTimeout")
                        break
                if self._stopping.is_set() and state["state"] == "running":
                    state.update(state="cancelled", error_type="WorkerStopping")
                if state["state"] == "running":
                    state["exit_code"] = process.returncode
                    if not result_path.is_file():
                        raise ChildProcessError("child exited without result")
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                    if (
                        not isinstance(result, dict)
                        or result.get("run_id") != run_id
                        or result.get("job") != job
                    ):
                        raise ValueError("child result identity mismatch")
                    state.update(
                        state="completed"
                        if process.returncode == 0
                        and result.get("status") == "completed"
                        else "blocked"
                        if result.get("status") == "blocked"
                        else "failed",
                        result=result,
                        exit_code=process.returncode,
                    )
                state["finished_at"] = _utcnow()
                self._record(job, state)
                if state["state"] in {"failed", "blocked"}:
                    logger.warning(
                        "FQ %s job %s; inspect %s",
                        job,
                        state["state"],
                        self.job_status_path,
                    )
            except Exception as exc:  # noqa: BLE001 - persist typed boundary failures
                if process is not None:
                    try:
                        self._terminate(process)
                    except Exception as stop_exc:  # noqa: BLE001
                        state["termination_error"] = type(stop_exc).__name__
                state.update(
                    state="failed", finished_at=_utcnow(), error_type=type(exc).__name__
                )
                self._record(job, state)
                logger.error("FQ %s worker job failed (%s)", job, type(exc).__name__)
            finally:
                with self._jobs_lock:
                    self._children.pop(job, None)

        return run

    def stop(self) -> None:
        self._stopping.set()
        super().stop()
        with self._jobs_lock:
            children = [child for child in self._children.values() if child is not None]
        for child in children:
            self._terminate(child)


class FundamentalWorker(RuntimeService):
    """Existing heartbeat/process-lock host, containing only FQ cron jobs."""

    def __init__(self, config: dict, *, config_path: Path = DEFAULT_CONFIG_PATH):
        scoped = worker_config(config)
        if not ScheduleManager._fundamental_specs(scoped):
            raise ValueError("fundamental_quant and its schedule must be enabled")
        super().__init__(scoped, scheduling=False)
        self.scheduling = True
        self.scheduler = FundamentalScheduleManager(scoped, Path(config_path))

    def _write_status(self, state: str) -> None:
        if state == "running" and not self.scheduler.scheduler.running:
            raise RuntimeError("fundamental scheduler stopped unexpectedly")
        super()._write_status(state)
        payload = json.loads(self.status_path.read_text(encoding="utf-8"))
        payload.update(
            scope="fundamental_quant_only",
            config_path=str(self.scheduler.config_path.resolve()),
            scheduled_jobs=[
                {**job, "next_run_time": job["next_run"]}
                for job in self.scheduler.get_schedule()
            ],
        )
        _atomic_json(self.status_path, payload)
        drain_path = self.status_path.parent / "drain.json"
        if drain_path.exists():
            request = json.loads(drain_path.read_text(encoding="utf-8"))
            if request.get("host_pid") == os.getpid():
                with self.scheduler._jobs_lock:
                    if not self.scheduler._children:
                        self.stop()

    def run(self, on_started=None, *, run_now: bool = False) -> None:
        def started():
            if run_now:
                self.scheduler.scheduler.add_job(
                    self.scheduler._job_runner(
                        "fundamental_quant", ["--fundamental-quant"]
                    ),
                    trigger="date",
                    id="fundamental_quant_startup",
                    replace_existing=True,
                )
            if on_started:
                on_started()

        super().run(on_started=started)


def run_child(config_path: Path, job: str, run_id: str, result_path: Path) -> int:
    try:
        result = execute_job(load_config(config_path), job)
    except Exception as exc:  # noqa: BLE001 - never persist credential-bearing errors
        result = {"status": "failed", "error_type": type(exc).__name__}
    _atomic_json(
        result_path, {"run_id": run_id, "job": job, "finished_at": _utcnow(), **result}
    )
    return (
        0
        if result["status"] == "completed"
        else 2
        if result["status"] == "blocked"
        else 1
    )
