"""Bounded native/Pi agent loops; the Python service owns every tool and secret."""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from .client import AssistantAPIError
from .store import canonical_json

FINAL_INSTRUCTION = (
    "本轮工具预算已用完。请仅根据已取得的证据回答用户，列明尚未查清的点。"
    "不要发起工具调用、虚构执行成功或再要求用户提供仓库已有的内容。"
)


def compact_messages(messages: list[dict], limit: int) -> list[dict]:
    """Retain tool-call/result pairing; shorten older tool bodies, never commands."""
    result = copy.deepcopy(messages)
    if len(canonical_json(result)) <= limit:
        return result
    for item in result:
        content = item.get("content")
        if (
            item.get("role") == "tool"
            and isinstance(content, str)
            and len(content) > 1200
        ):
            item["content"] = canonical_json(
                {
                    "compacted": True,
                    "sha256": hashlib.sha256(content.encode()).hexdigest(),
                    "preview": content[:1000],
                    "notice": "旧工具正文已压缩，精确细节可按路径重新读取。",
                }
            )
            if len(canonical_json(result)) <= limit:
                return result
    raise AssistantAPIError("本轮上下文超过预算，请清空对话后聚焦一个模块继续。")


class ConversationHarness:
    def __init__(self, client, settings, root: Path, stop_event: threading.Event):
        self.client, self.settings = client, settings
        self.root, self.stop_event = Path(root), stop_event
        self._process = None
        self._lock = threading.Lock()

    def stop(self) -> None:
        with self._lock:
            process = self._process
        if process is not None and process.poll() is None:
            self._terminate(process)

    @staticmethod
    def _terminate(process) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                process.terminate()
            else:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=1)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            if process.poll() is None:
                if os.name == "nt":
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)

    def run(self, messages, tools, invoke, audit, format_value, check_answer) -> str:
        run = _Run(self, tools, invoke, audit, format_value, check_answer)
        audit("harness_start", {"backend": self.settings.harness_backend})
        try:
            if self.settings.harness_backend == "pi":
                answer = self._pi(messages, tools, run)
            else:
                answer = self._native(messages, run)
            audit(
                "harness_finished",
                {
                    "backend": self.settings.harness_backend,
                    "model_rounds": run.rounds,
                    "tool_calls": run.calls,
                },
            )
            return answer
        finally:
            self.stop()

    def _native(self, messages, run) -> str:
        messages = copy.deepcopy(messages)
        while True:
            response = run.model(messages)
            messages.append(response)
            if not response.get("tool_calls"):
                return response.get("content") or "没有生成回复，请补充需求。"
            for call in response["tool_calls"]:
                if not isinstance(call, dict) or not isinstance(call.get("id"), str):
                    raise AssistantAPIError("工具调用格式不完整。")
                try:
                    function = call["function"]
                    result = run.tool(
                        function["name"], json.loads(function["arguments"])
                    )
                except (ValueError, KeyError, TypeError) as exc:
                    result = {"error": str(exc)}
                    run.audit("tool_error", {"error": str(exc)})
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": canonical_json(run.format_value(result)),
                    }
                )

    def _pi(self, messages, tools, run) -> str:
        executable = shutil.which("node")
        bridge = self.root / "tools" / "feishu-pi" / "bridge.mjs"
        if not executable or not bridge.is_file() or bridge.is_symlink():
            raise AssistantAPIError(
                "Pi harness 未安装完整，请检查部署依赖或切换 native。"
            )
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG"}
        }
        options = (
            {"creationflags": subprocess.CREATE_NO_WINDOW}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        # No shell and no API keys, home directory, Node options or plugin config.
        with self._lock:
            run.guard()
            process = subprocess.Popen(
                [executable, str(bridge)],
                cwd=bridge.parent,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                **options,
            )
            self._process = process
        messages_queue = queue.Queue(maxsize=8)
        reading_stopped = threading.Event()

        def read_output():
            try:
                while not reading_stopped.is_set():
                    line = process.stdout.readline(2 * 1024 * 1024 + 1)
                    if len(line) > 2 * 1024 * 1024:
                        value = {
                            "type": "error",
                            "message": "Pi protocol frame too large",
                        }
                    elif not line:
                        value = {
                            "type": "error",
                            "message": "Pi process ended without a result",
                        }
                    else:
                        value = json.loads(line.decode("utf-8"))
                    if not isinstance(value, dict):
                        raise TypeError("Pi protocol messages must be objects")
                    while not reading_stopped.is_set():
                        try:
                            messages_queue.put(value, timeout=0.1)
                            break
                        except queue.Full:
                            continue
                    if value.get("type") in {"done", "error"}:
                        return
            except (ValueError, TypeError, OSError) as exc:
                try:
                    messages_queue.put(
                        {"type": "error", "message": str(exc)}, timeout=0.1
                    )
                except queue.Full:
                    pass

        reader = threading.Thread(
            target=read_output, daemon=True, name="feishu-pi-output"
        )
        reader.start()
        watchdog = threading.Timer(
            max(0.01, run.deadline - time.monotonic()), self._terminate, args=(process,)
        )
        watchdog.daemon = True
        watchdog.start()

        def send(value):
            process.stdin.write((canonical_json(value) + "\n").encode("utf-8"))
            process.stdin.flush()

        try:
            send(
                {
                    "type": "start",
                    "messages": messages,
                    "tools": tools,
                    "max_tool_rounds": self.settings.max_tool_rounds,
                    "max_tool_calls": self.settings.max_tool_calls,
                    "max_context_chars": self.settings.max_context_chars,
                }
            )
            while True:
                run.guard()
                try:
                    event = messages_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                kind = event.get("type")
                if kind == "done":
                    answer = event.get("answer")
                    if not isinstance(answer, str) or not answer:
                        raise AssistantAPIError("Pi harness 未返回可用回复。")
                    return answer
                if kind == "error":
                    raise AssistantAPIError(
                        "Pi harness 失败：" + str(event.get("message", ""))[:1000]
                    )
                if kind != "request" or not isinstance(event.get("id"), str):
                    raise AssistantAPIError("Pi harness 返回了无效协议消息。")
                if event.get("kind") == "model":
                    result = run.model(event["messages"])
                elif event.get("kind") == "tool":
                    result = run.format_value(
                        run.tool(event.get("name"), event.get("arguments"))
                    )
                else:
                    raise AssistantAPIError("Pi harness 请求了未注册能力。")
                send({"reply_to": event["id"], "result": result})
        finally:
            watchdog.cancel()
            reading_stopped.set()
            self._terminate(process)
            for pipe in (process.stdin, process.stdout):
                if pipe is not None:
                    pipe.close()
            reader.join(timeout=1)
            with self._lock:
                self._process = None


class _Run:
    def __init__(self, harness, tools, invoke, audit, format_value, check_answer):
        self.harness, self.settings = harness, harness.settings
        self.tools, self.invoke, self.audit = tools, invoke, audit
        self.names = {item["function"]["name"] for item in tools}
        self.format_value, self.check_answer = format_value, check_answer
        self.rounds = self.calls = self.repairs = 0
        self.repeated = {}
        self.deadline = time.monotonic() + self.settings.conversation_timeout_seconds
        self.finalizing = False

    def guard(self):
        if self.harness.stop_event.is_set():
            raise AssistantAPIError("助手正在停止。")
        if time.monotonic() >= self.deadline:
            raise AssistantAPIError("本轮问答达到总时间预算，请聚焦一个模块继续。")

    def model(self, messages):
        self.guard()
        if self.rounds >= self.settings.max_tool_rounds:
            raise AssistantAPIError("本轮问答已达到模型调用上限。")
        self.finalizing = (
            self.rounds == self.settings.max_tool_rounds - 1
            or self.calls >= self.settings.max_tool_calls
        )
        messages = compact_messages(messages, self.settings.max_context_chars)
        if self.finalizing:
            messages.append({"role": "system", "content": FINAL_INSTRUCTION})
        self.rounds += 1
        self.audit(
            "model_request",
            {
                "round": self.rounds,
                "model": self.settings.model,
                "backend": self.settings.harness_backend,
                "input_chars": len(canonical_json(messages)),
                "finalizing": self.finalizing,
            },
        )
        start = time.monotonic()
        complete = self.harness.client.complete
        kwargs = (
            {"deadline": self.deadline}
            if "deadline" in inspect.signature(complete).parameters
            else {}
        )
        response = complete(messages, [] if self.finalizing else self.tools, **kwargs)
        self.audit(
            "model_response",
            {
                "elapsed_ms": round((time.monotonic() - start) * 1000),
                "response": response,
            },
        )
        self.guard()
        if self.finalizing and response.get("tool_calls"):
            raise AssistantAPIError("模型未在预算内完成总结；已有提案仍须单独确认。")
        if not response.get("tool_calls"):
            errors = self.check_answer(response.get("content") or "")
            if errors:
                self.audit("citation_rejected", {"errors": errors})
                if self.repairs == 0 and self.rounds < self.settings.max_tool_rounds:
                    self.repairs += 1
                    return self.model(
                        [
                            *messages,
                            response,
                            {
                                "role": "system",
                                "content": "引用校验未通过。按工具显示的L行号更正引用，必要时继续读源码；"
                                "这不授权配置变更或执行。未读到的路径和行号不得作为依据。错误："
                                + canonical_json(errors),
                            },
                        ]
                    )
                raise AssistantAPIError(
                    "回答的来源引用未通过核验，已记录排查轨迹；请聚焦问题重试。"
                )
        return response

    def tool(self, name, arguments):
        self.guard()
        try:
            if name not in self.names or not isinstance(arguments, dict):
                raise ValueError("未知工具或工具参数不是对象。")
            if self.finalizing or self.calls >= self.settings.max_tool_calls:
                raise ValueError("工具预算已用完，请根据已有证据总结。")
            self.calls += 1
            key = canonical_json([name, arguments])
            self.repeated[key] = self.repeated.get(key, 0) + 1
            if self.repeated[key] > 1:
                raise ValueError(
                    "相同查询已执行；请使用已有结果，或根据错误改换数据集、关键词或路径。"
                )
            return self.invoke(name, arguments)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            self.audit("tool_error", {"name": name, "error": str(exc)})
            return {"error": str(exc)}
