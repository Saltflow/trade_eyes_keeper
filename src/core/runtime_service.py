"""Scheduler and outbound Bot host; exposes no HTTP listener."""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .execution import _identity, get_runtime
from .process_lock import exclusive_process_lock
from .report_execution import register_report_jobs, runtime_path
from .runtime_files import atomic_write_json
from .schedule_manager import ScheduleManager

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
HEARTBEAT_SECONDS = 10
STATUS_MAX_AGE_SECONDS = 60


def _stop_binding(payload: dict) -> dict:
    """Validate a concrete host session, never a freshness-only process claim."""
    pid = payload.get("pid")
    birth = payload.get("process_birth")
    execution = payload.get("execution")
    if not isinstance(execution, dict):
        raise TypeError("invalid execution session identity")
    owner = execution.get("owner_id")
    started = payload.get("started_at")
    if (
        type(pid) is not int
        or pid <= 0
        or not isinstance(birth, str)
        or not birth
        or birth == "?"
        or not isinstance(owner, str)
        or len(owner) != 32
        or any(char not in "0123456789abcdef" for char in owner)
        or not isinstance(started, str)
    ):
        raise ValueError("invalid service session identity")
    parsed = datetime.fromisoformat(started)
    if parsed.tzinfo is None or parsed > datetime.now(timezone.utc):
        raise ValueError("invalid service session time")
    return {
        "owner_id": owner,
        "pid": pid,
        "process_birth": birth,
        "started_at": started,
    }


def request_service_stop(config: dict) -> dict:
    """Request one live session's planned drain; never start a runtime or Bot."""
    status = read_service_status(config)
    if not status.get("fresh") or status.get("state") not in {"running", "stopping"}:
        raise ValueError("service status is unavailable or stale")
    binding = _stop_binding(status)
    if _identity(binding["pid"]) != binding["process_birth"]:
        raise ValueError("service process identity is unavailable or changed")
    path = runtime_directory(config) / "service_stop.json"
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
        if _valid_stop_request(previous, binding):
            return previous
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    request = dict(
        binding,
        command="stop",
        request_id=uuid.uuid4().hex,
        requested_at=datetime.now(timezone.utc).isoformat(),
    )
    atomic_write_json(path, request, indent=2)
    return request


def _valid_stop_request(request: dict, binding: dict) -> bool:
    if not isinstance(request, dict) or request.get("command") != "stop":
        return False
    if any(
        type(request.get(key)) is not type(value) or request.get(key) != value
        for key, value in binding.items()
    ):
        return False
    identifier = request.get("request_id")
    if (
        not isinstance(identifier, str)
        or len(identifier) != 32
        or any(char not in "0123456789abcdef" for char in identifier)
    ):
        return False
    try:
        requested = datetime.fromisoformat(request["requested_at"])
        started = datetime.fromisoformat(binding["started_at"])
        return requested.tzinfo is not None and (
            started <= requested
            and (requested - datetime.now(timezone.utc)).total_seconds() <= 5
        )
    except (ValueError, TypeError, KeyError):
        return False


def runtime_directory(config: dict) -> Path:
    path = Path((config.get("storage", {}) or {}).get("data_dir", "data"))
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path / "runtime"


def read_service_status(config: dict) -> dict:
    """Read local process evidence without network requests or credentials."""
    path = runtime_directory(config) / "service_status.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("status must be an object")
        thread_states = payload.get("bot_threads_alive", {})
        if not isinstance(thread_states, dict):
            raise TypeError("thread states must be an object")
        updated = datetime.fromisoformat(payload["updated_at"])
        age = (datetime.now(timezone.utc) - updated).total_seconds()
        payload["fresh"] = 0 <= age <= STATUS_MAX_AGE_SECONDS
        notices = payload.get("health_notifications") or {}
        if not isinstance(notices, dict) or not isinstance(
            payload.get("execution", {}), dict
        ):
            raise TypeError("health evidence must be an object")
        payload["ready"] = bool(
            payload.get("state") == "running"
            and payload["fresh"]
            and not payload.get("failure")
            and payload.get("transport_ready", True)
            and all(thread_states.values())
            and (payload.get("execution") or {}).get("ready", False)
            and not notices.get("issues")
            and not notices.get("unknown")
            and not notices.get("failed")
            and not notices.get("exhausted")
            and not notices.get("route_unconfigured")
        )
        return payload
    except (OSError, ValueError, TypeError, KeyError):
        return {"state": "unavailable", "ready": False, "fresh": False}


