"""Shared Baostock connection, wire-request budget and persistent circuit breaker.

The provider forbids concurrent connections, not just concurrent queries.  The
OS lock spans login through local socket close; a scoped SDK transport wrapper
accounts for login, logout and pagination as well as the initial queries.
"""

from __future__ import annotations

import json
import math
import os
import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

SHANGHAI_TZ = timezone(timedelta(hours=8))
PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATE_CONTRACT = "baostock-access-1"
_SESSION_LOCK = threading.RLock()
_ACTIVE = threading.local()


class BaostockAccessBlocked(RuntimeError):
    def __init__(self, reason: str, message: str = "", *, retry_at: str | None = None):
        super().__init__(message or f"Baostock access blocked: {reason}")
        self.reason = reason
        self.retry_at = retry_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": "baostock",
            "blocked": True,
            "reason": self.reason,
            "message": str(self),
            "retry_at": self.retry_at,
        }


class BaostockTransientError(RuntimeError):
    def __init__(
        self,
        message: str = "",
        *,
        retry_at: str | None = None,
        reason: str = "transient",
    ):
        super().__init__(message or f"Baostock temporarily unavailable: {reason}")
        self.reason = reason
        self.retry_at = retry_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": "baostock",
            "blocked": False,
            "reason": self.reason,
            "message": str(self),
            "retry_at": self.retry_at,
        }


@dataclass(frozen=True)
class BaostockAccessPolicy:
    state_dir: Path = PROJECT_ROOT / "data/provider_state/baostock"
    min_request_interval_seconds: float = 2.0
    daily_request_budget: int = 40000
    retry_backoff_initial_seconds: float = 60.0
    retry_backoff_max_seconds: float = 900.0
    connection_lock_timeout_seconds: float = 1200.0

    @classmethod
    def from_config(cls, config: dict | None = None) -> BaostockAccessPolicy:
        settings = ((config or {}).get("provider_access", {}) or {}).get(
            "baostock", {}
        ) or {}
        if not isinstance(settings, dict):
            raise TypeError("provider_access.baostock must be a mapping")
        unknown = set(settings) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown Baostock access settings: {sorted(unknown)}")
        values = dict(settings)
        state_dir = Path(values.pop("state_dir", cls.state_dir)).expanduser()
        if not state_dir.is_absolute():
            state_dir = PROJECT_ROOT / state_dir
        for name, value in values.items():
            if isinstance(value, bool):
                raise TypeError(f"{name} must be a positive number")
            number = float(value)
            if not math.isfinite(number) or number <= 0:
                raise ValueError(f"{name} must be a positive finite number")
            if name == "daily_request_budget":
                if not number.is_integer() or number > 40000:
                    raise ValueError("daily_request_budget must be 1..40000")
                values[name] = int(number)
            else:
                values[name] = number
        policy = cls(state_dir=state_dir.resolve(), **values)
        if policy.retry_backoff_max_seconds < policy.retry_backoff_initial_seconds:
            raise ValueError("maximum Baostock backoff must be >= initial backoff")
        return policy


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, SHANGHAI_TZ).isoformat()


