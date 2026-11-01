"""Stage adapters: reuse financial contracts, preserve partial evidence and raw text."""

from __future__ import annotations

import base64
import copy
import json
import re
from decimal import Decimal
from pathlib import Path

from tokenizers import Tokenizer

from .common import artifact_key, atomic_write, file_sha, fingerprint, load


class LocalStageError(ValueError):
    """One input or derived artifact is unavailable; other subjects may continue."""


def visible(receipt: dict) -> str:
    folder = Path(receipt.get("capture_dir", "__missing_capture__"))
    for name in ("content.md", "content.txt"):
        path = folder / name
        if path.is_file():
            expected = receipt.get("content_sha256")
            if expected and file_sha(path) != expected:
                raise LocalStageError("response_content_integrity_changed")
            return path.read_text("utf-8")
    return ""


def usable_artifact(store, key: str) -> dict | None:
    value = store.get_artifact(key)
    if not value:
        from .recovery import recover_artifact

        value = recover_artifact(store, key)
    if not value or value.get("status") not in {
        "complete",
        "partial",
        "completed",
        "succeeded",
    }:
        return None
    paths = [value["path"], *value.get("metadata", {}).get("copies", [])]
    for name in paths:
        path = Path(name)
        if path.is_file() and file_sha(path) == value["sha256"]:
            return {**value, "path": str(path)}
    return None


def market_fingerprint(rows: list[dict], valuation_at: str | None = None) -> list[dict]:
    values = []
    for row in rows:
        quote_time = row.get("quote_time") or ""
        day = row.get("date") or quote_time[:10] or None
        price = row.get("price", row.get("close"))
        values.append(
            {
                "instrument": row.get("instrument") or row.get("code"),
                "price": format(Decimal(str(price)).normalize(), "f")
                if price is not None
                else None,
                "date": day,
                "valuation_at": valuation_at or row.get("valuation_at") or day,
                "currency": row.get("currency"),
                "share_class": row.get("share_class"),
                "adr_ratio": row.get("adr_ratio"),
                "fx": row.get("fx"),
            }
        )
    return sorted(values, key=lambda row: str(row["instrument"]))


