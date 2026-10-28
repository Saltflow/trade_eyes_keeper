"""Explicit discovery-only onboarding for a separate Feishu test App.

Prints authenticated private-chat IDs; no model/tool/business receiver remains.
Production identity is read only to exclude its App from this discovery tool.
"""

from __future__ import annotations

import argparse
import json
import logging
import multiprocessing
import os
import queue
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.personal_monitor_transport import message_identity, production_app_ids


def load_test_settings(path: Path, *, discover=False, root=ROOT, environ=None) -> dict:
    environ = os.environ if environ is None else environ
    if path.resolve() == (root / "config" / "config.yaml").resolve():
        raise ValueError("必须指定独立测试配置，不能使用生产配置。")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    allowed = {
        "test_mode",
        "app_id",
        "allowed_chat_ids",
        "allowed_sender_ids",
        "workspace_name",
        "market_check_times",
    }
    if (
        not isinstance(raw, dict)
        or raw.get("test_mode") is not True
        or set(raw) - allowed
    ):
        raise ValueError("测试配置字段不合法，必须显式 test_mode: true。")
    app_id = raw.get("app_id", "")
    known = production_app_ids(root, environ)
    if not known:
        raise ValueError("无法确认生产 App ID，拒绝启动；请先核对本地生产配置。")
    if app_id in known:
        raise ValueError("拒绝使用生产 App ID 启动测试接收器。")
    if (
        not isinstance(app_id, str)
        or not re.fullmatch(r"cli_[A-Za-z0-9]{6,64}", app_id)
        or "REPLACE" in app_id
    ):
        raise ValueError("请填写新建测试应用的 App ID。")
    secret = environ.get("PERSONAL_MONITOR_TEST_FEISHU_APP_SECRET", "").strip()
    key = environ.get("PERSONAL_MONITOR_TEST_DEEPSEEK_API_KEY", "").strip()
    if not secret or (not discover and not key):
        raise ValueError("缺少专用 PERSONAL_MONITOR_TEST_* 测试凭证环境变量。")
    for field, prefix in (("allowed_chat_ids", "oc_"), ("allowed_sender_ids", "ou_")):
        values = raw.get(field, [])
        if not isinstance(values, list) or any(
            not isinstance(value, str)
            or not re.fullmatch(prefix + r"[A-Za-z0-9_-]+", value)
            for value in values
        ):
            raise ValueError("测试会话和发送者必须为显式 ID 白名单，不能使用通配符。")
        if not discover and not values:
            raise ValueError("正常模式必须填写 chat_id 和 sender open_id 双白名单。")
    name = raw.get("workspace_name", "")
    if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,48}", name):
        raise ValueError("workspace_name 只能使用字母、数字、下划线和短横线。")
    base = root / "data" / "personal_monitor_feishu_test"
    workspace = base / name
    if base.is_symlink() or not workspace.resolve().is_relative_to(
        root.resolve() / "data" / "personal_monitor_feishu_test"
    ):
        raise ValueError("测试工作区不能通过链接指向其他位置。")
    times = raw.get("market_check_times", ["09:50", "14:30", "19:00"])
    if (
        not isinstance(times, list)
        or not 1 <= len(times) <= 8
        or any(
            not isinstance(value, str)
            or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value)
            for value in times
        )
    ):
        raise ValueError("market_check_times 必须为 1 至 8 个 HH:MM 时点。")
    return dict(
        raw,
        app_secret=secret,
        api_key=key,
        workspace=workspace,
        market_check_times=times,
    )


def discover_message(body, emit=print):
    identity = message_identity(body)
    if identity:
        owner, chat, _message_id, _message = identity
        emit(json.dumps({"chat_id": chat, "sender_open_id": owner}, ensure_ascii=False))
        return True
    return False


def run_receiver(settings, *, discover=False):
    if not discover:
        raise ValueError("旧模型桥已退役；此工具仅保留只读会话发现。")
    import tempfile

    with tempfile.TemporaryDirectory(prefix="trade_eyes_feishu_discovery_") as path:
        return _run_receiver(settings, inbox_path=Path(path) / "inbox.db")


def _run_receiver(settings, *, inbox_path):
    from src.interactive.feishu_bot import _run_sdk
    from src.interactive.ingress import DurableIngress

    context = multiprocessing.get_context("spawn")
    events = context.Queue(maxsize=64)
    inbox_settings = {
        "path": str(inbox_path),
        "app_id": settings["app_id"],
        "allowed_chat_ids": ["*"],
    }
    inbox = DurableIngress(**inbox_settings)
    process = context.Process(
        target=_run_sdk,
        args=(settings["app_id"], settings["app_secret"], events, inbox_settings),
        daemon=True,
    )
    started = False
    try:
        process.start()
        started = True
        print(
            "只读发现会话：请私聊新测试机器人，Ctrl+C 退出。",
            flush=True,
        )
        while True:
            inbox.dispatch(
                None,
                lambda _app, payload: (
                    "discovered" if discover_message(payload) else "ignored"
                ),
            )
            try:
                kind, _payload = events.get(timeout=0.5)
            except queue.Empty:
                if not process.is_alive():
                    raise RuntimeError("测试 SDK 接收器已退出。")
                continue
            if kind == "error":
                raise RuntimeError("测试 SDK 接收失败。")
            # 'wake' only prompts the next durable inbox scan.
    finally:
        if started:
            if process.is_alive():
                process.terminate()
            process.join(timeout=3)
            if process.is_alive():
                process.kill()
                process.join(timeout=2)
            process.close()
        events.close()
        events.cancel_join_thread()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--discover-chat",
        action="store_true",
        help="只读接收并打印私聊 chat_id/open_id；不模型调用、不发送消息",
    )
    parser.add_argument(
        "--check", action="store_true", help="仅本地配置与隔离边界检查，不联网"
    )
    args = parser.parse_args(argv)
    if not args.discover_chat and not args.check:
        parser.error("旧模型桥已退役；仅支持 --discover-chat 或 --check。")
    try:
        settings = load_test_settings(args.config, discover=args.discover_chat)
        if args.check:
            print("测试配置与隔离边界检查通过；尚未连接飞书或模型。")
            return 0
        logging.basicConfig(level=logging.WARNING)
        run_receiver(settings, discover=args.discover_chat)
    except KeyboardInterrupt:
        return 0
    except (ValueError, OSError, RuntimeError, yaml.YAMLError) as exc:
        # Configuration errors use our fixed messages; parser/file exceptions
        # may contain private paths or YAML content and are never echoed.
        print(
            str(exc)
            if type(exc) is ValueError
            else "测试启动失败：" + type(exc).__name__,
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
