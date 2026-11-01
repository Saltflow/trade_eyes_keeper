"""Origin-bound Feishu/Pi tools; model tools never apply repair candidates.

The assistant's existing ProposalStore performs human confirmation. This module
only prepares a bound payload; ``apply_confirmation`` is called by that trusted
confirmation dispatcher, never registered as an LLM tool.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from .common import file_sha, load
from .models import ResearchSpec
from .observability import bounded_log_tail, progress_snapshot, run_spec
from .repair import RepairManager


def research_tool_schema() -> list[dict]:
    def tool(name, description, properties, required):
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            },
        }

    identity = {"run_id": {"type": "string", "minLength": 1, "maxLength": 128}}
    return [
        tool(
            "research_pipeline_prepare",
            "准备A/H/US完整年报研究并显示预算预览；只有用户确认后才执行",
            {
                "companies": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 100,
                    "items": {"type": "string"},
                },
                "budget_limit_usd": {"type": "string"},
                "years": {"type": "integer", "minimum": 1, "maximum": 100},
                "start_year": {"type": "integer"},
                "end_year": {"type": "integer"},
                "focus": {"type": "string", "maxLength": 8000},
            },
            ["companies", "budget_limit_usd"],
        ),
        tool(
            "research_pipeline_tasks",
            "列出当前原会话本人发起的研究，含中断任务；不扫描其他用户资料",
            {"limit": {"type": "integer", "minimum": 1, "maximum": 20}},
            [],
        ),
        tool(
            "research_pipeline_status",
            "读取本人原会话研究状态/费用/缺口，无模型调用",
            identity,
            ["run_id"],
        ),
        tool(
            "research_pipeline_logs",
            "读取本人研究的脱敏事件，不读取任意服务器路径",
            {
                **identity,
                "after": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            ["run_id"],
        ),
        tool(
            "research_pipeline_artifacts",
            "列出本人研究完整产物及可交付文件，无补提取",
            identity,
            ["run_id"],
        ),
        tool(
            "research_pipeline_cancel",
            "停止本人研究新派发；保留并收齐在途，不清除资料",
            identity,
            ["run_id"],
        ),
        tool(
            "research_pipeline_diagnose",
            "使用该研究已授权维修额度诊断现存故障并生成/测试候选；不会应用或重发",
            identity,
            ["run_id"],
        ),
        tool(
            "research_pipeline_repair_preview",
            "读取已测试候选补丁及真实测试回执，生成单独人工确认提案",
            {
                **identity,
                "candidate_id": {"type": "string", "pattern": "^[a-f0-9]{32}$"},
            },
            ["run_id", "candidate_id"],
        ),
    ]


class ResearchTools:
    def __init__(
        self, repo: Path, store=None, *, executor_factory=None, sandbox_image=None
    ):
        self.repo = Path(repo).resolve()
        if store is None:
            from .store import Store

            store = Store(self.repo / "data/fundamental_data/shared_research")
        self.store, self.executor_factory = store, executor_factory
        self.sandbox_image = sandbox_image

    def _owned(self, run_id: str, chat: str, sender: str) -> dict:
        if not isinstance(run_id, str) or not 1 <= len(run_id) <= 128:
            raise ValueError("invalid_research_run_id")
        run = self.store.get_run(run_id)
        origin = run_spec(run).get("origin", {})
        if (
            origin.get("channel") != "feishu"
            or not chat
            or not sender
            or origin.get("chat") != chat
            or origin.get("sender") != sender
        ):
            raise PermissionError("research_origin_not_authorized")
        return run

    def _manager(self, run_id: str, executor=None) -> RepairManager:
        return RepairManager(
            self.store, run_id, self.repo, executor, sandbox_image=self.sandbox_image
        )

    def prepare_start(
        self, arguments: dict, chat: str, sender: str, action_id: str
    ) -> dict:
        allowed = {
            "companies",
            "budget_limit_usd",
            "years",
            "start_year",
            "end_year",
            "focus",
        }
        if set(arguments) - allowed or not {"companies", "budget_limit_usd"} <= set(
            arguments
        ):
            raise ValueError("invalid_research_preparation_arguments")
        if (
            not isinstance(arguments["companies"], list)
            or not 1 <= len(arguments["companies"]) <= 100
        ):
            raise ValueError("bounded_explicit_companies_required")
        output = self.repo / "data/fundamental_data/research_runs" / action_id
        spec = ResearchSpec(
            companies=tuple(arguments["companies"]),
            output=output,
            budget_limit_usd=Decimal(str(arguments["budget_limit_usd"])),
            years=arguments.get("years", 7),
            start_year=arguments.get("start_year"),
            end_year=arguments.get("end_year"),
            focus=arguments.get("focus", ""),
            repo=self.repo,
            origin={"channel": "feishu", "chat": chat, "sender": sender},
        )
        from .engine import plan_research

        preview_spec = ResearchSpec.from_dict(spec.to_dict())
        preview_spec.output = output.with_name(output.name + "_preview")
        plan = plan_research(preview_spec)
        return {
            "operation": "research_pipeline_start",
            "spec": spec.to_dict(),
            "preview": {
                "companies": list(spec.companies),
                "budget_limit_usd": str(spec.budget_limit_usd),
                "repair_budget_inside_total": str(
                    spec.budget_limit_usd * spec.repair_fraction
                ),
                "plan": plan,
                "new_paid_authorization_on_confirmation": True,
            },
            "origin": spec.origin,
        }

    def prepare_repair_confirmation(
        self, run_id: str, candidate_id: str, chat: str, sender: str
    ) -> dict:
        run = self._owned(run_id, chat, sender)
        manager = self._manager(run_id)
        candidate = manager._candidate(candidate_id)
        binding = manager.confirmation_payload(candidate_id, sender)
        receipt = load(Path(candidate["test_receipt"]))
        return {
            "operation": "research_pipeline_repair",
            "run_id": run_id,
            "candidate_id": candidate_id,
            "confirmation": binding,
            "origin": run_spec(run)["origin"],
            "job_dir": run_spec(run)["output"],
            "preview_files": [
                str(Path(candidate["workspace"]).parent / "candidate.json"),
                candidate["test_receipt"],
            ],
            "preview": {
                "run_id": run_id,
                "candidate_id": candidate_id,
                "candidate_hash": candidate["candidate_hash"],
                "base_hash": candidate["base_hash"],
                "diff": candidate["diff"],
                "test_receipt": receipt,
                "budget": self.store.budget(run_id),
                "effect": "确认后发布不可变执行版本；只接续从未发送任务，旧结果不改、失败/unknown不重发。",
            },
        }

    def call(self, name: str, arguments: dict, chat: str, sender: str):
        if not isinstance(arguments, dict):
            raise TypeError("research_tool_arguments_must_be_object")
        if name not in {x["function"]["name"] for x in research_tool_schema()}:
            raise ValueError("research_tool_not_registered")
        if name == "research_pipeline_tasks":
            if set(arguments) - {"limit"}:
                raise ValueError("unknown_research_task_query_parameter")
            limit = arguments.get("limit", 10)
            if type(limit) is not int or not 1 <= limit <= 20:
                raise ValueError("invalid_research_task_limit")
            with self.store.connection() as db:
                rows = db.execute(
                    "SELECT data FROM research_runs WHERE json_extract(data,'$.spec.origin.channel')='feishu' "
                    "AND json_extract(data,'$.spec.origin.chat')=? AND json_extract(data,'$.spec.origin.sender')=? "
                    "ORDER BY rowid DESC LIMIT ?",
                    (chat, sender, limit),
                ).fetchall()
            import json

            return {
                "runs": [
                    {
                        key: value
                        for key, value in json.loads(row[0]).items()
                        if key
                        in {
                            "id",
                            "run_id",
                            "state",
                            "phase",
                            "error",
                            "updated_at",
                            "repair_state",
                        }
                    }
                    for row in rows
                ],
                "origin_scoped": True,
            }
        allowed = (
            {"run_id", "after", "limit"}
            if name == "research_pipeline_logs"
            else {"run_id"}
        )
        if name in {"research_pipeline_prepare", "research_pipeline_repair_preview"}:
            raise ValueError("research_write_requires_host_proposal_dispatch")
        if set(arguments) - allowed or "run_id" not in arguments:
            raise ValueError("unknown_or_missing_research_tool_arguments")
        run_id = arguments["run_id"]
        run = self._owned(run_id, chat, sender)
        if name == "research_pipeline_status":
            return progress_snapshot(self.store, run_id)
        if name == "research_pipeline_logs":
            return bounded_log_tail(
                self.store,
                run_id,
                after=arguments.get("after", 0),
                limit=arguments.get("limit", 100),
            )
        if name == "research_pipeline_artifacts":
            output = Path(run_spec(run)["output"]).resolve()
            records = []
            for path in sorted(output.rglob("*")):
                if (
                    not path.is_file()
                    or path.is_symlink()
                    or not path.resolve().is_relative_to(output)
                    or "repairs" in path.relative_to(output).parts
                    or path.suffix.lower()
                    not in {".md", ".html", ".png", ".json", ".csv"}
                ):
                    continue
                if any(
                    token in path.name.lower()
                    for token in ("request", "secret", "credential")
                ):
                    continue
                records.append(
                    {
                        "relative_path": path.relative_to(output).as_posix(),
                        "bytes": path.stat().st_size,
                        "sha256": file_sha(path),
                    }
                )
                if len(records) == 100:
                    break
            return {
                "run_id": run_id,
                "files": records,
                "delivery_mode": "bot_attachment_or_origin_status",
                "notice": "Server paths are not remotely accessible URLs; use the origin host file delivery.",
            }
        if name == "research_pipeline_cancel":
            from .engine import cancel_research

            return cancel_research(run_id, store_root=self.store.root)
        if name == "research_pipeline_diagnose":
            error = run.get("error") or run.get("waiting_reason")
            if not error:
                return {"state": "no_current_fault", "run_id": run_id}
            manager = self._manager(run_id)
            incident = manager.diagnose(
                str(error),
                {
                    "phase": run.get("phase"),
                    "events": self.store.events(run_id, limit=20),
                },
            )
            if incident["state"] == "diagnostic_only":
                return incident
            from .engine import LazyModelExecutor

            executor = (self.executor_factory or LazyModelExecutor)(
                self.store, ResearchSpec.from_dict(run_spec(run)), run_id
            )
            try:
                manager.executor = executor
                return manager.run_agent(incident["id"])
            finally:
                executor.close()
        raise ValueError("unhandled_research_tool")

    def apply_confirmation(self, payload: dict, chat: str, sender: str) -> dict:
        """Only the existing human-confirmation dispatcher calls this method."""
        if payload.get("origin") != {
            "channel": "feishu",
            "chat": chat,
            "sender": sender,
        }:
            raise PermissionError("research_confirmation_origin_mismatch")
        if payload.get("operation") == "research_pipeline_start":
            spec = ResearchSpec.from_dict(payload["spec"])
            if spec.origin != payload["origin"]:
                raise PermissionError("research_start_origin_changed")
            spec.paid = True
            from .engine import start_research

            return start_research(spec, background=True)
        if payload.get("operation") == "research_pipeline_repair":
            self._owned(payload["run_id"], chat, sender)
            revision = self._manager(payload["run_id"]).confirm_apply(
                payload["candidate_id"], payload["confirmation"], sender
            )
            from .engine import resume_research

            resumed = resume_research(
                payload["run_id"], store_root=self.store.root, background=True
            )
            return {"revision": revision, "resume": resumed}
        raise ValueError("unsupported_research_confirmation")