class StageAdapters:
    def __init__(
        self, repo: Path, store, run_id: str, executor, output: Path, event=None
    ):
        self.repo, self.store, self.run_id = Path(repo), store, run_id
        self.executor, self.output = executor, Path(output)
        self.event = event or (
            lambda event, **fields: store.event(run_id, event, **fields)
        )
        self.config = load(self.repo / "config/fundamental_data_v2.json")
        self.config["examples_path"] = str(self.repo / self.config["examples_path"])
        self.summary_config = load(self.repo / "config/company_summary_contract.json")
        self.research_config = load(self.repo / "config/research_report.json")
        path = self.repo / self.research_config["tokenizer"]
        self.tokenizer = Tokenizer.from_file(str(path))
        self.retained = {}

    def token_count(self, text: str) -> int:
        return len(self.tokenizer.encode(text).ids)

    def request(
        self, system: str, text: str, images: dict[str, Path] | None = None
    ) -> dict:
        parts = [{"type": "text", "text": text}]
        for label, path in (images or {}).items():
            parts += [
                {"type": "text", "text": "实际图片：" + label},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64,"
                        + base64.b64encode(path.read_bytes()).decode(),
                        "detail": "original",
                    },
                },
            ]
        return {
            "model": "deepseek-flash",
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
            "max_tokens": 131072,
            "stream": True,
            "stream_options": {"include_usage": True},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": parts},
            ],
        }

    def estimate(self, request: dict) -> int:
        from scripts.run_company_summary import estimate

        return estimate(request, self.tokenizer, self.research_config)

    def check_context(self, request: dict) -> dict:
        count = self.estimate(request)
        total = (count * 11 + 9) // 10 + int(request["max_tokens"]) + 8192
        raw_bytes = len(
            json.dumps(request, ensure_ascii=False, indent=2).encode("utf-8")
        )
        if total > 1000000:
            raise LocalStageError("hard_context_limit_no_truncation")
        if raw_bytes > 50 * 1024 * 1024:
            raise LocalStageError("hard_request_bytes_limit_no_truncation")
        if count > 500000:
            self.event(
                "soft_context_waterline", input_tokens=count, conservative_total=total
            )
        return {
            "input_tokens_estimate": count,
            "conservative_total": total,
            "request_bytes": raw_bytes,
            "soft_waterline_exceeded": count > 500000,
        }

    def call(self, key, request, phase, company="", hold_usd=0):
        check = self.check_context(request)
        self.event(
            "request_prepared", task_key=key, phase=phase, company=company, **check
        )
        return self.executor.call(
            key,
            request,
            self.output / "execution",
            phase,
            company=company,
            hold_usd=hold_usd,
        )

    def extraction_requests(
        self,
        issuer: str,
        code: str,
        year: int,
        versions: dict,
        profiles: list[str],
        content: list,
    ) -> list[dict]:
        from scripts.build_fundamental_data_v2 import planned_jobs
        from src.fundamental_data.v2_transport import payload

        profiles = [p for p in profiles if p in self.config["industry_profiles"]]
        names = planned_jobs(self.config, profiles)
        ownership = {
            name: {k: self.config["jobs"][name][k] for k in ("topics", "modules")}
            for name in names
        }
        prefix = {
            "expected_company": code,
            "expected_year": year,
            "profiles": profiles,
            "frozen_job_ownership": ownership,
            "issuer_id": issuer,
            "annual_bundle_versions": versions,
        }
        full = [
            {"type": "text", "text": json.dumps(prefix, ensure_ascii=False)},
            *copy.deepcopy(content),
        ]
        result = []
        for name in names:
            value = payload(self.config, name, full)
            value.update(stream=True, stream_options={"include_usage": True})
            value["max_tokens"] = min(131072, value["max_tokens"])
            result.append(
                {
                    "key": artifact_key("extract:" + name, issuer, versions),
                    "block": name,
                    "request": value,
                }
            )
        return result

    def accept_extraction(
        self, job: dict, receipt: dict, issuer: str, versions: dict
    ) -> tuple[dict | None, dict]:
        from src.fundamental_data.v2_schema import parse, validate

        key = job["key"]
        cached = usable_artifact(self.store, key)
        if cached:
            return load(Path(cached["path"])), {
                "block": job["block"],
                "status": cached["status"],
                "reused": True,
            }
        raw = visible(receipt)
        failure = {
            "block": job["block"],
            "status": receipt.get("status", "failed"),
            "capture_dir": receipt.get("capture_dir"),
            "error": receipt.get("reason") or receipt.get("error_type"),
        }
        if not raw:
            return None, failure
        try:
            value = parse(raw)
            schema_stage = self.config["jobs"][job["block"]].get(
                "schema_stage", job["block"]
            )
            errors = validate(self.config, schema_stage, value)
            if not isinstance(value, dict) or errors:
                raise LocalStageError("schema_invalid: " + "; ".join(errors[:3]))
        except (ValueError, TypeError, AttributeError) as error:
            return None, {
                **failure,
                "status": "invalid_json_or_schema",
                "error": str(error),
            }
        path = self.output / "parsed_blocks" / (key + ".json")
        if not path.exists():
            atomic_write(path, value, exclusive=True)
        status = "complete" if receipt.get("status") == "complete" else "partial"
        self.store.put_artifact(
            key,
            "extract",
            path,
            status=status,
            metadata={
                "issuer": issuer,
                "versions": versions,
                "block": job["block"],
                "receipt": receipt,
            },
        )
        return value, {**failure, "status": status}

    def assemble_annual(
        self,
        issuer: str,
        code: str,
        year: int,
        versions: dict,
        parts: dict,
        failures: list[dict],
    ) -> dict:
        from src.fundamental_data.v2_schema import combine_blocks

        key = artifact_key("annual", issuer, versions)
        value = combine_blocks(
            self.config, parts, code, year, report_id=fingerprint(versions)
        )
        for failure in failures:
            if failure["status"] not in {"complete", "completed", "succeeded"}:
                value["unstructured_blocks"][failure["block"]] = failure
                value["block_status"][failure["block"]] = failure["status"]
        path = self.output / "annuals" / (key + ".json")
        if path.exists():
            path = path.parent / (key + "_" + fingerprint(value)[:20] + ".json")
        if not path.exists():
            atomic_write(path, value, exclusive=True)
        status = (
            "complete"
            if parts and all(x["status"] == "complete" for x in failures)
            else "partial"
        )
        page = None
        try:
            from src.fundamental_data.v2_view import publish

            views = path.with_suffix("")
            publish(views, value, self.config["labels"])
            page = str(views / "阅读.html")
        except Exception as error:  # noqa: BLE001 - optional derived views
            self.event("presentation_failed", phase="annual", error=str(error))
        return self.store.put_artifact(
            key,
            "annual",
            path,
            status=status,
            metadata={
                "issuer": issuer,
                "year": year,
                "versions": versions,
                "failures": failures,
                "accepted_blocks": len(parts),
                "reading_html": page,
            },
        )

    def context(
        self,
        issuer: str,
        versions: dict,
        annuals: list[dict],
        coverage: dict,
        force=False,
    ) -> dict | None:
        from src.fundamental_data.model_context import assemble, audit, reading

        key = artifact_key("context", issuer, versions)
        cached = usable_artifact(self.store, key)
        if cached and not force:
            return cached
        entries, metadata, failures = [], [], []
        for annual in annuals:
            try:
                data = load(Path(annual["path"]))
                if not isinstance(data, dict):
                    raise LocalStageError("annual_json_not_object")
                if annual.get("metadata", {}).get("accepted_blocks") == 0:
                    continue
                name = str(annual.get("metadata", {}).get("year", len(entries)))
                entries.append((name, data))
                metadata.append(
                    {
                        "declared_issuer": issuer,
                        "year": name,
                        "company": data.get("company"),
                        "reports": data.get("reports"),
                        "metadata": data.get("metadata", []),
                        "structure_tables": [
                            t
                            for t in data.get("tables", [])
                            if isinstance(t, dict)
                            and t.get("topic")
                            in self.config["peer_input"]["structure_topics"]
                        ],
                        "policy_context": [
                            p
                            for p in data.get("policies", [])
                            if isinstance(p, dict)
                            and p.get("topic")
                            in self.config["peer_input"]["policy_topics"]
                        ],
                        "issues": data.get("issues", []),
                    }
                )
                failures.extend(annual.get("metadata", {}).get("failures", []))
            except (OSError, ValueError, TypeError) as error:
                self.event(
                    "local_input_unavailable",
                    phase="context",
                    company=issuer,
                    error=str(error),
                )
        if not entries:
            return None
        plan = assemble(entries)
        text, cells, _narrative = reading(entries, plan, self.config["labels"])
        retained = audit(entries, plan, cells, text)
        folder = self.output / "contexts" / key
        if folder.exists():
            folder = folder / (
                "revision_"
                + fingerprint(
                    {"annuals": [a["sha256"] for a in annuals], "coverage": coverage}
                )[:20]
            )
        if (folder / "模型正文.md").exists():
            # Identical offline rebuilds need no new materialization.
            return cached
        atomic_write(folder / "数据保留.json", retained, exclusive=True)
        if not retained["passed"]:
            raise LocalStageError("company_retention_failed")
        atomic_write(folder / "模型正文.md", text, exclusive=True)
        atomic_write(folder / "元数据.json", metadata, exclusive=True)
        self.present(text, folder / "阅读.html")
        status = (
            "partial"
            if coverage.get("gaps") or any(f["status"] != "complete" for f in failures)
            else "complete"
        )
        return self.store.put_artifact(
            key,
            "context",
            folder / "模型正文.md",
            status=status,
            metadata={
                "issuer": issuer,
                "versions": versions,
                "coverage": coverage,
                "retention": retained,
                "metadata_path": str(folder / "元数据.json"),
                "metadata_sha256": file_sha(folder / "元数据.json"),
                "failures": failures,
                "tokens_local": self.token_count(text),
                "source_data": [a["path"] for a in annuals],
                "source_data_sha256": {a["path"]: a["sha256"] for a in annuals},
                "reading_html": str(folder / "阅读.html"),
            },
        )

    def partition(self, system: str, header: str, material: str) -> list[str]:
        """Character slicing preserves every byte and whitespace; never tokenizer-decode."""
        if self.estimate(self.request(system, header + material)) <= 760000:
            return [material]
        pieces, cursor = [], 0
        while cursor < len(material):
            low, high = 1, len(material) - cursor
            while low < high:
                mid = (low + high + 1) // 2
                if (
                    self.estimate(
                        self.request(system, header + material[cursor : cursor + mid])
                    )
                    <= 740000
                ):
                    low = mid
                else:
                    high = mid - 1
            piece = material[cursor : cursor + low]
            self.check_context(self.request(system, header + piece))
            pieces.append(piece)
            cursor += low
        assert "".join(pieces) == material
        return pieces

    def summary_plan(
        self, issuer: str, versions: dict, context: dict, coverage: dict
    ) -> dict:
        from .parsing import hash_lock

        key = artifact_key("summary_plan", issuer, versions)
        with hash_lock(self.store.root / "summary_plan_locks" / (key + ".lock")):
            cached = usable_artifact(self.store, key)
            if cached:
                self.event("summary_plan_reused", company=issuer, artifact_key=key)
                return load(Path(cached["path"]))
            if any(
                record["key"] == key
                for record in self.store.list_artifacts("summary_plan")
            ):
                raise LocalStageError(
                    "summary_plan_integrity_unavailable_no_automatic_rebuild"
                )
            plan = self._build_summary_plan(issuer, versions, context, coverage)
            path = self.output / "summary_plans" / (key + "_full.json")
            atomic_write(path, plan, exclusive=True)
            self.store.put_artifact(
                key,
                "summary_plan",
                path,
                metadata={
                    "issuer": issuer,
                    "versions": versions,
                    "source_context": context["path"],
                },
            )
            return plan

    def _build_summary_plan(
        self, issuer: str, versions: dict, context: dict, coverage: dict
    ) -> dict:
        key = artifact_key("summary", issuer, versions)
        system = (self.repo / self.summary_config["prompts"]["summary"]).read_text(
            "utf-8"
        )
        system += "\n本轮为年报分析资产，不输入现价，不以旧估值判断当前买入回报。资料覆盖以完整覆盖清单为准，局部分段没有某年不表示原件未提供。空附件、null检查位不自动代表提取失败。不同原报/重述/股权口径并列，不猜未披露。"
        questions = "\n".join(
            f"Q{i}: {q}" for i, q in enumerate(self.summary_config["questions"], 1)
        )
        header = (
            "\n# 全局覆盖和执行缺口\n"
            + json.dumps(coverage, ensure_ascii=False)
            + "\n# 七问\n"
            + questions
        )
        material = Path(context["path"]).read_text("utf-8")
        definitions = re.search(r"(?ms)^## 共用定义\s*\n.*?(?=^## [^\n]|\Z)", material)
        if definitions:
            header += "\n# 每个分段都适用的原始共用定义\n" + definitions.group(0)
        for failure in context.get("metadata", {}).get("failures", []):
            if failure.get("capture_dir") and failure.get("status") not in {
                "complete",
                "completed",
            }:
                appendix = visible(failure)
                if appendix:
                    material += (
                        "\n\n# 原失败/截断附件（未核验，非源文未披露）\n"
                        + json.dumps(failure, ensure_ascii=False)
                        + "\n"
                        + appendix
                    )
        pieces = self.partition(system, header, material)
        if len(pieces) > 20:
            raise LocalStageError("summary_partition_plan_exceeds_twenty_tasks")
        atomic_write(
            self.output / "summary_plans" / (key + ".json"),
            {
                "issuer": issuer,
                "versions": versions,
                "material_sha256": fingerprint(material),
                "lossless_partition_verified": "".join(pieces) == material,
                "parts": [
                    {
                        "index": i,
                        "sha256": fingerprint(piece),
                        "tokens_local": self.token_count(piece),
                    }
                    for i, piece in enumerate(pieces)
                ],
                "common_header_sha256": fingerprint(header),
                "common_definitions_in_every_part": bool(definitions),
                "source_context": context["path"],
            },
        )
        return {
            "key": key,
            "system": system,
            "header": header,
            "pieces": pieces,
            "source_partial": context["status"] != "complete",
            "context": context,
        }

    def save_text_artifact(
        self, key: str, stage: str, text: str, status: str, metadata: dict
    ) -> dict | None:
        if not text.strip():
            return None
        filename = {
            "summary": "完整摘要.md",
            "peer": "完整分析.md",
            "research": "最终报告.md",
        }[stage]
        path = (
            self.output
            / ("summaries" if stage == "summary" else stage)
            / key
            / filename
        )
        if path.exists() and path.read_text("utf-8") != text:
            path = path.parent / ("revision_" + fingerprint(text)[:16]) / filename
        if not path.exists():
            atomic_write(path, text, exclusive=True)
        self.present(text, path.with_suffix(".html"))
        return self.store.put_artifact(
            key,
            stage,
            path,
            status=status,
            metadata={**metadata, "tokens_local": self.token_count(text)},
        )

    def present(self, text: str, path: Path) -> None:
        try:
            from scripts.research_render import html

            atomic_write(path, html(text))
        except Exception as error:  # noqa: BLE001 - optional derived rendering
            self.event("presentation_failed", error=str(error), path=str(path))

    def render_charts(self, text: str, folder: Path, status: str) -> dict[str, Path]:
        """Chart/HTML/sidecar faults must never erase the paid raw response."""
        from scripts import research_render

        folder.mkdir(parents=True, exist_ok=True)
        rendered = {}
        try:
            rendered = research_render.render(folder, text, status)
        except Exception as error:  # noqa: BLE001 - optional derived rendering
            self.event(
                "presentation_failed",
                phase="research",
                error=str(error),
                path=str(folder),
            )
        pictures = {
            "assets/" + folder.name + "_" + p.name: p
            for p in sorted((folder / "assets").glob("*.png"))
        }
        self.event(
            "charts_rendered",
            phase="research",
            view=folder.name,
            image_count=len(pictures),
            chart_blocks=rendered.get("chart_blocks", 0),
            issues=rendered.get("chart_issues", []),
        )
        return pictures

    def research_request(
        self,
        index: int,
        base: str,
        previous: str = "",
        pictures: dict[str, Path] | None = None,
    ) -> dict:
        cfg = self.research_config
        common = "\n\n".join((self.repo / p).read_text("utf-8") for p in cfg["prompts"])
        common += "\n# 原七问\n" + "\n".join(self.summary_config["questions"])
        common += "\n" + (self.repo / cfg["rounds"][index]["prompt"]).read_text("utf-8")
        text = base
        if index:
            text += "\n\n# 上一轮原文（可能partial，不能改写资料覆盖）\n" + previous
            text += "\n\n# 本轮实际图片\n" + (
                "\n".join(pictures or {}) or "未提供实际图片，不得声称已视觉审查。"
            )
        return self.request(common, text, pictures)

    def finalize_images(
        self, text: str, pictures: dict[str, Path], target: Path
    ) -> dict:
        for label, path in pictures.items():
            if not re.fullmatch(r"assets/[A-Za-z0-9_.-]+\.png", label):
                continue
            destination = target / label
            if not destination.exists():
                atomic_write(destination, path.read_bytes(), exclusive=True)
        references = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text)
        missing = [name for name in references if name not in pictures]
        note = {
            "available_images": list(pictures),
            "missing_image_references": missing,
            "financial_validation": "not_independently_verified",
        }
        atomic_write(target / "呈现注记.json", note)
        return note