@contextmanager
def _file_lock(path: Path, timeout: float, sleep: Callable = time.sleep):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if not handle.tell():
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise BaostockTransientError(
                        f"Baostock lock busy: {path.name}", reason="lock_busy"
                    ) from exc
                sleep(min(0.1, max(0.001, deadline - time.monotonic())))
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class BaostockAccessController:
    def __init__(
        self,
        config: dict | None = None,
        *,
        clock: Callable = time.time,
        sleep: Callable = time.sleep,
    ):
        self.policy = BaostockAccessPolicy.from_config(config)
        self.clock = clock
        self.sleep = sleep
        self.state_path = self.policy.state_dir / "state.json"

    def _read(self) -> dict:
        today = datetime.fromtimestamp(self.clock(), SHANGHAI_TZ).date().isoformat()
        if not self.state_path.exists():
            return {
                "contract": STATE_CONTRACT,
                "day": today,
                "request_count": 0,
                "last_request_at": 0.0,
                "consecutive_failures": 0,
                "cooldown_until": 0.0,
                "block": None,
            }
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if state["contract"] != STATE_CONTRACT:
                raise ValueError("unsupported access-state contract")
            if datetime.fromisoformat(state["day"]).date().isoformat() != state["day"]:
                raise ValueError("invalid access-state day")
            for key in ("request_count", "consecutive_failures"):
                if type(state[key]) is not int or state[key] < 0:
                    raise ValueError(f"invalid {key}")
            for key in ("last_request_at", "cooldown_until"):
                if not math.isfinite(float(state[key])) or float(state[key]) < 0:
                    raise ValueError(f"invalid {key}")
            block = state["block"]
            if block is not None and (
                not isinstance(block, dict) or not block.get("reason")
            ):
                raise ValueError("invalid circuit-breaker state")
            if state["day"] > today:
                raise ValueError("clock precedes persisted access-state day")
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise BaostockAccessBlocked(
                "state_invalid",
                "Baostock access state is unreadable; repair before use",
            ) from exc
        if state["day"] != today:
            state["day"] = today
            state["request_count"] = 0
        return state

    def _write(self, state: dict) -> None:
        state["updated_at"] = _iso(self.clock())
        temporary = self.state_path.with_suffix(f".{os.getpid()}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.state_path)
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def _state_lock(self):
        with _file_lock(self.policy.state_dir / "state.lock", 10, self.sleep):
            yield

    @contextmanager
    def session_lock(self):
        with _file_lock(
            self.policy.state_dir / "connection.lock",
            self.policy.connection_lock_timeout_seconds,
            self.sleep,
        ):
            yield

    def _status(self, state: dict) -> dict:
        status = {
            "source": "baostock",
            "blocked": False,
            "reason": None,
            "retry_at": None,
            "day": state["day"],
            "request_count": state["request_count"],
            "remaining": max(
                0, self.policy.daily_request_budget - state["request_count"]
            ),
        }
        if state.get("budget_hold_day") == state["day"]:
            status["remaining"] = 0
        if state["block"]:
            status.update(state["block"], blocked=True)
        elif not status["remaining"] or state.get("budget_hold_day") == state["day"]:
            midnight = datetime.fromtimestamp(self.clock(), SHANGHAI_TZ).replace(
                hour=0, minute=0, second=0, microsecond=0
            ) + timedelta(days=1)
            status.update(
                blocked=True,
                reason="daily_budget",
                retry_at=midnight.isoformat(),
                remaining=0,
            )
            if state.get("budget_hold_day") == state["day"]:
                status["message"] = state.get("budget_hold_reason", "")
        elif float(state["cooldown_until"]) > self.clock():
            status.update(reason="cooldown", retry_at=_iso(state["cooldown_until"]))
        return status

    def status(self) -> dict:
        try:
            with self._state_lock():
                return self._status(self._read())
        except BaostockAccessBlocked as exc:
            return exc.to_dict()

    @staticmethod
    def _check(status: dict) -> None:
        if status["blocked"]:
            raise BaostockAccessBlocked(
                status["reason"],
                status.get("message", ""),
                retry_at=status.get("retry_at"),
            )
        if status.get("reason") == "cooldown":
            raise BaostockTransientError(
                "Baostock is cooling down after a network error",
                retry_at=status["retry_at"],
                reason="cooldown",
            )

    def check_available(self) -> None:
        self._check(self.status())

    def reserve_request(self) -> None:
        """Durably charge before sending, even when the SDK subsequently fails."""
        while True:
            with self._state_lock():
                state = self._read()
                self._check(self._status(state))
                delay = (
                    (
                        float(state["last_request_at"])
                        + self.policy.min_request_interval_seconds
                        - self.clock()
                    )
                    if state["last_request_at"]
                    else 0
                )
                if delay <= 0:
                    state["request_count"] += 1
                    state["last_request_at"] = self.clock()
                    self._write(state)
                    return
            self.sleep(delay)

    def record_blacklist(self, message: str, retry_at: str | None = None) -> None:
        with self._state_lock():
            state = self._read()
            state["block"] = {
                "reason": "blacklist",
                "message": message,
                "retry_at": retry_at,
                "blocked_at": _iso(self.clock()),
                "release_confirmation_required": True,
            }
            self._write(state)

    def record_transient(self, message: str = "") -> str:
        with self._state_lock():
            state = self._read()
            state["consecutive_failures"] += 1
            delay = min(
                self.policy.retry_backoff_max_seconds,
                self.policy.retry_backoff_initial_seconds
                * (2 ** min(30, state["consecutive_failures"] - 1)),
            )
            state["cooldown_until"] = self.clock() + delay
            self._write(state)
            return _iso(state["cooldown_until"])

    def record_success(self) -> None:
        with self._state_lock():
            state = self._read()
            state["consecutive_failures"] = 0
            state["cooldown_until"] = 0.0
            self._write(state)

    def hold_daily_budget(self, message: str) -> None:
        """Hold the remainder of a migration day whose old usage is unknown."""
        with self._state_lock():
            state = self._read()
            state["budget_hold_day"] = state["day"]
            state["budget_hold_reason"] = message
            self._write(state)

    def apply_official_status(self, payload: dict, checked_at: float) -> dict:
        """Update the circuit from the official IP-status endpoint, never a timer.

        Callers must query the current egress, supply the unmodified response,
        and record the time BEFORE querying. No IP or user id is persisted.
        """
        try:
            rows = payload["stats"]["data"]
            total = payload["stats"]["total"]
            if (
                not isinstance(rows, list)
                or type(total) is not int
                or total < len(rows)
            ):
                raise ValueError("invalid blacklist result count")
            if not math.isfinite(checked_at) or checked_at > self.clock():
                raise ValueError("invalid official check time")
            for row in rows:
                if type(row["effectiveStatus"]) is not int or row[
                    "effectiveStatus"
                ] not in (0, 1):
                    raise ValueError("unknown official restriction status")
            active = [row for row in rows if row["effectiveStatus"] == 1]
            if not active and total != len(rows):
                raise ValueError("incomplete official status cannot clear blacklist")
            pending = []
            for row in active:
                value = row.get("pendingReleaseDate")
                if value:
                    when = datetime.fromisoformat(str(value))
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=SHANGHAI_TZ)
                    pending.append(when.timestamp())
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid or incomplete official Baostock status") from exc
        with self._state_lock():
            state = self._read()
            block = state["block"]
            if (
                block
                and block.get("blocked_at")
                and datetime.fromisoformat(block["blocked_at"]).timestamp() > checked_at
            ):
                return self._status(state)
            state["official_checked_at"] = _iso(checked_at)
            if active:
                state["block"] = {
                    "reason": "blacklist",
                    "message": "Official Baostock IP blacklist is active",
                    "retry_at": _iso(max(pending))
                    if len(pending) == len(active)
                    else None,
                    "blocked_at": _iso(checked_at),
                    "release_confirmation_required": True,
                }
            elif block and block.get("reason") == "blacklist":
                state["block"] = None
            self._write(state)
            return self._status(state)

    def check_result(self, result: Any) -> None:
        if result is None:
            retry_at = self.record_transient()
            raise BaostockTransientError(
                "Baostock returned no response", retry_at=retry_at
            )
        self.check_error(
            str(getattr(result, "error_code", "0")),
            str(getattr(result, "error_msg", "")),
        )

    def check_error(self, code: str, message: str) -> None:
        if code == "0":
            return
        if code == "10001011" or "黑名单" in message or "blacklist" in message.lower():
            self.record_blacklist(message)
            raise BaostockAccessBlocked("blacklist", message)
        if code.startswith("10002") or code == "10005001":
            retry_at = self.record_transient(message)
            raise BaostockTransientError(
                f"Baostock {code}: {message}", retry_at=retry_at
            )
        raise RuntimeError(f"Baostock {code}: {message}")


