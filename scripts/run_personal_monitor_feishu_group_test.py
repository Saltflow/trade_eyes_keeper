"""Poll exactly one authorized test group without another WebSocket receiver.

Credentials are read into memory from whitelisted local production fields.
Only personal tools and an independent test SQLite database are used. Probe
authenticates and GETs permissions; it never builds the bridge or sends messages.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.personal_monitor_process import FileLease
from scripts.personal_monitor_transport import make_transport, validate_profile_binding
from src.core.runtime_files import atomic_write_json

API = "https://open.feishu.cn/open-apis"


def read_credentials(root=ROOT, environ=None):
    """No dotenv loader, interpolation, logging or export to os.environ."""
    environ = os.environ if environ is None else environ
    raw = (
        yaml.safe_load((root / "config" / "config.yaml").read_text(encoding="utf-8"))
        or {}
    )
    dotenv = {}
    keys = {"FEISHU_APP_ID", "FEISHU_APP_SECRET", "DEEPSEEK_API_KEY"}
    for path in (root / ".env", root / "config" / ".env"):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.fullmatch(r"\s*(?:export\s+)?([A-Z_]+)\s*=\s*(.*?)\s*", line)
            if not match or match[1] not in keys:
                continue
            value = match[2]
            if value[:1] in {"'", '"'}:
                if len(value) < 2 or value[-1] != value[0]:
                    raise ValueError("本地凭据格式无法安全读取。")
                value = value[1:-1]
            else:
                value = value.split(" #", 1)[0].strip()
            if "${" in value:
                raise ValueError("测试工具不展开凭据变量表达式。")
            dotenv[match[1]] = value
    feishu = (raw.get("interactive") or {}).get("feishu") or {}
    llm = raw.get("llm") or {}
    values = {
        "app_id": feishu.get("app_id")
        or environ.get("FEISHU_APP_ID")
        or dotenv.get("FEISHU_APP_ID"),
        "app_secret": feishu.get("app_secret")
        or environ.get("FEISHU_APP_SECRET")
        or dotenv.get("FEISHU_APP_SECRET"),
        "api_key": environ.get("DEEPSEEK_API_KEY")
        or dotenv.get("DEEPSEEK_API_KEY")
        or llm.get("api_key"),
    }
    if not all(
        isinstance(values[key], str) and values[key].strip()
        for key in ("app_id", "app_secret")
    ):
        raise ValueError("本机缺少现有应用凭据，无法进行只读权限检查。")
    return {
        key: (value.strip() if isinstance(value, str) else "")
        for key, value in values.items()
    }


def load_settings(path, *, root=ROOT, environ=None, probe=False, control_only=False):
    if path.resolve() == (root / "config" / "config.yaml").resolve():
        raise ValueError("必须使用独立群测试配置。")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    allowed = {
        "test_mode",
        "chat_id",
        "allowed_sender_ids",
        "workspace_name",
        "market_check_times",
        "poll_seconds",
    }
    if (
        not isinstance(raw, dict)
        or raw.get("test_mode") is not True
        or set(raw) - allowed
    ):
        raise ValueError("群测试配置字段不合法。")
    chat = raw.get("chat_id")
    owners = raw.get("allowed_sender_ids")
    if not isinstance(chat, str) or not re.fullmatch(r"oc_[A-Za-z0-9_-]+", chat):
        raise ValueError("必须指定唯一群 chat_id，不能使用通配符。")
    if (
        not isinstance(owners, list)
        or not owners
        or any(
            not isinstance(owner, str) or not re.fullmatch(r"ou_[A-Za-z0-9_-]+", owner)
            for owner in owners
        )
    ):
        raise ValueError("必须指定发送者 open_id 白名单。")
    name = raw.get("workspace_name")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", name):
        raise ValueError("群测试工作区名称不合法。")
    base = root / "data" / "personal_monitor_feishu_group_test"
    workspace = base / name
    if base.is_symlink() or not workspace.resolve().is_relative_to(
        root.resolve() / "data" / "personal_monitor_feishu_group_test"
    ):
        raise ValueError("群测试工作区不能指向其他位置。")
    if control_only:
        return {"workspace": workspace}
    times = raw.get("market_check_times", ["09:50", "14:30", "19:00"])
    if (
        not isinstance(times, list)
        or not 1 <= len(times) <= 8
        or any(
            not isinstance(at, str)
            or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", at)
            for at in times
        )
    ):
        raise ValueError("检查时点必须使用 HH:MM。")
    poll = raw.get("poll_seconds", 5)
    if type(poll) not in (int, float) or not 2 <= poll <= 60:
        raise ValueError("poll_seconds 必须在 2 至 60 秒之间。")
    credentials = read_credentials(root, environ)
    if not probe and not credentials["api_key"]:
        raise ValueError("缺少模型凭据；只读 probe 不需要模型密钥。")
    return dict(
        raw,
        **credentials,
        allowed_chat_ids=[chat],
        workspace=workspace,
        market_check_times=times,
        poll_seconds=poll,
        _scope_label="独立单群测试",
    )


def validate_production_filter(path, settings, *, root=ROOT, now=None):
    if path is None:
        raise ValueError("缺少生产群过滤核验文件，禁止启动群测试。")
    path = Path(path)
    if path.is_symlink() or not path.resolve().is_relative_to(
        root.resolve() / "data" / "deployment_audit"
    ):
        raise ValueError("核验文件必须来自受控 deployment_audit 目录。")
    content = path.read_bytes()
    proof = json.loads(content)
    if not isinstance(proof, dict):
        raise ValueError("生产核验文件必须为对象。")  # noqa: TRY004
    current = now or datetime.now(timezone.utc)
    checked = datetime.fromisoformat(
        str(proof.get("checked_at", "")).replace("Z", "+00:00")
    )
    if (
        checked.tzinfo is None
        or current.tzinfo is None
        or not -60 <= (current - checked).total_seconds() <= 86400
    ):
        raise ValueError("生产群过滤核验已过期或时间无效。")
    if (
        proof.get("app_id") != settings["app_id"]
        or proof.get("chat_id") != settings["chat_id"]
        or proof.get("group_no_mention_ignored") is not True
    ):
        raise ValueError("生产核验与当前应用或群不匹配。")
    if not isinstance(proof.get("handler_sha256"), str) or not re.fullmatch(
        r"[a-fA-F0-9]{64}", proof["handler_sha256"]
    ):
        raise ValueError("生产核验缺少有效处理器哈希。")
    return hashlib.sha256(content).hexdigest()


class FeishuReadError(RuntimeError):
    def __init__(self, operation, code=None, http_status=None, *, retryable=False):
        self.operation, self.code, self.http_status = operation, code, http_status
        self.retryable = retryable
        super().__init__(
            f"Feishu read failed: {operation}; code={code}; HTTP={http_status}"
        )


class GroupAPI:
    def __init__(self, transport, chat_id, *, get=None):
        self.transport, self.chat_id = transport, chat_id
        self.get = get or requests.get

    def read(self, endpoint, operation, params=None):
        token = self.transport.get_tenant_token()
        if not token:
            raise FeishuReadError("auth")
        try:
            response = self.get(
                API + endpoint,
                params=params,
                headers={"Authorization": "Bearer " + token},
                timeout=(5, 20),
                allow_redirects=False,
            )
            if response.status_code == 429 or 500 <= response.status_code <= 599:
                # Proxy/overload pages may be HTML. Classify by HTTP before JSON.
                raise FeishuReadError(
                    operation, http_status=response.status_code, retryable=True
                )
            try:
                payload = response.json()
            except ValueError:
                raise FeishuReadError(
                    operation, http_status=response.status_code
                ) from None
            code = payload.get("code") if isinstance(payload, dict) else None
            if response.status_code != 200 or type(code) is not int or code != 0:
                raise FeishuReadError(
                    operation, code if type(code) is int else None, response.status_code
                )
            data = payload.get("data")
            if not isinstance(data, dict):
                raise FeishuReadError(operation, http_status=response.status_code)
            return data
        except (
            requests.Timeout,
            requests.ConnectionError,
            requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ContentDecodingError,
        ) as exc:
            raise FeishuReadError(
                operation, retryable=not isinstance(exc, requests.exceptions.SSLError)
            ) from None
        except (requests.RequestException, ValueError, TypeError):
            raise FeishuReadError(operation) from None

    def history_page(self, start_ms, end_ms, token=None):
        params = {
            "container_id_type": "chat",
            "container_id": self.chat_id,
            "start_time": str(start_ms // 1000),
            "end_time": str(end_ms // 1000),
            "sort_type": "ByCreateTimeAsc",
            "page_size": 50,
        }
        if token:
            params["page_token"] = token
        return self.read("/im/v1/messages", "history", params)

    def probe(self, now_ms=None, *, raise_errors=False):
        result = {}
        now_ms = now_ms or int(time.time() * 1000)
        for operation in ("chat", "history"):
            try:
                if operation == "chat":
                    data = self.read(f"/im/v1/chats/{self.chat_id}", "chat")
                    if data.get("chat_mode") != "group":
                        raise FeishuReadError("chat_not_group")
                else:
                    self.history_page(now_ms, now_ms + 1000)
                result[operation] = {"ok": True, "code": 0}
            except FeishuReadError as exc:
                if raise_errors:
                    raise
                result[operation] = {
                    "ok": False,
                    "code": exc.code,
                    "http_status": exc.http_status,
                }
        return result

    def collect_window(self, start_ms, end_ms, *, max_pages=200):
        """Freeze boundaries and exhaust pages before processing or advancing."""
        token, seen_tokens, items = None, set(), {}
        for _page in range(max_pages):
            data = self.history_page(start_ms, end_ms, token)
            page = data.get("items", [])
            if not isinstance(page, list) or type(data.get("has_more")) is not bool:
                raise FeishuReadError("invalid_history_page")
            for item in page:
                if not isinstance(item, dict) or not isinstance(
                    item.get("message_id"), str
                ):
                    raise FeishuReadError("invalid_history_item")
                key = item["message_id"]
                if key in items and items[key] != item:
                    # A message edited during pagination cannot become fresh intent.
                    items[key] = dict(item, updated=True)
                else:
                    items[key] = item
            if not data["has_more"]:
                return sorted(
                    items.values(),
                    key=lambda item: (
                        int(item.get("create_time", 0)),
                        item["message_id"],
                    ),
                )
            token = data.get("page_token")
            if not isinstance(token, str) or not token or token in seen_tokens:
                raise FeishuReadError("invalid_history_pagination")
            seen_tokens.add(token)
        raise FeishuReadError("history_page_limit")


def item_to_event(item, chat_id, owners):
    """Adapt actual API fields without relabeling a group as private/SDK input."""
    from src.interactive.feishu_handler import _message_text

    if (
        item.get("chat_id") != chat_id
        or item.get("deleted") is not False
        or item.get("updated") is not False
        or item.get("mentions")
    ):
        return None
    sender = item.get("sender") or {}
    if not isinstance(sender, dict):
        return None
    if (
        sender.get("sender_type") != "user"
        or sender.get("id_type") != "open_id"
        or sender.get("id") not in owners
    ):
        return None
    created, updated = item.get("create_time"), item.get("update_time")
    if (
        not isinstance(created, str)
        or not re.fullmatch(r"\d{13,16}", created)
        or (updated is not None and updated != created)
    ):
        return None
    kind = item.get("msg_type")
    body = item.get("body") or {}
    if not isinstance(body, dict):
        return None
    content = body.get("content")
    if kind not in {"text", "image", "post"} or not isinstance(content, str):
        return None
    try:
        decoded = json.loads(content)
    except (ValueError, TypeError):
        return None
    if not isinstance(decoded, dict):
        return None

    def mentions(value):
        if isinstance(value, dict):
            return value.get("tag") == "at" or any(
                mentions(part) for part in value.values()
            )
        if isinstance(value, list):
            return any(mentions(part) for part in value)
        return isinstance(value, str) and "@" in value

    if mentions(decoded):
        return None
    message = {
        "chat_id": chat_id,
        "chat_type": "group",
        "message_id": item.get("message_id"),
        "message_type": kind,
        "content": content,
        "create_time": created,
        "update_time": updated,
    }
    if _message_text(message).lstrip().startswith("/"):
        return None
    return {
        "header": {
            "event_type": "im.message.receive_v1",
            "source": "feishu_history_api",
        },
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": sender["id"]}},
            "message": message,
        },
    }


def group_identity(body):
    if (body.get("header") or {}).get("source") != "feishu_history_api":
        return None
    event = body.get("event") or {}
    message, sender = event.get("message") or {}, event.get("sender") or {}
    if message.get("chat_type") != "group" or sender.get("sender_type") != "user":
        return None
    owner = (sender.get("sender_id") or {}).get("open_id")
    if not all(
        isinstance(value, str) and value
        for value in (owner, message.get("chat_id"), message.get("message_id"))
    ):
        return None
    return owner, message["chat_id"], message["message_id"], message


class GroupState:
    def __init__(self, settings, *, started_ms=None):
        workspace = settings["workspace"]
        workspace.mkdir(parents=True, exist_ok=True)
        self.path = workspace / "test.sqlite3"
        if self.path.is_symlink() or not self.path.resolve().is_relative_to(
            workspace.resolve()
        ):
            raise ValueError("群测试数据库不能指向其他位置。")
        with sqlite3.connect(self.path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS group_binding (app_id TEXT,chat_id TEXT,origin_ms INTEGER,completed_until_ms INTEGER)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS group_messages (message_id TEXT PRIMARY KEY,create_time TEXT,state TEXT)"
            )
            binding = db.execute("SELECT * FROM group_binding").fetchall()
            if binding and (
                len(binding) != 1
                or binding[0][:2] != (settings["app_id"], settings["chat_id"])
            ):
                raise ValueError("群测试工作区已绑定其他应用或群。")
            if not binding:
                origin = (
                    started_ms if started_ms is not None else int(time.time() * 1000)
                )
                db.execute(
                    "INSERT INTO group_binding VALUES(?,?,?,?)",
                    (settings["app_id"], settings["chat_id"], origin, origin),
                )
            # Crossing model/tool/send boundaries may have committed side effects.
            # An interrupted message stays uncertain rather than being re-executed.
            db.execute(
                "UPDATE group_messages SET state='interrupted_unknown' WHERE state='processing'"
            )
            self.origin_ms = db.execute(
                "SELECT origin_ms FROM group_binding"
            ).fetchone()[0]

    def claim(self, item):
        with sqlite3.connect(self.path) as db:
            return (
                db.execute(
                    "INSERT OR IGNORE INTO group_messages VALUES(?,?,'processing')",
                    (item["message_id"], item.get("create_time")),
                ).rowcount
                == 1
            )

    def finish(self, message_id, status):
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT INTO group_messages(message_id,state) VALUES(?,?) "
                "ON CONFLICT(message_id) DO UPDATE SET state=excluded.state",
                (message_id, status),
            )

    def seen(self, message_id):
        with sqlite3.connect(self.path) as db:
            return (
                db.execute(
                    "SELECT 1 FROM group_messages WHERE message_id=?", (message_id,)
                ).fetchone()
                is not None
            )

    def advance(self, until_ms):
        with sqlite3.connect(self.path) as db:
            db.execute(
                "UPDATE group_binding SET completed_until_ms=MAX(completed_until_ms,?)",
                (until_ms,),
            )


class GroupPoller:
    def __init__(self, settings, api, state, bridge, *, activity=None):
        self.settings, self.api, self.state, self.bridge = settings, api, state, bridge
        self.activity = activity or (lambda **_values: None)

    def poll_once(self, now_ms=None):
        self.activity(activity="polling")
        # Query complete seconds only. Re-scan the durable session origin to
        # include late indexing and every same-second/page-boundary message.
        until = (
            (now_ms if now_ms is not None else int(time.time() * 1000)) // 1000 - 2
        ) * 1000
        if until <= self.state.origin_ms:
            return {"processed": 0, "ignored": 0}
        items = self.api.collect_window(self.state.origin_ms, until)
        counts = {"processed": 0, "ignored": 0}
        for item in items:
            try:
                created = int(item.get("create_time", 0))
            except (ValueError, TypeError):
                continue
            durable = getattr(self.bridge, "durable_admission", False) is True
            if not self.state.origin_ms <= created < until:
                continue
            if (
                self.state.seen(item["message_id"])
                if durable
                else not self.state.claim(item)
            ):
                continue
            event = item_to_event(
                item, self.settings["chat_id"], self.settings["allowed_sender_ids"]
            )
            status = "ignored"
            if event:
                self.activity(
                    activity="processing", activity_message_id=item["message_id"]
                )
                try:
                    status = self.bridge.process(event)
                except Exception:
                    if durable:
                        # Shared admission is transactional and idempotent. A
                        # failed admission must not consume the history cursor.
                        raise
                    status = "interrupted_unknown"
                finally:
                    self.activity(activity="polling")
                counts["processed"] += 1
            else:
                counts["ignored"] += 1
            self.state.finish(item["message_id"], status)
        self.state.advance(until)
        return counts


def safe_error(exc):
    """Keep diagnostics without exception text, absolute paths or frame locals."""
    operations = {
        "auth",
        "chat",
        "chat_not_group",
        "history",
        "invalid_history_page",
        "invalid_history_item",
        "invalid_history_pagination",
        "history_page_limit",
    }
    return {
        "error_type": type(exc).__name__,
        "operation": exc.operation
        if isinstance(exc, FeishuReadError) and exc.operation in operations
        else None,
        "code": exc.code
        if isinstance(exc, FeishuReadError) and type(exc.code) is int
        else None,
        "http_status": exc.http_status
        if isinstance(exc, FeishuReadError) and type(exc.http_status) is int
        else None,
        "retryable": isinstance(exc, FeishuReadError) and exc.retryable is True,
        "errno": exc.errno if type(getattr(exc, "errno", None)) is int else None,
        "winerror": exc.winerror
        if type(getattr(exc, "winerror", None)) is int
        else None,
        "frames": [
            {
                "file": re.sub(
                    r"[^A-Za-z0-9_.<>-]",
                    "_",
                    frame.filename.replace("\\", "/").rsplit("/", 1)[-1],
                )[:128],
                "line": frame.lineno,
                "function": re.sub(r"[^A-Za-z0-9_.<>-]", "_", frame.name)[:128],
            }
            for frame in traceback.extract_tb(exc.__traceback__, limit=8)
        ],
    }


def process_identity(pid):
    """Read process identity, distinguishing inaccessible from absent processes."""
    import psutil

    try:
        process = psutil.Process(pid)
        return {
            "alive": process.is_running() and process.status() != psutil.STATUS_ZOMBIE,
            "started_at": process.create_time(),
            "argv": process.cmdline(),
            "cwd": process.cwd(),
        }
    except psutil.NoSuchProcess:
        return {"alive": False}
    except psutil.Error:
        raise OSError("process inspection unavailable") from None


def read_runtime_status(
    settings, *, inspector=process_identity, now=None, processing_timeout_seconds=900
):
    """A saved 'running' label alone is never evidence of a live receiver."""
    path = settings["workspace"] / "poller_status.json"
    if not path.exists():
        return {"state": "not_started", "healthy": False}
    saved = json.loads(path.read_text(encoding="utf-8"))
    result = {
        "recorded_state": saved.get("state"),
        "pid": saved.get("pid"),
        "started_at": saved.get("process_started_at"),
        "process_started_at": saved.get("process_started_at"),
        "updated_at": saved.get("updated_at"),
        "activity": saved.get("activity"),
        "activity_message_id": saved.get("activity_message_id"),
        "phase_started_at": saved.get("phase_started_at"),
        "last_success_at": saved.get("last_success_at"),
        "last_error": saved.get("last_error"),
        "shared_execution": saved.get("shared_execution"),
        "healthy": False,
    }
    current = now or datetime.now(timezone.utc)
    try:
        stamp = datetime.fromisoformat(saved["updated_at"])
        age = (current - stamp).total_seconds()
        phase_stamp = datetime.fromisoformat(
            saved.get("phase_started_at") or stamp.isoformat()
        )
        phase_age = (current - phase_stamp).total_seconds()
    except (KeyError, ValueError, TypeError):
        return dict(result, state="invalid_status")
    result["heartbeat_age_seconds"] = round(age, 1)
    result["phase_age_seconds"] = round(phase_age, 1)
    pid = saved.get("pid")
    if type(pid) is not int or pid <= 0:
        return dict(result, state="invalid_status")
    try:
        identity = inspector(pid)
    except (ImportError, OSError, RuntimeError):
        return dict(result, state="process_unverified")
    if not identity.get("alive"):
        return dict(result, state="not_running")
    if saved.get("process_started_at") != identity.get("started_at"):
        return dict(result, state="process_identity_mismatch")
    script = Path(__file__).resolve()
    cwd = Path(identity.get("cwd", ""))
    if not any(
        (cwd / arg).resolve() == script
        for arg in identity.get("argv", [])
        if isinstance(arg, str) and arg.endswith(".py")
    ):
        return dict(result, state="process_identity_mismatch")
    processing = saved.get("activity") == "processing"
    budget = processing_timeout_seconds if processing else 180
    if (
        age < -5
        or phase_age < -5
        or age > budget
        or (processing and phase_age > budget)
    ):
        return dict(result, state="stale")
    state = saved.get("state")
    if saved.get("shared_runtime"):
        execution = saved.get("shared_execution") or {}
        if not execution.get("running"):
            return dict(result, state="failed", healthy=False)
        if not execution.get("ready"):
            return dict(result, state="degraded", healthy=False)
    return dict(result, state=state, healthy=state == "running")


class RuntimeStatus:
    """Durable local operational evidence; never stores requests or message text."""

    def __init__(self, settings, *, clock=None, sleep=None, replace_attempts=8):
        if type(replace_attempts) is not int or not 1 <= replace_attempts <= 8:
            raise ValueError("状态提交重试次数必须在1至8之间。")
        self._lock = threading.RLock()
        self._sleep = sleep or time.sleep
        self._replace_attempts = replace_attempts
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        settings["workspace"].mkdir(parents=True, exist_ok=True)
        self.path = settings["workspace"] / "poller_status.json"
        try:
            started_at = process_identity(os.getpid())["started_at"]
        except (ImportError, OSError):
            # Optional local diagnostics must not disable message reception.
            # --status will report process_unverified if inspection is missing.
            started_at = None
        self.payload = {
            "pid": os.getpid(),
            "process_started_at": started_at,
            "state": "starting",
            "activity": "starting",
            "activity_message_id": None,
            "phase_started_at": self.clock().isoformat(),
            "last_error": None,
            "consecutive_read_failures": 0,
            "retry_delay_seconds": 0,
            "last_success_at": None,
        }
        if self.path.is_symlink():
            raise ValueError("运行状态文件不能指向其他位置。")
        if self.path.exists():
            try:
                previous = json.loads(self.path.read_text(encoding="utf-8"))
                previous = previous.get("last_error") or {}
                known_types = {
                    "FeishuReadError",
                    "PermissionError",
                    "FileNotFoundError",
                    "OSError",
                    "RuntimeError",
                    "ValueError",
                    "TypeError",
                    "KeyError",
                }
                if previous.get("error_type") in known_types:
                    detail = safe_error(
                        FeishuReadError(
                            previous.get("operation"),
                            previous.get("code"),
                            previous.get("http_status"),
                            retryable=previous.get("retryable") is True,
                        )
                    )
                    detail["error_type"] = previous["error_type"]
                    for key in ("errno", "winerror"):
                        if type(previous.get(key)) is int:
                            detail[key] = previous[key]
                    detail["frames"] = [
                        {
                            "file": frame["file"],
                            "line": frame["line"],
                            "function": frame["function"],
                        }
                        for frame in (previous.get("frames") or [])[-8:]
                        if isinstance(frame, dict)
                        and type(frame.get("line")) is int
                        and frame["line"] > 0
                        and all(
                            isinstance(frame.get(key), str)
                            and re.fullmatch(r"[A-Za-z0-9_.<>-]{1,128}", frame[key])
                            for key in ("file", "function")
                        )
                    ]
                    stamp = previous.get("at")
                    if isinstance(stamp, str) and len(stamp) <= 40:
                        detail["at"] = datetime.fromisoformat(stamp).isoformat()
                    self.payload["last_error"] = detail
            except (ValueError, TypeError, AttributeError):
                pass
        self.update()

    def update(self, **values):
        # The activity callback and receiving thread share this instance. A
        # coherent payload and its disk commit must have the same lock owner.
        with self._lock:
            now = self.clock().isoformat()
            if "activity" in values:
                values.setdefault("activity_message_id", None)
                if any(
                    values[key] != self.payload.get(key)
                    for key in ("activity", "activity_message_id")
                ):
                    self.payload["phase_started_at"] = now
            self.payload.update(values)
            if (
                self.payload.get("shared_runtime")
                and self.payload.get("state") == "running"
            ):
                execution = self.payload.get("shared_execution") or {}
                if not execution.get("running"):
                    self.payload["state"] = "failed"
                elif not execution.get("ready"):
                    self.payload["state"] = "degraded"
            self.payload["updated_at"] = now
            if self.path.is_symlink():
                raise ValueError("运行状态文件不能指向其他位置。")
            atomic_write_json(
                self.path,
                self.payload,
                indent=2,
                replace_attempts=self._replace_attempts,
                sleep=self._sleep,
            )

    def error(self, exc, *, failures=0, delay=0):
        with self._lock:
            detail = dict(safe_error(exc), at=self.clock().isoformat())
            try:
                self.update(
                    state="retrying" if delay else "failed",
                    last_error=detail,
                    consecutive_read_failures=failures,
                    retry_delay_seconds=delay,
                )
            except OSError as write_error:
                # Retain the original failure even if its durable diagnostic
                # cannot be committed; do not replace it with a bare class name.
                print(
                    json.dumps(
                        {
                            "poller_state": "failed",
                            "last_error": detail,
                            "status_write_error": safe_error(write_error),
                        }
                    ),
                    file=sys.stderr,
                    flush=True,
                )
                raise
            print(
                json.dumps(
                    {
                        "poller_state": self.payload["state"],
                        "last_error": detail,
                        "retry_delay_seconds": delay,
                    }
                ),
                file=sys.stderr,
                flush=True,
            )


class _StopRequested(Exception):
    pass


def read_with_retry(read, *, validate, status, stop_event, wait):
    """Retry only safe reads; proof expiry and stable errors remain fatal."""
    failures = 0
    while not stop_event.is_set():
        validate()
        try:
            result = read()
        except FeishuReadError as exc:
            if not exc.retryable:
                raise
            failures += 1
            delay = min(60, 2 ** min(failures, 6))
            status.error(exc, failures=failures, delay=delay)
            if wait(delay):
                raise _StopRequested
            continue
        # Permission proof may expire while a GET or complete pagination runs.
        validate()
        status.update(
            state="running",
            consecutive_read_failures=0,
            retry_delay_seconds=0,
            last_success_at=datetime.now(timezone.utc).isoformat(),
        )
        return result
    raise _StopRequested


def run_probe(settings):
    """Read-only authenticated permission probe; no business stores or Host."""
    transport = make_transport(settings)
    try:
        checks = GroupAPI(transport, settings["chat_id"]).probe()
        print(json.dumps({"probe": checks}, ensure_ascii=False), flush=True)
        return 0 if all(item["ok"] for item in checks.values()) else 2
    finally:
        transport.stop()


class HistoryBot:
    """One host-owned history transport; business work belongs to the host."""

    def __init__(self, settings, runtime, proof_path, *, root=ROOT):
        from scripts.personal_monitor_shared_test import SharedPersonalTestBridge

        self.settings, self.runtime = settings, runtime
        self.proof_path, self.root = proof_path, root
        self._stop = threading.Event()
        self._paused = threading.Event()
        validate_production_filter(proof_path, settings, root=root)
        if (
            runtime.store.path.resolve()
            != (settings["workspace"] / "test.sqlite3").resolve()
        ):
            raise ValueError("history receiver must use the original profile SQLite")

        def allowed(route):
            if route != settings["chat_id"]:
                return False
            try:
                validate_production_filter(proof_path, settings, root=root)
                return True
            except (ValueError, OSError, KeyError, TypeError):
                return False

        self.app = make_transport(settings, route_guard=allowed)
        self.bridge = None
        try:
            self.bridge = SharedPersonalTestBridge(
                settings,
                transport=self.app,
                identity_resolver=group_identity,
                execution_runtime=runtime,
            )
            self.state = GroupState(settings)
            self.status = RuntimeStatus(settings)
            self.api = GroupAPI(self.app, settings["chat_id"])
            self.poller = GroupPoller(
                settings,
                self.api,
                self.state,
                self.bridge,
                activity=self.status.update,
            )
        except BaseException:
            if self.bridge is not None:
                self.bridge.stop()
            self.app.stop()
            raise

    @property
    def assistant(self):
        return self.bridge.assistant

    def validate_config(self):
        validate_production_filter(self.proof_path, self.settings, root=self.root)
        if (
            self.runtime.store.path.resolve()
            != (self.settings["workspace"] / "test.sqlite3").resolve()
        ):
            raise ValueError("history receiver must use the original profile SQLite")

    def health(self):
        with self.status._lock:
            payload = dict(self.status.payload)
        state = payload["state"]
        return {
            "ready": state == "running" and not self._paused.is_set(),
            "state": state,
            "evidence": "authenticated_history_GET",
            "last_success_at": payload.get("last_success_at"),
            "last_error": payload.get("last_error"),
            "issues": [],
        }

    def pause_admission(self):
        self._paused.set()
        self.bridge.pause_admission()

    def run(self):
        from scripts.personal_monitor_shared_test import AdmissionPaused

        def validate():
            if self._stop.is_set():
                raise _StopRequested
            if self._paused.is_set():
                raise AdmissionPaused("history admission paused")
            self.validate_config()
            if not self.runtime.running:
                raise RuntimeError("Shared execution dispatcher stopped")

        def read(operation):
            return read_with_retry(
                operation,
                validate=validate,
                status=self.status,
                stop_event=self._stop,
                wait=self._stop.wait,
            )

        try:
            checks = read(lambda: self.api.probe(raise_errors=True))
            if not all(item["ok"] for item in checks.values()):
                raise RuntimeError("history permission probe failed")
            while not self._stop.is_set():
                if self._paused.is_set():
                    self._stop.wait(self.settings["poll_seconds"])
                    continue
                frozen = int(time.time() * 1000)
                read(lambda frozen=frozen: self.poller.poll_once(frozen))
                self._stop.wait(self.settings["poll_seconds"])
        except AdmissionPaused:
            self._stop.wait()
        except _StopRequested:
            return
        except Exception as exc:
            self.status.error(exc)
            raise

    def stop(self):
        self.pause_admission()
        self._stop.set()
        self.bridge.stop()
        self.status.update(state="stopped")


def run_host(settings, *, proof_path, root=ROOT, on_started=None):
    """Canonical native-Task entry; no legacy receiver or Python watchdog.

    OS native IgnoreNew/restart owns process recovery. No watchdog is started.
    The legacy poller lease is held before stores, assistant, health or claims.
    """
    from src.core.execution import ExecutionRuntime, ExecutionStore
    from src.core.runtime_service import RuntimeService

    validate_production_filter(proof_path, settings, root=root)
    with FileLease(settings["workspace"] / "poller.lock"):
        database = settings["workspace"] / "test.sqlite3"
        if database.is_symlink():
            raise ValueError("测试数据库不能是链接。")
        validate_profile_binding(settings, database)
        execution = ExecutionRuntime(ExecutionStore(database))
        config = {
            "storage": {"data_dir": str(settings["workspace"])},
            "interactive": {
                "feishu": {
                    "enabled": True,
                    "app_id": settings["app_id"],
                    "allowed_chat_ids": settings["allowed_chat_ids"],
                }
            },
        }

        def factory(_config, runtime):
            return {"feishu": HistoryBot(settings, runtime, proof_path, root=root)}

        host = RuntimeService(
            config,
            scheduling=False,
            execution_runtime=execution,
            bot_factory=factory,
            register_reports=False,
        )
        host.run(on_started=on_started)
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--probe",
        action="store_true",
        help="只读检查群详情与history权限，不建数据库、不调用模型、不发送消息",
    )
    parser.add_argument("--production-filter-verified", type=Path)
    parser.add_argument(
        "--host-runtime",
        action="store_true",
        help="唯一常驻运行入口：公共宿主管理本测试群及原数据库",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="只读核对真实进程身份及心跳，不连接飞书或启动接收器",
    )
    parser.add_argument(
        "--request-stop",
        action="store_true",
        help="仅请求当前公共宿主会话排空退出，不启动接收器或读取模型凭据",
    )
    args = parser.parse_args(argv)
    try:
        settings = (
            load_settings(args.config, control_only=True)
            if args.request_stop or args.status
            else load_settings(args.config, probe=args.probe)
        )
        if args.request_stop:
            from src.core.runtime_service import request_service_stop

            request = request_service_stop(
                {"storage": {"data_dir": str(settings["workspace"])}}
            )
            print(json.dumps(request, ensure_ascii=False), flush=True)
            return 0
        if args.status:
            from src.core.runtime_service import read_service_status

            service = read_service_status(
                {"storage": {"data_dir": str(settings["workspace"])}}
            )
            transport = read_runtime_status(settings)
            ready = bool(
                service.get("ready")
                and transport.get("healthy")
                and service.get("pid") == transport.get("pid")
            )
            print(
                json.dumps(
                    {"service": service, "transport": transport, "ready": ready},
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return 0 if ready else 2
        if args.probe:
            return run_probe(settings)
        if args.host_runtime:
            return run_host(settings, proof_path=args.production_filter_verified)
        parser.error("常驻运行必须显式指定 --host-runtime；旧运行入口已退役。")
    except KeyboardInterrupt:
        return 0
    except (
        OSError,
        ValueError,
        RuntimeError,
        TypeError,
        KeyError,
        yaml.YAMLError,
    ) as exc:
        # API/config bodies, tokens, secrets and user message text are never echoed.
        print(
            json.dumps(
                {"poller_state": "failed", "last_error": safe_error(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
