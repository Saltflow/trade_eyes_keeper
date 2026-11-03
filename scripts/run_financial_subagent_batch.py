"""Run independent annual-report extractions through the existing v2 pipeline."""

import argparse
import copy
import hashlib
import json
import os
import re
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import build_fundamental_data_v2 as pipeline

REPO = Path(__file__).resolve().parents[1]
ROLES = ("BS", "IS", "CFS")
TITLES = {"BS": "合并资产负债表", "IS": "合并利润表", "CFS": "合并现金流量表"}
QUOTA = re.compile(
    r"(?:usage limit|rate limit|rate_limit|quota exceeded|insufficient_quota|"
    r"too many requests|you have hit your .* limit|usage_limit_reached|"
    r"http(?: status)?[ :]+429|\b429 too many requests)",
    re.IGNORECASE,
)


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf8"
    )
    os.replace(temporary, path)


def has_issues(value):
    if isinstance(value, dict):
        return any(
            (key == "issues" and bool(item)) or has_issues(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(has_issues(item) for item in value)
    return False


class Paused(Exception):
    pass


class Batch:
    def __init__(self, output, records, manifest_hash, workers):
        self.output = output
        self.semaphore = threading.Semaphore(workers)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.pause_reason = None
        self.local = threading.local()
        self.status_path = output / "status.json"
        if self.status_path.exists():
            self.status = pipeline.parse(self.status_path.read_text("utf8"))
            if self.status.get("manifest_sha256") != manifest_hash:
                raise ValueError("output_manifest_mismatch")
        else:
            self.status = {
                "manifest_sha256": manifest_hash,
                "created_at": now(),
                "records": {r["id"]: {"status": "pending"} for r in records},
            }
        self.status["state"] = "running"
        self.status["pause_reason"] = None
        self.status["active_native_calls"] = 0
        self.status.setdefault("native_calls", 0)
        self.status["status_meaning"] = {
            "processed": "execution finished without recorded issues; completeness is not verified",
            "processed_with_issues": "execution finished with recorded issues; completeness is not verified",
        }
        for item in self.status["records"].values():
            if "active_call" in item:
                item["interrupted_call"] = item.pop("active_call")
        files = [*pipeline.implementation_files(REPO), Path(__file__)]
        hashes = {
            str(p.resolve()): hashlib.sha256(p.read_bytes()).hexdigest() for p in files
        }
        frozen = self.status.get("implementation_sha256")
        if frozen is not None and frozen != hashes:
            raise ValueError("batch_implementation_changed_since_start")
        self.status["implementation_sha256"] = hashes
        self.write()

    def write(self):
        self.status["updated_at"] = now()
        atomic_json(self.status_path, self.status)

    def update(self, record_id, status, **details):
        with self.lock:
            current = self.status["records"].setdefault(record_id, {})
            current.update(status=status, updated_at=now(), **details)
            if status == "attempted":
                current["started_at"] = now()
            elif status in {
                "processed",
                "processed_with_issues",
                "failed",
                "incomplete_or_interrupted",
            }:
                current["finished_at"] = now()
            self.write()
            with (self.output / "batch.log").open("a", encoding="utf8") as stream:
                stream.write(
                    json.dumps(
                        {"id": record_id, "status": status, "at": now(), **details},
                        ensure_ascii=False,
                    )
                    + "\n"
                )

    def pause(self, reason):
        with self.lock:
            if not self.stop.is_set():
                self.pause_reason = reason
                self.stop.set()
                self.status["state"] = "paused"
                self.status["pause_reason"] = reason
                self.write()

    def check_integrity(self):
        for name, digest in self.status["implementation_sha256"].items():
            try:
                if hashlib.sha256(Path(name).read_bytes()).hexdigest() == digest:
                    continue
            except OSError:
                pass
            self.pause("implementation_integrity_changed")
            raise Paused("implementation_integrity_changed")

    def native(self, repo, folder, request, timeout, effort, block, role=None):
        with self.semaphore:
            if self.stop.is_set():
                raise Paused(self.pause_reason or "batch_paused")
            self.check_integrity()
            rid = self.local.record_id
            with self.lock:
                if self.stop.is_set():
                    raise Paused(self.pause_reason or "batch_paused")
                self.status["native_calls"] += 1
                self.status["active_native_calls"] += 1
                current = self.status["records"][rid]
                current["native_calls"] = current.get("native_calls", 0) + 1
                current["active_call"] = {
                    "block": block,
                    "role": role,
                    "started_at": now(),
                }
                self.write()
            try:
                result = pipeline.call_subagent(
                    repo,
                    folder,
                    request,
                    pipeline.parse,
                    timeout=timeout,
                    native=True,
                    effort=effort,
                )
            finally:
                with self.lock:
                    self.status["active_native_calls"] -= 1
                    current = self.status["records"][rid]
                    current["last_call"] = {
                        **current.pop("active_call"),
                        "finished_at": now(),
                    }
                    self.write()
            receipt = pipeline.parse((folder / "receipt.json").read_text("utf8"))
            if receipt.get("status") != "received":
                stderr = folder / "native-stderr.txt"
                error_text = (
                    stderr.read_text("utf8", errors="replace")
                    if stderr.exists()
                    else ""
                )
                events = folder / "native-events.jsonl"
                if events.exists():
                    for line in events.read_text("utf8", errors="replace").splitlines():
                        try:
                            item = json.loads(line)
                        except ValueError:
                            continue
                        event_type = str(item.get("type", "")).lower()
                        if event_type in {"error", "turn.failed", "thread.failed"}:
                            error_text += "\n" + json.dumps(item, ensure_ascii=False)
                if QUOTA.search(error_text):
                    self.pause("native_account_quota_or_rate_limit")
                    raise Paused("native_account_quota_or_rate_limit")
            return result

    def receive(
        self,
        repo,
        folder,
        name,
        request,
        config,
        replay_source=None,
        subagent_timeout=None,
        subagent_native=False,
        subagent_effort="high",
    ):
        if name != "statements":
            return self.native(
                repo, folder, request, subagent_timeout, subagent_effort, name
            ), []

        parts = []
        for role in ROLES:
            child = folder / "sources" / role
            scoped = copy.deepcopy(request)
            scoped["messages"][-1]["content"].append(
                {
                    "type": "text",
                    "text": (
                        f"本轮并行拆分职责：你唯一负责{role}（{TITLES[role]}），只返回这一个topic，"
                        "其他两张表由另外的独立任务负责。保持原schema及原报告数值，逐行完整转录该合并报表"
                        "所有行，包括标题、空值、其中项、小计、合计和所有比较列，跨页一直提取至下一张报表开始；"
                        "排除母公司单体表。不要选摘要或关键项，不得用“本次未完成”代替能够读取的行。"
                        "不要为节约篇幅主动省略行。无需解释过程，直接输出完整JSON；不能读取的单元格才用null并给具体原因。"
                    ),
                }
            )
            pipeline.prepare_request(repo, child, scoped, [self.local.pdf])
            data = self.native(
                repo, child, scoped, subagent_timeout, subagent_effort, name, role
            )
            receipt = pipeline.parse((child / "receipt.json").read_text("utf8"))
            parts.append((role, data, receipt))

        valid = [
            (role, data)
            for role, data, receipt in parts
            if isinstance(data, dict)
            and isinstance(data.get("tables"), list)
            and not receipt.get("completion_note")
        ]
        merged = None
        notes = []
        if valid:
            merged = {
                key: copy.deepcopy(valid[0][1].get(key))
                for key in ("schema_version", "company", "report_period")
            }
            merged["tables"] = []
            merged["issues"] = []
            for role, data in valid:
                for key in ("schema_version", "company", "report_period"):
                    if data.get(key) != merged[key]:
                        notes.append(f"{role}: conflicting {key}")
                tables = [
                    t
                    for t in data["tables"]
                    if isinstance(t, dict) and t.get("role") == role
                ]
                if not tables:
                    notes.append(f"{role}: no matching table returned")
                merged["tables"].extend(tables)
                issues = data.get("issues")
                if isinstance(issues, list):
                    merged["issues"].extend(issues)
        missing = [role for role in ROLES if role not in {r for r, _ in valid}]
        receipt = {
            "mode": "mechanical_assembly",
            "status": "received" if merged else "failed",
            "sources": [f"sources/{role}/receipt.json" for role in ROLES],
            "automatic_retries": 0,
            "external_api_calls": 0,
            "usage": None,
            "assembly_notes": notes,
            "missing_sources": missing,
        }
        if missing or notes:
            receipt["completion_note"] = "statement_sources_incomplete_or_conflicting"
        # These are explicitly synthesized artifacts, not a model's raw response.
        pipeline.save(folder / "receipt.json", receipt)
        pipeline.save(folder / "assembled.json", merged)
        return merged, notes

    def run_record(self, record, config):
        rid = record["id"]
        folder = self.output / rid
        previous = self.status["records"].get(rid, {}).get("status")
        if previous in {
            "processed",
            "processed_with_issues",
            "failed",
            "incomplete_or_interrupted",
        }:
            return
        if folder.exists():
            self.update(
                rid, "incomplete_or_interrupted", reason="existing_output_preserved"
            )
            return
        if self.stop.is_set():
            return
        folder.mkdir()
        summary = {"stage": "extract", "model_calls": 0, "status": "prepared"}
        self.update(rid, "attempted", output=str(folder))
        state = "failed"
        details = {"output": str(folder)}
        try:
            pdf = Path(record["pdf"]).resolve()
            self.local.pdf = pdf
            self.local.record_id = rid
            if (
                hashlib.sha256(pdf.read_bytes()).hexdigest().lower()
                != record["pdf_sha256"].lower()
            ):
                raise ValueError("pdf_sha256_mismatch")
            pipeline.save(folder / "source.json", record)
            args = SimpleNamespace(
                stage="extract",
                output=folder,
                profile=list(dict.fromkeys(record["profiles"])),
                block=None,
                company=record["code"],
                year=record["year"],
                parse_cache_root=None,
                budget_limit_usd=None,
                paid=False,
                subagent=True,
                subagent_model="gpt-6.1-terra",
                subagent_native=True,
                subagent_effort="high",
                subagent_timeout=1800,
            )
            data = pipeline.extract(REPO, args, config, [pdf], summary)
            if data is not None:
                pipeline.publish(folder, data, config["labels"])
            if not isinstance(data, dict) or not data.get("tables"):
                raise ValueError("no_usable_output_data")
            issues = (
                summary.get("status") != "completed"
                or bool(data.get("unstructured_blocks"))
                or has_issues(data)
            )
            state = "processed_with_issues" if issues else "processed"
            details["extraction_status"] = summary.get("status")
        except Paused as exc:
            summary.update(status="incomplete_or_interrupted", error=str(exc))
            state = "incomplete_or_interrupted"
            details["reason"] = str(exc)
        except Exception as exc:  # noqa: BLE001 - isolate failures at the report boundary
            summary.update(
                status="failed",
                error_type=type(exc).__name__,
                error=str(exc),
                traceback=traceback.format_exc(),
            )
            details.update(error_type=type(exc).__name__, error=str(exc))
        finally:
            pipeline.save(folder / "summary.json", summary)
            self.update(rid, state, **details)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.workers <= 5:
        parser.error("workers must be between 1 and 5")
    raw = args.manifest.read_bytes()
    manifest = pipeline.parse(raw.decode("utf8"))
    records = manifest["records"]
    config = pipeline.parse(
        (REPO / "config/fundamental_data_v2.json").read_text("utf8")
    )
    ids = set()
    for record in records:
        rid = record["id"]
        if (
            not isinstance(rid, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", rid)
            or rid in ids
        ):
            raise ValueError("invalid_or_duplicate_record_id")
        ids.add(rid)
        if not all(
            k in record
            for k in ("code", "year", "name", "pdf", "profiles", "pdf_sha256", "task")
        ):
            raise ValueError("missing_record_field")
        if not record["profiles"] or any(
            p not in config["industry_profiles"] for p in record["profiles"]
        ):
            raise ValueError("invalid_profiles")
    args.output.mkdir(parents=True, exist_ok=True)
    batch = Batch(
        args.output.resolve(), records, hashlib.sha256(raw).hexdigest(), args.workers
    )
    pipeline.receive = batch.receive
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [
            executor.submit(batch.run_record, record, config) for record in records
        ]
        for future in as_completed(futures):
            future.result()
    with batch.lock:
        batch.status["state"] = "paused" if batch.stop.is_set() else "finished"
        batch.status["pause_reason"] = batch.pause_reason
        batch.write()
    return 2 if batch.stop.is_set() else 0


if __name__ == "__main__":
    raise SystemExit(main())