class _CheckedResult:
    def __init__(self, result: Any, controller: BaostockAccessController):
        self._result = result
        self._controller = controller

    def __getattr__(self, name: str):
        return getattr(self._result, name)

    def next(self) -> bool:
        try:
            available = self._result.next()
        except (IndexError, ValueError, TypeError) as exc:
            retry_at = self._controller.record_transient("invalid paginated response")
            raise BaostockTransientError(
                "Malformed Baostock paginated response", retry_at=retry_at
            ) from exc
        self._controller.check_result(self._result)
        return available


class _GuardedModule:
    def __init__(self, module: Any, controller: BaostockAccessController, wire: bool):
        self.module = module
        self.controller = controller
        self.wire = wire

    def invoke(self, method: Callable, *args, **kwargs):
        self.controller.check_available()
        if not self.wire:
            # Dependency-injected test adapters have no SDK wire transport.
            self.controller.reserve_request()
        try:
            result = method(*args, **kwargs)
        except (IndexError, ValueError, OSError) as exc:
            retry_at = self.controller.record_transient("invalid SDK response")
            raise BaostockTransientError(
                f"Baostock request/response failed: {type(exc).__name__}",
                retry_at=retry_at,
            ) from exc
        self.controller.check_result(result)
        return _CheckedResult(result, self.controller)

    def __getattr__(self, name: str):
        value = getattr(self.module, name)
        if name.startswith("query_") and callable(value):
            return lambda *args, **kwargs: self.invoke(value, *args, **kwargs)
        return value