class RuntimeService:
    """Own the lifecycle of one scheduler and all configured Bot receivers."""

    def __init__(
        self,
        config: dict,
        *,
        scheduling: bool = True,
        execution_runtime=None,
        bot_factory: Callable | None = None,
        register_reports: bool = True,
    ):
        if bot_factory is not None and not callable(bot_factory):
            raise TypeError("bot_factory must be a trusted Python callable")
        if not isinstance(register_reports, bool):
            raise TypeError("register_reports must be boolean")
        self.config = config
        self.scheduling = scheduling
        self.bot_factory = bot_factory
        self.register_reports = register_reports
        self.stop_event = threading.Event()
        self.execution_runtime = execution_runtime or get_runtime(runtime_path(config))
        if (
            bot_factory is None
            and self.execution_runtime.store.path.resolve()
            != runtime_path(config).resolve()
        ):
            raise ValueError(
                "default Bot and execution runtime must share configured SQLite"
            )
        self.scheduler = (
            ScheduleManager(config, execution_runtime=self.execution_runtime)
            if scheduling
            else None
        )
        self.bots: dict[str, object] = {}
        self.threads: dict[str, threading.Thread] = {}
        self.failure: str | None = None
        self.health_reporter = None
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.status_path = runtime_directory(config) / "service_status.json"
        self._execution_started = False
        self._start_attempted = False
        self._owner_evidence = {}
        self._previous_issue = None
        self._planned_stop = False
        self._phase = "constructed"
        self._diagnostics = []
        self._accepted_stop_request_id = None

    def _configure_bots(self) -> None:
        if self.bot_factory is not None:
            bots = self.bot_factory(self.config, self.execution_runtime)
            if not isinstance(bots, dict) or any(
                not isinstance(name, str) or not name for name in bots
            ):
                raise TypeError("bot_factory must return a named Bot mapping")
            self.bots = bots
            if not self.scheduling and not self.bots:
                raise ValueError("interactive mode requires at least one enabled Bot")
            for bot in self.bots.values():
                for method in ("validate_config", "run", "pause_admission", "stop"):
                    if not callable(getattr(bot, method, None)):
                        raise TypeError(f"Bot requires {method}()")
                bot.validate_config()
            return
        settings = self.config.get("interactive", {}) or {}
        if (settings.get("telegram", {}) or {}).get("enabled", False):
            from ..interactive.telegram_bot import TelegramBot

            self.bots["telegram"] = TelegramBot(self.config)
        if (settings.get("feishu", {}) or {}).get("enabled", False):
            from ..interactive.feishu_bot import FeishuBot

            self.bots["feishu"] = FeishuBot(
                self.config, execution_runtime=self.execution_runtime
            )
        if not self.scheduling and not self.bots:
            raise ValueError("interactive mode requires at least one enabled Bot")
        for bot in self.bots.values():
            bot.validate_config()

    def _run_bot(self, name: str, bot) -> None:
        try:
            bot.run()
            if not self.stop_event.is_set():
                raise RuntimeError(f"{name} receiver stopped unexpectedly")
        except BaseException as exc:  # noqa: BLE001 -- an exited receiver must fail its host
            if not self.stop_event.is_set():
                self.failure = f"{name} receiver failed"
                self._diagnostic(f"bot.{name}.run", exc)
                self.stop_event.set()

    def _write_status(self, state: str) -> None:
        try:
            execution = self.execution_runtime.health()
        except Exception as exc:
            if state == "running":
                raise
            self._diagnostic("execution_health_status", exc)
            self.failure = self.failure or "execution health unavailable"
            execution = {
                "ready": False,
                "running": None,
                "owner_id": self.execution_runtime.owner_id,
                "evidence": "unavailable",
            }
        module_health = {}
        issues = []
        if self.failure:
            issues.append(
                {"id": "runtime_service", "module_id": "host", "reason": self.failure}
            )
        bot = self.bots.get("feishu")
        if bot is not None:
            app = getattr(bot, "app", None)
            assistant = self._assistant(bot)
            if getattr(app, "assistant_enabled", False) and assistant is None:
                issues.append(
                    {
                        "id": "conversation.initialization",
                        "module_id": "conversation",
                        "kind": "initialization_failed",
                        "reason": "assistant_unavailable",
                    }
                )
            if assistant is not None:
                try:
                    module_health["personal_monitor"] = (
                        assistant.personal_runtime.health()
                    )
                except Exception as exc:
                    if state == "running":
                        raise
                    self._diagnostic("personal_monitor_health_status", exc)
                    self.failure = self.failure or "personal monitor health unavailable"
                    module_health["personal_monitor"] = {"error": type(exc).__name__}
                error = module_health["personal_monitor"].get("error")
                if error:
                    issues.append(
                        {
                            "id": "personal_monitor",
                            "module_id": "personal_monitor",
                            "reason": error,
                        }
                    )
        for name, receiver in self.bots.items():
            health = getattr(receiver, "health", None)
            if callable(health):
                try:
                    snapshot = health()
                    if not isinstance(snapshot, dict) or not isinstance(
                        snapshot.get("ready"), bool
                    ):
                        raise TypeError("Bot health must include boolean ready")
                    transport_issues = snapshot.get("issues", [])
                    if not isinstance(transport_issues, list):
                        raise TypeError("Bot health issues must be a list")
                    if any(
                        not isinstance(item, dict)
                        or not isinstance(item.get("id"), str)
                        or not item["id"]
                        for item in transport_issues
                    ):
                        raise TypeError("Bot health issues require stable identifiers")
                    degraded = snapshot.get("degraded", False)
                    if type(degraded) is not bool:
                        raise TypeError("Bot health degraded must be boolean")
                    snapshot = dict(snapshot)
                    snapshot["ready"] = (
                        snapshot["ready"]
                        and not degraded
                        and snapshot.get("state") != "degraded"
                        and not transport_issues
                    )
                except Exception as exc:
                    if state == "running":
                        raise
                    self._diagnostic(f"bot.{name}.health", exc)
                    self.failure = self.failure or f"{name} health unavailable"
                    snapshot = {"ready": False, "error_type": type(exc).__name__}
                    transport_issues = []
                module_health[f"transport.{name}"] = snapshot
                if state == "running":
                    issues.extend(transport_issues)
                if (
                    state == "running"
                    and not snapshot["ready"]
                    and not transport_issues
                ):
                    issues.append(
                        {
                            "id": f"transport.{name}",
                            "module_id": "transport",
                            "kind": "transport_not_ready",
                            "reason": "receiver_not_ready",
                        }
                    )
        if (
            self.health_reporter is not None
            and state in {"running", "failed"}
            and not self._planned_stop
        ):
            try:
                self.health_reporter.observe(execution, issues=issues)
            except Exception as exc:
                if state == "running":
                    raise
                self._diagnostic("health_observation_status", exc)
                self.failure = self.failure or "health observation unavailable"
        try:
            notifications = (
                self.health_reporter.health() if self.health_reporter else {}
            )
        except Exception as exc:
            if state == "running":
                raise
            self._diagnostic("health_receipts_status", exc)
            self.failure = self.failure or "health receipts unavailable"
            notifications = {"unavailable": True, "exception_class": type(exc).__name__}
        payload = {
            "pid": os.getpid(),
            "state": "failed" if self.failure and state == "stopped" else state,
            "started_at": self.started_at,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "scheduler_enabled": self.scheduling,
            "bot_threads_alive": {
                name: thread.is_alive() for name, thread in self.threads.items()
            },
            "failure": self.failure,
            "planned_stop": self._planned_stop,
            "accepted_stop_request_id": self._accepted_stop_request_id,
            "process_birth": self._owner_evidence.get("birth"),
            "phase": self._phase,
            "diagnostics": list(self._diagnostics),
            "execution": execution,
            "module_health": module_health,
            "transport_ready": all(
                value["ready"]
                for key, value in module_health.items()
                if key.startswith("transport.")
            ),
            "health_notifications": notifications,
        }
        atomic_write_json(self.status_path, payload, indent=2)

    @staticmethod
    def _assistant(bot):
        assistant = getattr(bot, "assistant", None)
        return (
            assistant
            if assistant is not None
            else getattr(getattr(bot, "app", None), "_assistant", None)
        )

    def _previous_session(self):
        try:
            old = json.loads(self.status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        if (
            not isinstance(old, dict)
            or old.get("planned_stop")
            or not isinstance(old.get("state"), str)
            or old.get("state") not in {"running", "failed", "stopping"}
        ):
            return None
        execution = old.get("execution")
        if not isinstance(execution, dict):
            return None
        owner_id = execution.get("owner_id")
        if not isinstance(owner_id, str) or not owner_id:
            return None
        try:
            started = datetime.fromisoformat(old["started_at"])
            if started.tzinfo is None:
                return None
        except (KeyError, ValueError, TypeError):
            return None
        with self.execution_runtime.store.connection() as db:
            owner = db.execute(
                "SELECT * FROM execution_owners WHERE id=?", (owner_id,)
            ).fetchone()
        if owner is None or owner["pid"] != old.get("pid"):
            return None
        if old.get("process_birth") not in (None, owner["birth"]):
            return None
        if any(
            p["id"] == owner_id for p in self.execution_runtime.store.active_owners()
        ):
            raise RuntimeError("previous host owner is still live")
        return {
            "key": owner_id,
            "metadata": {
                "pid": owner["pid"],
                "birth": owner["birth"],
                "started_at": old.get("started_at"),
            },
        }

    def _record_previous_session(self):
        issue = self._previous_issue
        if issue is None:
            return
        self.health_reporter.record_host_recovery(issue["key"], issue["metadata"])
        self._previous_issue = None

    def stop(self, *, planned: bool = True) -> None:
        if planned and not self.failure and not self._planned_stop:
            try:
                previous = json.loads(self.status_path.read_text(encoding="utf-8"))
                if (previous.get("execution") or {}).get(
                    "owner_id"
                ) != self.execution_runtime.owner_id:
                    raise ValueError("planned stop session does not own status")
                previous.update(
                    state="stopping",
                    planned_stop=True,
                    accepted_stop_request_id=self._accepted_stop_request_id,
                )
                atomic_write_json(self.status_path, previous, indent=2)
                self._planned_stop = True
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                self.failure = (
                    self.failure or f"planned stop persistence: {type(exc).__name__}"
                )
                self._diagnostic("planned_stop_persistence", exc)
        self.stop_event.set()

    def _consume_stop_request(self) -> bool:
        path = self.status_path.with_name("service_stop.json")
        if not path.exists():
            return False
        try:
            request = json.loads(path.read_text(encoding="utf-8"))
            binding = _stop_binding(
                {
                    "pid": os.getpid(),
                    "process_birth": self._owner_evidence.get("birth"),
                    "started_at": self.started_at,
                    "execution": {"owner_id": self.execution_runtime.owner_id},
                }
            )
            if not _valid_stop_request(request, binding):
                return False
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return False
        self._accepted_stop_request_id = request["request_id"]
        self.stop(planned=True)
        return True

    def _cleanup(self, label, callback):
        """One failing cleanup never skips the other resources or original error."""
        try:
            callback()
            return True
        except Exception as exc:  # noqa: BLE001 -- independent cleanup must always continue
            self.failure = self.failure or f"{label}: {type(exc).__name__}"
            self._diagnostic(label, exc)
            return False

    def _diagnostic(self, stage, exc):
        detail = {"stage": stage, "exception_class": type(exc).__name__}
        for name in ("errno", "winerror"):
            value = getattr(exc, name, None)
            if type(value) is int:
                detail[name] = value
        frames, trace = [], exc.__traceback__
        while trace is not None:
            frames.append(
                {
                    "file": Path(trace.tb_frame.f_code.co_filename).name,
                    "function": trace.tb_frame.f_code.co_name,
                    "line": trace.tb_lineno,
                }
            )
            frames = frames[-5:]
            trace = trace.tb_next
        detail["frames"] = frames
        self._diagnostics.append(detail)
        self._diagnostics = self._diagnostics[-20:]
        logger.error("Host stage %s: %s", stage, type(exc).__name__)

    def _check_drained(self):
        snapshot = self.execution_runtime.health()
        if snapshot["running"] or snapshot["active"]:
            raise RuntimeError("execution drain incomplete")

    def run(self, on_started: Callable[[], None] | None = None) -> None:
        with exclusive_process_lock(
            self.execution_runtime.store.path.parent / ".service.lock"
        ) as acquired:
            if not acquired:
                raise RuntimeError("another scheduler/Bot service is already running")
            # Never construct/recover another owner's business objects, nor
            # stop its runtime in a failed contender's cleanup path.
            if (
                self.execution_runtime.running
                or self.execution_runtime.store.has_live_owner()
            ):
                raise RuntimeError("another execution owner is already running")
            self._previous_issue = self._previous_session()
            previous_handlers = {}
            rejected_owner = False
            try:
                self._phase = "register_reports"
                if self.register_reports:
                    register_report_jobs(self.execution_runtime, config=self.config)
                self._phase = "configure_bots"
                self._configure_bots()
                from .execution_health import HealthReporter

                self._phase = "health_registration"
                self.health_reporter = HealthReporter(
                    self.execution_runtime,
                    self._send_health_notice if "feishu" in self.bots else None,
                    exclusive=True,
                )
                self._record_previous_session()
                # Cleanup owns this initially stopped runtime object even if a
                # start raises after recording its new owner but before return.
                self._start_attempted = True
                self._phase = "execution_start"
                if not self.execution_runtime.start_if_unowned():
                    rejected_owner = True
                    raise RuntimeError("another execution owner is already running")
                self._execution_started = True
                with self.execution_runtime.store.connection() as db:
                    owner = db.execute(
                        "SELECT pid,birth FROM execution_owners WHERE id=?",
                        (self.execution_runtime.owner_id,),
                    ).fetchone()
                self._owner_evidence = dict(owner) if owner is not None else {}
                if threading.current_thread() is threading.main_thread():
                    for signum in (signal.SIGINT, signal.SIGTERM):
                        previous_handlers[signum] = signal.getsignal(signum)
                        signal.signal(signum, lambda *_: self.stop())
                if self.scheduler is not None:
                    self.scheduler.start()
                self._phase = "receiver_start"
                for name, bot in self.bots.items():
                    thread = threading.Thread(
                        target=self._run_bot,
                        args=(name, bot),
                        name=f"bot-{name}",
                        daemon=True,
                    )
                    self.threads[name] = thread
                    thread.start()
                if self.failure:
                    raise RuntimeError(self.failure)
                self._phase = "running_status"
                self._write_status("running")
                if on_started is not None:
                    on_started()
                self._phase = "host_loop"
                while not self.stop_event.wait(HEARTBEAT_SECONDS):
                    if self._consume_stop_request():
                        break
                    self._write_status("running")
                    if not self.execution_runtime.running:
                        self.failure = "shared execution dispatcher stopped"
                        self.stop_event.set()
                if self.failure:
                    raise RuntimeError(self.failure)
            except BaseException as exc:
                self.failure = self.failure or "service startup or runtime failed"
                self._diagnostic(self._phase, exc)
                raise
            finally:
                self._phase = "shutdown"
                self.stop(planned=False)
                try:
                    # Fence admission, drain admitted work, then close transports.
                    for name, bot in self.bots.items():
                        pause = getattr(bot, "pause_admission", None)
                        if callable(pause) and not self._cleanup(
                            f"{name} admission pause failed", pause
                        ):
                            self._cleanup(f"{name} admission close failed", bot.stop)
                    if self.scheduler is not None:
                        self._cleanup("scheduler shutdown failed", self.scheduler.stop)
                    if (
                        self.failure
                        and self.health_reporter is not None
                        and not rejected_owner
                    ):
                        self._cleanup(
                            "host failure status", lambda: self._write_status("failed")
                        )
                    if self._start_attempted:
                        self._cleanup(
                            "execution shutdown failed",
                            lambda: self.execution_runtime.stop(drain=True, timeout=20),
                        )
                        self._cleanup("execution drain incomplete", self._check_drained)
                    if self.health_reporter is not None:
                        self._cleanup(
                            "health reporter shutdown failed", self.health_reporter.stop
                        )
                    for name, bot in self.bots.items():
                        self._cleanup(f"{name} receiver shutdown failed", bot.stop)
                    for name, thread in self.threads.items():
                        self._cleanup(
                            f"{name} receiver join failed",
                            lambda thread=thread: thread.join(timeout=20),
                        )
                        if thread.is_alive():
                            self.failure = (
                                self.failure or f"{name} receiver did not stop"
                            )
                    if not rejected_owner:
                        self._cleanup(
                            "service status could not be saved",
                            lambda: self._write_status(
                                "failed" if self.failure else "stopped"
                            ),
                        )
                finally:
                    restore_failed = False
                    for signum, handler in previous_handlers.items():
                        restored = self._cleanup(
                            "signal restore failed",
                            lambda signum=signum, handler=handler: signal.signal(
                                signum, handler
                            ),
                        )
                        restore_failed = restore_failed or not restored
                    if restore_failed and not rejected_owner:
                        self._cleanup(
                            "signal failure status",
                            lambda: self._write_status("failed"),
                        )
            if self.failure:
                raise RuntimeError(self.failure)

    def _send_health_notice(self, event):
        """Notify the originating chat, or a configured single allowed route."""
        bot = self.bots.get("feishu")
        if bot is None:
            return {"outcome": "failed", "reason": "health_route_unconfigured"}
        app = bot.app
        run = self.execution_runtime.store.get(event.get("run_id", ""))
        payload = (run or {}).get("payload", {})
        settings = (self.config.get("interactive") or {}).get("feishu") or {}
        route = (
            payload.get("chat")
            if (run or {}).get("job_id") == "conversation.turn"
            else None
        )
        route = route or settings.get("health_alert_chat_id")
        allowed = list(app.allowed_chat_ids)
        if not route and len(allowed) == 1 and allowed[0] != "*":
            route = allowed[0]
        if not route or not app.gate.is_allowed(route):
            return {"outcome": "failed", "reason": "health_route_unconfigured"}
        text = (
            "【运行状态】"
            + str(event.get("job_id") or event.get("module_id") or "runtime")
            + "："
            + str(event.get("status") or event.get("kind"))
            + "\n编号："
            + str(event.get("run_id") or event.get("event_id", ""))[:32]
            + "\n"
            + str(event.get("reason", ""))[:500]
        )
        assistant = self._assistant(bot)
        if assistant is not None:
            text = assistant.redact(text)
        result = app.send_personal_message(route, text)
        if isinstance(result, dict) and result.get("platform_message_id"):
            result = dict(result, message_id=result["platform_message_id"])
        return result
