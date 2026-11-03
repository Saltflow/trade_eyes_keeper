"""Evidence-led research loop, immutable models and reproducible handoffs.

Industry knowledge belongs to the agent and source material, not a formula registry.
The host controls evidence access, attempt identity, budget and code execution.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

from src.agent_runtime.harness import ConversationHarness
from src.data_hub import DataHub
from src.data_hub.public_web import fetch_public, search_results, search_url
from src.platform_composition import authority_for
from src.task_runtime.request_policy import RequestPolicyBlocked

from .common import atomic_write, file_sha, fingerprint, load, now
from .model_sandbox import ModelSandbox, output_files
from .observability import redact
from .stages import visible


class RunStop:
    def __init__(self, pipeline):
        self.pipeline = pipeline

    def is_set(self):
        return self.pipeline.stopped()

    def wait(self, seconds):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            if self.is_set():
                return True
            time.sleep(min(0.05, max(0, until - time.monotonic())))
        return self.is_set()


def verify_bundle(path):
    manifest = load(path)
    for name, digest in manifest["files"].items():
        file = path.parent / name
        if (
            not file.resolve().is_relative_to(path.parent.resolve())
            or file.is_symlink()
            or file_sha(file) != digest
        ):
            raise ValueError("investigation_integrity_changed")
    return manifest


class ResearchWorkspace:
    def __init__(self, pipeline, research_key, revision="initial", parent=None):
        self.pipeline, self.key = pipeline, research_key
        self.root = pipeline.spec.output / "investigation" / revision
        self.root.mkdir(parents=True, exist_ok=True)
        self.revision, self.parent = revision, parent
        self.authority = authority_for(
            pipeline.spec.repo,
            explicit_root=pipeline.spec.authority_root,
            isolated=pipeline.spec.authority_isolated,
        )
        self.hub = DataHub(self.authority)
        self.cancel = RunStop(pipeline)
        self.deadline = time.monotonic() + 600
        self.sources = (
            load(self.root / "sources.json")
            if (self.root / "sources.json").exists()
            else {}
        )
        self.models = (
            load(self.root / "models.json")
            if (self.root / "models.json").exists()
            else {}
        )
        self.selected = None
        self.tool_step = 0
        self.sandbox = ModelSandbox()

    def register(self, path, label, *, kind="provided", provenance=None):
        path = Path(path)
        if path.is_symlink() or not path.is_file():
            raise ValueError("research_material_not_regular")
        original = self.hub.register_original(path)
        source_id = fingerprint([original["sha256"], label, provenance or {}])[:24]
        record = {
            **self.sources.get(source_id, {}),
            "id": source_id,
            "label": label,
            "kind": kind,
            "sha256": original["sha256"],
            "path": original["path"],
            "suffix": path.suffix.lower(),
            "provenance": provenance or {},
        }
        self.sources[source_id] = record
        atomic_write(self.root / "sources.json", self.sources)
        return {k: v for k, v in record.items() if k != "path"}

    def _text(self, source_id):
        record = self.sources[source_id]
        path = Path(record["path"])
        if file_sha(path) != record["sha256"]:
            raise ValueError("research_source_integrity_changed")
        text_path = self.root / "text" / (source_id + ".txt")
        if record.get("text_sha256"):
            if file_sha(text_path) != record["text_sha256"]:
                raise ValueError("research_text_integrity_changed")
            return text_path.read_text("utf-8")
        content_type = record["provenance"].get("content_type", "")
        if record["suffix"] in {".md", ".txt", ".json", ".csv"} or content_type.split(
            ";", 1
        )[0] in {
            "text/plain",
            "text/csv",
            "text/markdown",
            "application/json",
        }:
            view = {"text": path.read_text("utf-8"), "source_complete": True}
        else:
            if not content_type:
                with path.open("rb") as stream:
                    magic = stream.read(20)
                content_type = (
                    "application/pdf" if magic.startswith(b"%PDF-") else "text/html"
                )
            view = self.hub.text_view(self.hub.ensure_parsed(path, content_type))
        atomic_write(text_path, view["text"])
        record.update(
            text_sha256=file_sha(text_path),
            text_complete=view.get("source_complete", False),
        )
        atomic_write(self.root / "sources.json", self.sources)
        return view["text"]

    def read_source(self, source_id, start=1, count=100):
        if (
            type(start) is not int
            or type(count) is not int
            or start < 1
            or not 1 <= count <= 200
        ):
            raise ValueError("bounded_line_range_required")
        lines = self._text(source_id).splitlines()
        selected, size = [], 0
        for index in range(start - 1, min(start - 1 + count, len(lines))):
            line = lines[index]
            if size + len(line) > 24000:
                if not selected:
                    return {
                        "source_id": source_id,
                        "status": "long_line",
                        "line": index + 1,
                        "characters": len(line),
                        "hint": "Use read_source_slice for this line.",
                    }
                break
            selected.append(f"L{index + 1}: {line}")
            size += len(line)
        return {
            "source_id": source_id,
            "sha256": self.sources[source_id]["sha256"],
            "text_sha256": self.sources[source_id]["text_sha256"],
            "text_complete": self.sources[source_id]["text_complete"],
            "total_lines": len(lines),
            "next_line": start + len(selected),
            "text": "\n".join(selected),
        }

    def read_source_slice(self, source_id, line, offset=0):
        if type(line) is not int or type(offset) is not int or line < 1 or offset < 0:
            raise ValueError("invalid_text_slice")
        raw = self._text(source_id).splitlines()[line - 1]
        return {
            "source_id": source_id,
            "line": line,
            "offset": offset,
            "text": raw[offset : offset + 12000],
            "total_characters": len(raw),
        }

    def find_sources(self, query="", offset=0):
        if not isinstance(query, str) or len(query) > 256:
            raise ValueError("bounded_query_required")
        if type(offset) is not int or not 0 <= offset <= 10000:
            raise ValueError("bounded_search_offset_required")
        results, matched = [], 0

        def add(item):
            nonlocal matched
            matched += 1
            if matched > offset:
                results.append(item)
            return len(results) > 40

        for sid, record in self.sources.items():
            if (not query or query.casefold() in record["label"].casefold()) and add(
                {"source_id": sid, "label": record["label"], "kind": record["kind"]}
            ):
                break
            if query:
                try:
                    for line, text in enumerate(self._text(sid).splitlines(), 1):
                        if query.casefold() in text.casefold() and add(
                            {"source_id": sid, "line": line, "text": text[:500]}
                        ):
                            break
                except (ValueError, OSError) as error:
                    add({"source_id": sid, "error": str(error)})
            if len(results) > 40:
                break
        return {
            "results": results[:40],
            "source_count": len(self.sources),
            "next_offset": offset + 40 if len(results) > 40 else None,
            "match_mode": "case_insensitive_literal; use one keyword per query",
        }

    def fetch_source(self, url, purpose, refresh=False):
        if not isinstance(purpose, str) or not purpose.strip() or len(purpose) > 2000:
            raise ValueError("evidence_purpose_required")
        metadata = {}

        def download(target):
            raw, headers = fetch_public(
                url, self.authority, deadline=self.deadline, cancel=self.cancel
            )
            metadata.update(headers)
            return raw

        if type(refresh) is not bool:
            raise ValueError("refresh_requires_boolean")
        original = self.hub.ensure_original(
            url, download, version=self.key if refresh else ""
        )
        # A cached original has a known retrieval version, not guaranteed freshness.
        provenance = {
            **metadata,
            "requested_url": url,
            "purpose": purpose,
            "observed_at": now(),
            "cache_hit": original.get("cache_hit", False),
            "publication_date": "not_verified",
            "source_status": "unverified_external_material",
        }
        return self.register(
            original["path"], url, kind="public", provenance=provenance
        )

    def search_public(self, query, provider="bing"):
        source = self.fetch_source(
            search_url(query, provider), "Discovery only: " + query, refresh=True
        )
        raw = Path(self.sources[source["id"]]["path"]).read_bytes()
        rows = search_results(raw)
        return {
            "search_source_id": source["id"],
            "provider": provider,
            "results": rows,
            "status": "discovery_only" if rows else "no_discovery_results",
        }

    def save_model(self, python, inputs, explanation, evidence, assumptions):
        if not isinstance(python, str) or not 1 <= len(python) <= 100000:
            raise ValueError("bounded_model_source_required")
        ast.parse(python)  # Syntax only; Docker is the execution boundary.
        if (
            not isinstance(inputs, dict)
            or not isinstance(assumptions, dict)
            or not isinstance(evidence, list)
        ):
            raise TypeError("model_inputs_and_basis_required")
        if not isinstance(explanation, str) or not explanation.strip():
            raise ValueError("model_explanation_required")
        if not evidence:
            raise ValueError("model_requires_source_references")
        for item in evidence:
            if not isinstance(item, dict) or item.get("source_id") not in self.sources:
                raise ValueError("unknown_model_evidence")
            lines = self._text(item["source_id"]).splitlines()
            if (
                type(item.get("start")) is not int
                or type(item.get("end")) is not int
                or not 1 <= item["start"] <= item["end"] <= len(lines)
            ):
                raise ValueError("invalid_model_evidence_locator")
        basis = {
            "explanation": explanation,
            "evidence": evidence,
            "assumptions": assumptions,
        }
        identity = fingerprint({"python": python, "inputs": inputs, **basis})
        if len(json.dumps(inputs, ensure_ascii=False, allow_nan=False)) > 2_000_000:
            raise ValueError("model_input_size_limit")
        folder = self.root / "models" / identity
        if identity not in self.models:
            folder.mkdir(parents=True, exist_ok=True)
            for name, value in {"model.py": python, "inputs.json": inputs}.items():
                atomic_write(folder / "input" / name, value, exclusive=True)
            atomic_write(folder / "basis.json", basis, exclusive=True)
            files = {
                p.relative_to(folder).as_posix(): file_sha(p)
                for p in folder.rglob("*")
                if p.is_file()
            }
            atomic_write(folder / "freeze.json", {"files": files}, exclusive=True)
            self.models[identity] = {
                "id": identity,
                "path": str(folder),
                "freeze_sha256": file_sha(folder / "freeze.json"),
            }
            atomic_write(self.root / "models.json", self.models)
        return {"model_id": identity, "status": "saved_not_executed"}

    def _model(self, model_id):
        folder = Path(self.models[model_id]["path"])
        if file_sha(folder / "freeze.json") != self.models[model_id]["freeze_sha256"]:
            raise ValueError("model_freeze_changed")
        verify_bundle(folder / "freeze.json")
        return folder

    def run_model(self, model_id):
        folder = self._model(model_id)
        receipt_path = folder / "execution.json"
        if receipt_path.exists():
            return load(receipt_path)
        if (folder / "started.json").exists():
            return {
                "status": "unknown",
                "reason": "prior_model_execution_interrupted_no_rerun",
            }
        atomic_write(folder / "started.json", {"at": now()}, exclusive=True)
        result = self.sandbox.run(
            folder / "input",
            folder / "output",
            cancel=self.cancel,
            deadline=self.deadline,
        )
        atomic_write(receipt_path, result, exclusive=True)
        self.pipeline.event("investigation_model_executed", model_id=model_id, **result)
        if result.get("reason") == "container_cleanup_unconfirmed":
            raise RuntimeError("container_cleanup_unconfirmed_no_further_calculation")
        return result

    def read_result(self, model_id, name="results.json", offset=0):
        folder = self._model(model_id)
        receipt = load(folder / "execution.json")
        if receipt.get("status") != "computed" or name not in receipt.get("files", {}):
            raise ValueError("computed_result_not_available")
        paths = output_files(folder / "output/files")
        path = paths[name]
        if file_sha(path) != receipt["files"][name]:
            raise ValueError("model_result_integrity_changed")
        if (
            path.suffix not in {".json", ".csv", ".txt", ".md"}
            or type(offset) is not int
            or offset < 0
        ):
            raise ValueError("bounded_text_result_required")
        text = path.read_text("utf-8")
        return {
            "model_id": model_id,
            "name": name,
            "offset": offset,
            "text": text[offset : offset + 16000],
            "total_characters": len(text),
        }

    def select_model(self, model_id):
        folder = self._model(model_id)
        receipt = load(folder / "execution.json")
        if receipt.get("status") != "computed":
            raise ValueError("uncomputed_model_cannot_be_selected")
        for name, digest in receipt["files"].items():
            paths = output_files(folder / "output/files")
            if name not in paths or file_sha(paths[name]) != digest:
                raise ValueError("model_result_integrity_changed")
        self.selected = model_id
        atomic_write(self.root / "selected.json", {"model_id": model_id})
        return {
            "model_id": model_id,
            "checks_status": receipt["checks_status"],
            "financial_validation": "requires_research_review",
        }

    def invoke(self, name, arguments):
        if self.cancel.is_set() or time.monotonic() >= self.deadline:
            raise RuntimeError("investigation_stopped")
        allowed = {t["function"]["name"] for t in research_tools()}
        if name not in allowed:
            raise ValueError("unknown_investigation_tool")
        self.tool_step += 1
        call_key = fingerprint([name, arguments])
        sequence = self.root / "tool_sequence" / f"{self.tool_step:04d}.json"
        if sequence.exists():
            prior = load(sequence)
            if prior["call"] != call_key:
                raise ValueError("investigation_tool_replay_changed")
            if name == "select_model" and not prior["result"].get("error"):
                self.selected = arguments["model_id"]
            return prior["result"]
        # Durable replay of identical tool calls; mutations never run twice on resume.
        key = fingerprint(
            [name, arguments, sorted(self.sources) if name == "find_sources" else None]
        )
        path = self.root / "tools" / (key + ".json")
        if path.exists():
            value = load(path)
            if name == "select_model" and not value.get("error"):
                self.selected = arguments["model_id"]
        else:
            try:
                value = getattr(self, name)(**arguments)
            except (
                OSError,
                ValueError,
                KeyError,
                TypeError,
                SyntaxError,
                RequestPolicyBlocked,
            ) as error:
                value = {"error": str(error), "tool": name}
            atomic_write(path, value, exclusive=True)
        atomic_write(sequence, {"call": call_key, "result": value}, exclusive=True)
        return value

    def freeze(self, summary, error=""):
        lines = [
            "# 调查与经营模型交接",
            summary,
            "## 宿主状态",
            f"执行限制：{error or '无执行错误；经济解释仍需审阅'}",
        ]
        model_status = "not_built"
        if self.selected:
            self.select_model(self.selected)
            folder = self._model(self.selected)
            receipt = load(folder / "execution.json")
            model_status = receipt["checks_status"]
            lines += [
                "## 选定模型与可复现结果",
                json.dumps(load(folder / "basis.json"), ensure_ascii=False),
                "```python\n"
                + (folder / "input/model.py").read_text("utf-8")
                + "\n```",
                "### 完整输入\n" + (folder / "input/inputs.json").read_text("utf-8"),
                "### 实际计算结果\n"
                + (folder / "output/files/results.json").read_text("utf-8"),
                "### 执行回执\n" + json.dumps(receipt, ensure_ascii=False),
            ]
        else:
            lines.append(
                "未取得选定且实际运行的模型。不能宣称完成量化推导；仅保留已有证据与条件结论。"
            )
        lines += [
            "## 来源目录",
            json.dumps(
                [
                    {k: v for k, v in r.items() if k != "path"}
                    for r in self.sources.values()
                ],
                ensure_ascii=False,
            ),
        ]
        # Keep source bytes inside the immutable handoff for delivery and replay.
        for record in self.sources.values():
            target = self.root / "evidence" / (record["id"] + ".source")
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(record["path"], target)
            if file_sha(target) != record["sha256"]:
                raise ValueError("investigation_source_changed")
        handoff = self.root / "handoff.md"
        body = "\n\n".join(lines)
        if handoff.exists():
            if handoff.read_text("utf-8") != body:
                raise ValueError("investigation_unfrozen_handoff_conflict")
        else:
            atomic_write(handoff, body, exclusive=True)
        # A failed sandbox may have produced hostile links; never hash through them.
        files = {}
        rejected_outputs = []
        for model in self.models.values():
            folder = Path(model["path"])
            output = folder / "output"
            if (folder / "execution.json").exists() and load(
                folder / "execution.json"
            ).get("reason") == "container_cleanup_unconfirmed":
                rejected_outputs.append(output)
                continue
            try:
                output_files(output)
            except (ValueError, OSError):
                rejected_outputs.append(output)
        for p in self.root.rglob("*"):
            # A timed-out model request can still finish its transport audit.
            # Those mutable logs live in the run ledger, outside the research
            # snapshot. Model inputs, sandbox receipts and results remain frozen.
            if p.is_relative_to(self.root / "execution"):
                continue
            if any(p.is_relative_to(root) for root in rejected_outputs):
                continue
            if p.is_symlink() or any(
                parent.is_symlink()
                for parent in p.parents
                if parent.is_relative_to(self.root)
            ):
                continue
            if p.is_file() and p.name != "bundle.json":
                files[p.relative_to(self.root).as_posix()] = file_sha(p)
        manifest = {
            "files": files,
            "mutable_execution_path": "execution",
            "selected_model": self.selected,
            "model_status": model_status,
            "error": error,
            "parent": self.parent,
            "financial_validation": "not_independently_verified",
            "excluded_unsafe_outputs": [
                str(p.relative_to(self.root)) for p in rejected_outputs
            ],
        }
        atomic_write(self.root / "bundle.json", manifest, exclusive=True)
        return manifest


def research_tools():
    string = {"type": "string"}
    integer = {"type": "integer"}
    obj = {"type": "object"}
    definitions = [
        (
            "find_sources",
            "空query列资料；非空query用一个字面关键词查全文所有命中，每页40条，返回行号，可用next_offset翻页。多个独立关键词可在同一轮分别调用，不能把整句当搜索引擎关键词。",
            {
                "query": {"type": "string", "maxLength": 256},
                "offset": {"type": "integer", "minimum": 0, "maximum": 10000},
            },
            [],
        ),
        (
            "read_source",
            "按定位行读取来源；start从1开始，count为1至200。保留行号与原件SHA，宜批量读取已定位的相关区段。",
            {
                "source_id": string,
                "start": {"type": "integer", "minimum": 1},
                "count": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            ["source_id"],
        ),
        (
            "read_source_slice",
            "分页读取超长行，不丢失剩余原文。",
            {"source_id": string, "line": integer, "offset": integer},
            ["source_id", "line"],
        ),
        (
            "search_public",
            "搜索公开资料线索；默认Bing，可显式选择DuckDuckGo。失败或空结果不代表未披露；摘要不能作为已核实事实。没有自动切换或重试。",
            {
                "query": string,
                "provider": {"type": "string", "enum": ["bing", "duckduckgo"]},
            },
            ["query"],
        ),
        (
            "fetch_source",
            "下载公开HTTPS原件并记录版本；写明补证目的。",
            {"url": string, "purpose": string, "refresh": {"type": "boolean"}},
            ["url", "purpose"],
        ),
        (
            "save_model",
            "保存公司专属Python模型。读取/input/inputs.json，在当前/output目录写results.json（任意业务字段；checks为含passed布尔值的检查数组）。不联网/安装依赖。evidence每项为source_id/start/end；解释输入、假设和上下游。",
            {
                "python": string,
                "inputs": obj,
                "explanation": string,
                "evidence": {"type": "array", "items": obj},
                "assumptions": obj,
            },
            ["python", "inputs", "explanation", "evidence", "assumptions"],
        ),
        (
            "run_model",
            "无网络Docker执行；computed只代表程序完成。失败须新版本，不自动重试。",
            {"model_id": string},
            ["model_id"],
        ),
        (
            "read_result",
            "读取实际计算输出，禁止把模型预想输出当结果。",
            {"model_id": string, "name": string, "offset": integer},
            ["model_id"],
        ),
        (
            "select_model",
            "明确选择供报告使用的已运行版本；检查失败仍须如实披露。",
            {"model_id": string},
            ["model_id"],
        ),
    ]
    return [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": d,
                "parameters": {
                    "type": "object",
                    "properties": p,
                    "required": r,
                    "additionalProperties": False,
                },
            },
        }
        for n, d, p, r in definitions
    ]


class BudgetedResearchClient:
    def __init__(self, workspace, hold, max_turns=12):
        self.workspace, self.hold, self.turn = workspace, hold, 0
        self.max_turns = max_turns

    def complete(self, messages, tools, *, deadline=None):
        self.turn += 1
        ws = self.workspace
        protocol = "投研原生工具调用：使用API提供的工具完成调查、建模与验证。不要把工具调用写成正文JSON、XML或协议文本。工具回执只是证据；结束时给研究说明和缺口。"
        protocol += (
            f" 当前为第{self.turn}轮，含本轮至多剩余"
            f"{max(0, self.max_turns - self.turn) if tools else 0}轮工具决策，之后仅能总结。"
            "为保存模型、执行、检查实际结果和选择版本留足轮次；独立取证可批量调用。"
            "额度不足时保留真实阶段成果，不能伪造完成。"
        )
        protocol += (
            f"当前已保存{len(ws.models)}个模型，选定版本：{ws.selected or '无'}。"
        )
        if tools and self.turn >= self.max_turns // 2 and not ws.models:
            protocol += (
                "调查已使用至少半数轮次且尚无可执行模型。"
                "应将已有证据支持的经营机制先保存为可计算模型，运行后用差额指导补证，"
                "不必等所有问题解决再计算；不能据证计算的环节保留缺口，不补造数据。"
            )
        wire = []
        for item in messages:
            normalized = dict(item)
            if normalized.get("role") == "assistant" and normalized.get("tool_calls"):
                normalized.setdefault("reasoning_content", "")
                normalized["content"] = normalized.get("content") or ""
            wire.append(normalized)
        request = {
            "model": "deepseek-flash",
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
            "max_tokens": 65536,
            "stream": True,
            "stream_options": {"include_usage": True},
            "messages": [{"role": "system", "content": protocol}, *wire],
        }
        if tools:
            request["tools"] = tools
        ws.pipeline.adapters.check_context(request)
        key = fingerprint(
            {"investigation": ws.key, "revision": ws.revision, "turn": self.turn}
        )
        receipt = ws.pipeline.executor.call(
            key,
            request,
            ws.root / "execution",
            "investigation_model",
            hold_usd=self.hold,
            deadline=deadline,
        )
        if receipt.get("status") != "complete":
            raise RuntimeError(
                "investigation_model_unavailable:"
                + str(receipt.get("reason", receipt.get("status")))
            )
        if receipt.get("assistant_message_sha256"):
            path = Path(receipt["capture_dir"]) / "assistant_message.json"
            if file_sha(path) != receipt["assistant_message_sha256"]:
                raise ValueError("investigation_message_integrity_changed")
            value = load(path)
        else:
            # Compatibility for previously captured text-envelope attempts only;
            # new requests always use native API tools and retain their message.
            raw = visible(receipt).lstrip()
            value, end = json.JSONDecoder().raw_decode(raw)
            suffix = raw[end:].strip()
            if suffix:
                if suffix.startswith(("{", "[")):
                    raise ValueError("multiple_investigation_envelopes")
                ws.pipeline.event(
                    "investigation_response_suffix_ignored",
                    turn=self.turn,
                    characters=len(suffix),
                    suffix_sha256=fingerprint(suffix),
                )
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("tool_calls", []), list)
            or not isinstance(value.get("content", ""), str)
        ):
            raise TypeError("invalid_investigation_envelope")
        if not value.get("content", "").strip() and not value.get("tool_calls"):
            raise ValueError("empty_investigation_response")
        return {
            "role": "assistant",
            "content": value.get("content", ""),
            "tool_calls": value.get("tool_calls", []),
            **(
                {"reasoning_content": value["reasoning_content"]}
                if isinstance(value.get("reasoning_content"), str)
                else {}
            ),
        }


def investigate(pipeline, key, base, *, revision="initial", feedback="", parent=None):
    from .engine import FutureHold

    ws = ResearchWorkspace(pipeline, key, revision, parent)
    manifest_path = ws.root / "bundle.json"
    if manifest_path.exists():
        manifest = verify_bundle(manifest_path)
    else:
        base_path = ws.root / "provided.md"
        if not base_path.exists():
            atomic_write(base_path, base, exclusive=True)
        ws.register(base_path, "本轮完整底稿、研究范围与冻结行情")
        for issuer, subject in pipeline.subjects.items():
            for document in subject["documents"]:
                value = document.to_dict() if hasattr(document, "to_dict") else document
                path = Path(value.get("local_path") or "__missing__")
                if path.is_file():
                    if value.get("sha256") and file_sha(path) != value["sha256"]:
                        raise ValueError("investigation_original_integrity_changed")
                    ws.register(
                        path,
                        f"{issuer} {value.get('fiscal_year')} {value.get('document_id')}",
                        kind="annual",
                        provenance={
                            "url": value.get("url"),
                            "published_at": value.get("published_at"),
                            "fiscal_year": value.get("fiscal_year"),
                        },
                    )
        for issuer, state in pipeline.company_artifacts.items():
            for kind in ("context", "summary"):
                item = state.get(kind)
                if item and Path(item["path"]).is_file():
                    if file_sha(Path(item["path"])) != item["sha256"]:
                        raise ValueError("investigation_upstream_integrity_changed")
                    ws.register(item["path"], f"{issuer} {kind}")
        if parent:
            verify_bundle(Path(parent))
            ws.register(Path(parent).parent / "handoff.md", "上一冻结研究版本")
        cfg = pipeline.adapters.research_config.get("investigation", {})
        settings = SimpleNamespace(
            model="deepseek-flash",
            reasoning_effort="high",
            harness_backend="native",
            max_tool_rounds=min(16, max(1, int(cfg.get("max_model_turns", 12)))),
            max_tool_calls=min(64, max(1, int(cfg.get("max_tool_calls", 48)))),
            # Native tool continuity includes provider context and generated code.
            # Admission still checks the full request against the research token
            # and byte limits before any paid request is sent.
            max_context_chars=min(
                1000000, max(20000, int(cfg.get("max_context_chars", 1000000)))
            ),
            conversation_timeout_seconds=min(
                600, max(30, int(cfg.get("timeout_seconds", 600)))
            ),
        )
        ws.deadline = time.monotonic() + settings.conversation_timeout_seconds
        pipeline.phase("investigation_" + revision)
        client = BudgetedResearchClient(
            ws, FutureHold(3 if revision == "initial" else 1), settings.max_tool_rounds
        )
        harness = ConversationHarness(
            client,
            settings,
            stop_event=ws.cancel,
            repeatable_tools={"find_sources", "read_source", "read_result"},
        )
        prompt = (
            pipeline.spec.repo / "config/prompts/research_causal_chain_v1.md"
        ).read_text("utf-8")
        prompt += "\n\n" + (
            pipeline.spec.repo
            / cfg.get("prompt", "config/prompts/research_investigation_v1.md")
        ).read_text("utf-8")
        summary, error = "", ""
        limited = []

        def audit(event, fields):
            if event == "model_request" and fields.get("finalizing"):
                limited.append("investigation_execution_limit")
            pipeline.event(
                "investigation_" + event, revision=revision, details=redact(fields)
            )

        start_path = ws.root / "agent_start.json"
        if not start_path.exists():
            atomic_write(
                start_path,
                [
                    {"role": "system", "content": prompt},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "companies": pipeline.spec.companies,
                                "focus": pipeline.spec.focus,
                                "sources": ws.find_sources(),
                                "revision": revision,
                                "review_feedback": feedback,
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                exclusive=True,
            )
        try:
            summary = harness.run(
                load(start_path),
                research_tools(),
                ws.invoke,
                audit,
                lambda value: value,
                lambda answer: [],
            )
        except (RuntimeError, ValueError, OSError, TypeError, KeyError) as exc:
            error = str(exc)
            summary = "调查未完整结束，保留已有来源、模型与工具回执。"
        if limited and not error:
            error = limited[0]
        manifest = ws.freeze(summary, error)
    absolute_manifest = ws.root / "input_integrity.json"
    if not absolute_manifest.exists():
        atomic_write(
            absolute_manifest,
            {
                "files": {
                    str(ws.root / name): digest
                    for name, digest in manifest["files"].items()
                }
            },
            exclusive=True,
        )
    run = pipeline.store.get_run(pipeline.run_id)
    binding = {"path": str(absolute_manifest), "sha256": file_sha(absolute_manifest)}
    if binding not in run.get("integrity_manifests", []):
        pipeline.store.update_run(
            pipeline.run_id,
            integrity_manifests=[*run.get("integrity_manifests", []), binding],
        )
    if "budget_stop" in manifest["error"]:
        pipeline.blocked.append("budget_stop")
    artifact = pipeline.store.put_artifact(
        fingerprint({"investigation": key, "revision": revision}),
        "investigation",
        ws.root / "handoff.md",
        status="complete"
        if not manifest["error"] and manifest["model_status"] == "passed"
        else "partial",
        metadata={
            "bundle": str(manifest_path),
            "bundle_sha256": file_sha(manifest_path),
            "selected_model": manifest["selected_model"],
            "model_status": manifest["model_status"],
            "error": manifest["error"],
            "revision": revision,
            "financial_validation": "not_independently_verified",
        },
    )
    return artifact


def review_requests_rework(text):
    matches = re.findall(r"```research-rework\s*\n(.*?)```", text, flags=re.DOTALL)
    if not matches:
        return {"error": "missing_review_rework_decision"}
    if len(matches) != 1:
        return {"error": "ambiguous_review_rework_marker"}
    try:
        value = json.loads(matches[0])
        if (
            type(value.get("required")) is not bool
            or not isinstance(value.get("issues"), list)
            or any(not isinstance(x, str) or not x.strip() for x in value["issues"])
        ):
            raise ValueError("invalid_review_rework_marker")
        if value["required"] and not value["issues"]:
            raise ValueError("rework_requires_concrete_issues")
        return value
    except (ValueError, TypeError, AttributeError):
        return {"error": "invalid_review_rework_marker"}


def model_charts(artifact):
    """All model charts originate in the selected program's frozen output."""
    import yaml

    metadata = artifact["metadata"]
    manifest = Path(metadata["bundle"])
    verify_bundle(manifest)
    if not metadata.get("selected_model"):
        return ""
    result = load(
        manifest.parent
        / "models"
        / metadata["selected_model"]
        / "output/files/results.json"
    )
    charts = result.get("charts", [])
    if not isinstance(charts, list):
        return ""
    return "\n".join(
        "```chart\n" + yaml.safe_dump(chart, allow_unicode=True) + "```"
        for chart in charts
        if isinstance(chart, dict)
    )


def reproducibility_files(manifest_path):
    """A verified, portable public subset; omit request/tool diagnostics."""
    manifest = verify_bundle(manifest_path)
    selected = {}
    for name, digest in manifest["files"].items():
        if name in {
            "handoff.md",
            "provided.md",
            "sources.json",
            "selected.json",
            "models.json",
        } or re.fullmatch(
            r"(?:evidence/[a-f0-9]{24}\.source|text/[a-f0-9]{24}\.txt|models/[a-f0-9]{64}/(?:basis\.json|freeze\.json|execution\.json|input/(?:model\.py|inputs\.json)|output/files/.+))",
            name,
        ):
            selected[name] = digest
    public_manifest = {
        "files": selected,
        "original_bundle_sha256": file_sha(manifest_path),
        "selected_model": manifest["selected_model"],
        "model_status": manifest["model_status"],
        "error": manifest["error"],
        "financial_validation": manifest["financial_validation"],
        "scope": "reproducible_research_excluding_request_and_tool_diagnostics",
    }
    return [manifest_path.parent / name for name in selected], public_manifest