class _EofCheckingSocket:
    """The SDK loops forever on recv(b''); turn EOF into its error path."""

    def __init__(self, connection: Any):
        self.connection = connection

    def __getattr__(self, name: str):
        return getattr(self.connection, name)

    def recv(self, *args, **kwargs):
        chunk = self.connection.recv(*args, **kwargs)
        if chunk == b"":
            raise ConnectionResetError("Baostock server closed the connection")
        return chunk


@contextmanager
def _sdk_transport(module: Any, controller: BaostockAccessController):
    if getattr(module, "__name__", None) != "baostock":
        yield False
        return
    import baostock.common.contants as constants
    import baostock.util.socketutil as transport
    from baostock.common import context

    original = transport.send_msg

    def send(message):
        controller.reserve_request()
        connection = getattr(context, "default_socket", None)
        if connection is not None and not isinstance(connection, _EofCheckingSocket):
            context.default_socket = _EofCheckingSocket(connection)
        try:
            response = original(message)
        except OSError as exc:
            retry_at = controller.record_transient(str(exc))
            raise BaostockTransientError(str(exc), retry_at=retry_at) from exc
        if not isinstance(response, str) or not response.strip():
            controller.check_result(None)
        body = response[constants.MESSAGE_HEADER_LENGTH :].split(
            constants.MESSAGE_SPLIT
        )
        if len(body) < 2 or (body[0] == "0" and len(body) < 4):
            retry_at = controller.record_transient("malformed wire response")
            raise BaostockTransientError(
                "Malformed Baostock response", retry_at=retry_at
            )
        controller.check_error(body[0], body[1])
        return response

    transport.send_msg = send
    try:
        yield True
    finally:
        transport.send_msg = original


def _close_sdk_socket(module: Any) -> None:
    if getattr(module, "__name__", None) == "baostock":
        from baostock.common import context

        connection = getattr(context, "default_socket", None)
        try:
            if connection is not None:
                connection.close()
        finally:
            context.default_socket = None


@contextmanager
def guarded_baostock_session(
    module: Any,
    timeout_seconds: float,
    config: dict | None = None,
    *,
    controller: BaostockAccessController | None = None,
):
    controller = controller or BaostockAccessController(config)
    with _SESSION_LOCK:
        active = getattr(_ACTIVE, "module", None)
        if active is not None:
            if (
                active.module is not module
                or active.controller.policy != controller.policy
            ):
                raise RuntimeError("cannot nest a different Baostock session")
            active.controller.check_available()
            yield active
            return
        controller.check_available()
        with controller.session_lock():
            controller.check_available()
            previous_timeout = socket.getdefaulttimeout()
            socket.setdefaulttimeout(timeout_seconds)
            completed = False
            try:
                with _sdk_transport(module, controller) as wire:
                    guarded = _GuardedModule(module, controller, wire)
                    guarded.invoke(module.login)
                    _ACTIVE.module = guarded
                    try:
                        yield guarded
                        completed = True
                    finally:
                        _ACTIVE.module = None
                    # On any exception, close locally: never send another request
                    # into a ban, exhausted quota, or failed socket.
                    status = controller.status()
                    if not status["blocked"] and status.get("reason") != "cooldown":
                        guarded.invoke(module.logout)
                    if completed:
                        controller.record_success()
            finally:
                try:
                    _close_sdk_socket(module)
                finally:
                    socket.setdefaulttimeout(previous_timeout)
