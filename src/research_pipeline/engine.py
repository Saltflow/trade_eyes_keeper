"""One resumable research DAG; optional acquisition/presentation never owns liveness."""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import psutil
from zoneinfo import ZoneInfo

from .common import (
    annual_versions,
    artifact_key,
    atomic_write,
    file_sha,
    fingerprint,
    load,
    now,
)
from .models import AnnualDocument, Instrument, ResearchSpec
from .observability import ProgressPublisher, progress_snapshot
from .parsing import ParsedCache
from .pricing import PriceService, tariff_phase
from .sources import SourceService
from .stages import (
    LocalStageError,
    StageAdapters,
    market_fingerprint,
    usable_artifact,
    visible,
)
from .store import Store

TERMINAL = {
    "complete",
    "partial",
    "failed",
    "cancelled",
    "budget_stop",
    "awaiting_approval",
}


@dataclass(frozen=True)
class FutureHold:
    jobs: int


class LazyModelExecutor:
    """A complete cache hit must not read keys, query accounts, or fetch tariffs."""

    def __init__(
        self,
        store,
        spec,
        run_id,
        *,
        post=None,
        key=None,
        rates=None,
        account_reader=None,
        event=None,
    ):
        self.store, self.spec, self.run_id = store, spec, run_id
        self.post, self.key, self.rates = post, key, rates
        self.configured_rates = copy.deepcopy(rates)
        self.account_reader, self.event = account_reader, event
        self.actual = None
        self.executors = []
        self.rate_stamp = None
        self.lock = threading.RLock()

    def _prepare(self):
        from .runtime import ModelExecutor

        with self.lock:
            stamp = (
                (datetime.now(timezone.utc).date().isoformat(), tariff_phase())
                if self.post is None
                else ("offline", "injected")
            )
            if self.actual is not None and self.rate_stamp == stamp:
                return self.actual
            rates = copy.deepcopy(self.configured_rates)
            if rates is None and self.post is not None:
                # Offline boundary injection is explicitly labelled simulated.
                rates = {
                    "input": ".15",
                    "output": ".6",
                    "cached_input": ".003",
                    "peak_rates": {"input": ".3", "output": "1.2"},
                    "pricing_utc_dates": [
                        datetime.now(timezone.utc).date().isoformat()
                    ],
                }
            if rates is None:
                service = PriceService(self.store.root)
                try:
                    snapshot = service.refresh()
                except Exception:  # noqa: BLE001 - bounded recent official tariff fallback
                    snapshot = service.cached()
                    if (
                        not snapshot
                        or (
                            datetime.now(timezone.utc)
                            - datetime.fromisoformat(snapshot["verified_at"])
                        ).total_seconds()
                        > 86400
                    ):
                        raise LocalStageError(
                            "pricing_unverified_no_new_POST"
                        ) from None
                    if self.event:
                        self.event(
                            "pricing_recent_snapshot_used",
                            verified_at=snapshot["verified_at"],
                        )
                atomic_write(self.spec.output / "pricing_snapshot.json", snapshot)
                rates = {
                    **snapshot["rates"][tariff_phase()],
                    "peak_rates": snapshot["rates"]["peak"],
                    "pricing_utc_dates": [
                        datetime.now(timezone.utc).date().isoformat()
                    ],
                    "_tariff_phase": stamp[1],
                    "_account_tariff_ratio": snapshot["account_tariff_ratio"],
                }
            self.actual = ModelExecutor(
                self.store,
                self.spec,
                self.run_id,
                post=self.post,
                key=self.key,
                rates=rates,
                account_reader=self.account_reader,
                event_callback=self._persisted_event,
            )
            self.rates = rates
            self.rate_stamp = stamp
            self.executors.append(self.actual)
            return self.actual

    def _persisted_event(self, record):
        # ModelExecutor already committed this event. Never persist it twice.
        owner = getattr(self.event, "__self__", None)
        publisher = getattr(owner, "publisher", None)
        if publisher:
            publisher.observe(record)

    def call(
        self,
        task_key,
        request,
        folder,
        phase,
        company="",
        hold_usd=0,
        repair=False,
        deadline=None,
    ):
        existing = self.store.get_attempt(task_key)
        if existing and existing["status"] in {"queued", "sending"}:
            until = deadline or time.monotonic() + 7200
            while existing["status"] in {"queued", "sending"}:
                if time.monotonic() >= until:
                    return {
                        "status": "unknown",
                        "reason": "producer_still_active",
                        "capture_dir": existing["capture_dir"],
                        "reused": True,
                    }
                self.store.reconcile()
                time.sleep(0.05)
                existing = self.store.get_attempt(task_key)
        if existing and existing["status"] not in {"queued", "sending", "not_sent"}:
            receipt = copy.deepcopy(
                existing.get("receipt")
                or {
                    "status": existing["status"],
                    "usage": None,
                    "cost": {"usd": None, "kind": "usage_unknown"},
                }
            )
            return {
                **receipt,
                "capture_dir": existing["capture_dir"],
                "reused": True,
                "charged_to_current_run": False,
            }
        if Decimal(self.store.budget(self.run_id)["total_remaining_usd"]) <= 0:
            return {
                "status": "not_sent",
                "reason": "budget_stop",
                "capture_dir": "",
                "reused": False,
            }
        try:
            executor = self._prepare()
        except (LocalStageError, ValueError) as error:
            return {
                "status": "not_sent",
                "reason": str(error),
                "capture_dir": "",
                "reused": False,
            }
        if isinstance(hold_usd, FutureHold):
            peak = self.rates.get("peak_rates", {"input": ".3", "output": "1.2"})
            hold_usd = (
                hold_usd.jobs
                * (
                    Decimal(1048576) * Decimal(str(peak["input"]))
                    + Decimal(131072) * Decimal(str(peak["output"]))
                )
                / 1000000
            )
        receipt = executor.call(
            task_key,
            request,
            folder,
            phase,
            company=company,
            hold_usd=hold_usd,
            repair=repair,
            deadline=deadline,
        )
        if (
            receipt.get("status") == "not_sent"
            and receipt.get("reason") == "pricing_refresh_needed"
        ):
            # A tariff boundary can pass while a task waits for a lane. Its POST
            # has not happened; refresh once and admit the same logical work.
            with self.lock:
                self.rate_stamp = None
            executor = self._prepare()
            receipt = executor.call(
                task_key,
                request,
                folder,
                phase,
                company=company,
                hold_usd=hold_usd,
                repair=repair,
                deadline=deadline,
            )
        if (
            receipt.get("status") == "not_sent"
            and receipt.get("reason") == "budget_stop"
        ):
            run = self.store.get_run(self.run_id)
            # Releasing an unused hold is admission retry, never a repeated POST.
            if not run.get("repair_hold_released") and not run.get("repair_incidents"):
                self.store.update_run(self.run_id, repair_hold_released=True)
                if self.event:
                    self.event("unused_repair_hold_released")
                receipt = executor.call(
                    task_key,
                    request,
                    folder,
                    phase,
                    company=company,
                    hold_usd=hold_usd,
                    repair=repair,
                    deadline=deadline,
                )
        return receipt

    def close(self):
        for executor in self.executors:
            executor.close()


def _freeze_runtime(repo: Path, output: Path) -> Path:
    """Copy public implementation/config only. No .env, credentials or business data."""
    target = output / "frozen_runtime"
    selected = []
    for base in (repo / "src", repo / "scripts"):
        selected.extend(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)
    # The repair host must test the same frozen public inputs it diagnoses.
    # A tests/ directory containing only JSON examples is not a test snapshot.
    selected.extend((repo / "tests").glob("test_research_pipeline*.py"))
    for pattern in (
        "test_data_hub*.py", "test_model_gateway*.py", "test_task_runtime*.py",
        "test_agent_runtime*.py", "test_reusable_platform*.py",
    ):
        selected.extend((repo / "tests").glob(pattern))
    selected.extend((repo / "tests/validation").rglob("*.py"))
    selected.extend(
        repo / name
        for name in (
            "tests/__init__.py",
            "tests/conftest.py",
            "tests/research_investigation_support.py",
            "pytest.ini",
            "pyproject.toml",
            "requirements.txt",
            "conftest.py",
            "config/__init__.py",
            "config/alerts.yaml",
        )
        if (repo / name).is_file()
    )
    public_configs = [
        repo / "config" / name
        for name in (
            "fundamental_data_v2.json",
            "company_summary_contract.json",
            "research_report.json",
            "research_tariff_calendar.json",
        )
    ]
    selected.extend(public_configs)
    for config in public_configs:
        pending = [load(config)]
        while pending:
            node = pending.pop()
            if isinstance(node, dict):
                pending.extend(node.values())
            elif isinstance(node, list):
                pending.extend(node)
            elif isinstance(node, str) and node.endswith((".json", ".md")):
                path = (repo / node).resolve()
                if (
                    path.is_relative_to(repo)
                    and path.is_file()
                    and path.parts[len(repo.parts)] in {"config", "docs", "tests"}
                ):
                    selected.append(path)
    selected.extend((repo / "config/prompts").glob("*.md"))
    selected.extend(
        p
        for p in (repo / "docs/design").glob("*.md")
        if "research" in p.name or "summary" in p.name
    )
    tokenizer = repo / "data/fundamental_quant/tokenizer/tokenizer.json"
    if tokenizer.is_file():
        selected.append(tokenizer)
    files = {}
    for source in sorted(set(selected)):
        if source.is_symlink() or not source.resolve().is_relative_to(repo.resolve()):
            raise ValueError("runtime_public_input_link_forbidden")
        relative = source.relative_to(repo)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        files[str(relative).replace("\\", "/")] = file_sha(destination)
    atomic_write(
        output / "runtime_freeze.json",
        {"files": files, "root": str(target)},
        exclusive=True,
    )
    # The immutable base manifest also supports repair candidate verification.
    atomic_write(target / "runtime_manifest.json", {"files": files}, exclusive=True)
    return target


def _verify_runtime(run: dict) -> Path:
    from .integrity import verify_input_manifests

    verify_input_manifests(run)
    revision = run.get("execution_revision") or {}
    root = Path(revision.get("runtime_repo") or run["runtime_repo"])
    if revision:
        manifest_path = Path(revision.get("manifest", root.parent / "revision.json"))
        if not manifest_path.is_file():
            raise ValueError("approved_runtime_manifest_missing")
        value = load(manifest_path)
        files = value.get("runtime_files") or value.get("files")
        if not files:
            raise ValueError("approved_runtime_manifest_missing_files")
    else:
        value = load(Path(run["spec"]["output"]) / "runtime_freeze.json")
        files = value["files"]
    for name, digest in files.items():
        path = Path(name)
        path = path if path.is_absolute() else root / path
        if not path.is_file() or file_sha(path) != digest:
            raise ValueError("runtime_freeze_changed: " + str(name))
    return root


def _new_run(spec: ResearchSpec, *, freeze=False) -> tuple[Store, dict]:
    if spec.output.exists():
        raise FileExistsError("output_must_be_new")
    spec.output.mkdir(parents=True)
    inputs = {}
    for name in ("assumptions", "market_snapshot", "source_manifest"):
        source = getattr(spec, name)
        if source:
            source = Path(source).resolve()
            target = spec.output / "inputs" / (name + source.suffix)
            atomic_write(target, source.read_bytes(), exclusive=True)
            inputs[name] = {
                "source": str(source),
                "path": str(target),
                "sha256": file_sha(target),
            }
            if name == "source_manifest":
                value = load(source)
                records = (
                    value
                    if isinstance(value, list)
                    else value.get("documents", value.get("records", []))
                )
                for record in records:
                    for field in ("local_path", "pdf", "pdf_path"):
                        if record.get(field) and not Path(record[field]).is_absolute():
                            record[field] = str(
                                (source.parent / record[field]).resolve()
                            )
                normalized = target.with_name("source_manifest.normalized.json")
                atomic_write(normalized, value, exclusive=True)
                inputs[name]["normalized_path"] = str(normalized)
                inputs[name]["normalized_sha256"] = file_sha(normalized)
                target = normalized
            setattr(spec, name, target)
    atomic_write(spec.output / "input_manifest.json", inputs, exclusive=True)
    if not spec.cache_roots:
        spec.cache_roots = (spec.repo / "data/fundamental_data",)
    store = Store(spec.store_root)
    run = store.create_run(spec.to_dict())
    files = {
        str(spec.output / "input_manifest.json"): file_sha(
            spec.output / "input_manifest.json"
        )
    }
    for value in inputs.values():
        files[value["path"]] = value["sha256"]
        if value.get("normalized_path"):
            files[value["normalized_path"]] = value["normalized_sha256"]
    manifest = spec.output / "input_freeze.json"
    atomic_write(manifest, {"files": files}, exclusive=True)
    run = store.update_run(
        run["id"],
        integrity_manifests=[{"path": str(manifest), "sha256": file_sha(manifest)}],
    )
    atomic_write(spec.output / "spec.json", spec.to_dict(), exclusive=True)
    atomic_write(
        spec.output / "run.json",
        {
            "id": run["id"],
            "store_root": str(spec.store_root),
            "spec_sha256": file_sha(spec.output / "spec.json"),
        },
        exclusive=True,
    )
    if freeze:
        root = _freeze_runtime(spec.repo, spec.output)
        run = store.update_run(run["id"], runtime_repo=str(root))
    return store, run


def plan_research(spec: ResearchSpec) -> dict:
    """Offline only; unresolved discovery and cost remain visibly unknown."""
    from .legacy import LegacyImporter

    store, run = _new_run(spec)
    importer = LegacyImporter(store, spec.cache_roots, spec=spec)
    imported = importer.run()
    documents = SourceService(spec, store).local_documents()
    result = {
        "id": run["id"],
        "mode": "offline_plan",
        "companies": list(spec.companies),
        "reinvestigate": spec.reinvestigate,
        "investigation_default": True,
        "writing_rounds": 3,
        "discovery_pending": True,
        "quote_refresh_pending": not bool(spec.market_snapshot),
        "known_documents": [d.to_dict() for d in documents],
        "legacy_import": imported,
        "authorized_usd": str(spec.budget_limit_usd),
        "automatic_retries": 0,
        "paid_requests_sent": 0,
        "estimated_unknown_work": "requires_live_directory_and_material_preparation",
    }
    atomic_write(spec.output / "plan.json", result, exclusive=True)
    store.update_run(
        run["id"], state="planned", phase="offline_plan", legacy_import=imported
    )
    ProgressPublisher(store, run["id"], spec.output).publish()
    return result


class Pipeline:
    def __init__(
        self,
        spec,
        store,
        run_id,
        *,
        source=None,
        post=None,
        key=None,
        rates=None,
        account_reader=None,
        sender=None,
        resuming=False,
    ):
        self.spec, self.store, self.run_id = spec, store, run_id
        self.publisher = ProgressPublisher(store, run_id, spec.output, sender=sender)
        self.source = source or SourceService(spec, store)
        self.executor = LazyModelExecutor(
            store,
            spec,
            run_id,
            post=post,
            key=key,
            rates=rates,
            account_reader=account_reader,
            event=self.event,
        )
        self.adapters = None
        self.resuming = resuming
        self.documents, self.quotes, self.gaps, self.subjects = [], [], [], {}
        self.company_artifacts, self.final_artifacts = {}, []
        self.reused, self.blocked = 0, []
        self.model_work_needed = False

    def _parse_cache(self):
        from src.platform_composition import authority_for

        authority = getattr(self.source, "authority", None) or authority_for(self.spec.repo, explicit_root=getattr(self.spec, "authority_root", None), isolated=getattr(self.spec, "authority_isolated", False))
        root = ParsedCache.bind_root(self.store.root / "parsed", authority.materials_root / "parsed")
        return ParsedCache(root, authority=authority)

    def event(self, name, **fields):
        self.publisher.event(name, **fields)

    def phase(self, value):
        self.store.update_run(self.run_id, phase=value, gaps=self.gaps)
        self.event("phase_started", phase=value)

    def gap(self, kind, company="", **fields):
        value = {"kind": kind, "company": company, **fields}
        self.gaps.append(value)
        self.store.update_run(self.run_id, gaps=self.gaps)
        self.event("local_gap", **value)

    def stopped(self):
        run = self.store.get_run(self.run_id)
        return bool(run.get("cancel_requested") or run.get("requires_resume"))

    @contextmanager
    def dispatch_pool(self):
        pool = ThreadPoolExecutor(max_workers=self.spec.concurrency)
        try:
            yield pool
        except Exception as error:
            self.store.update_run(
                self.run_id,
                state="paused",
                requires_resume=True,
                pause_reason="program_error",
                error=str(error),
            )
            self.event(
                "queue_paused_before_repair",
                error=str(error),
                phase=self.store.get_run(self.run_id).get("phase"),
            )
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

    def discover(self):
        self.phase("annual_discovery")
        if self.resuming and (self.spec.output / "documents.json").is_file():
            self.documents = [
                AnnualDocument(**v) for v in load(self.spec.output / "documents.json")
            ]
            self.quotes = load(self.spec.output / "market_snapshot.json")
            self.gaps = load(self.spec.output / "source_coverage.json").get("gaps", [])
        else:
            from .legacy import LegacyImporter

            report = LegacyImporter(
                self.store, self.spec.cache_roots, spec=self.spec
            ).run()
            self.store.update_run(self.run_id, legacy_import=report)
            local = self.source.local_documents()
            as_of = datetime.now(ZoneInfo("Asia/Shanghai")).date()
            for identity in self.spec.companies:
                instrument = Instrument.parse(identity)
                self.event("source_check_started", company=identity)
                try:
                    docs = self.source.discover(instrument, as_of)
                except Exception as error:  # noqa: BLE001 - isolate optional IO or top-level failure
                    docs = [d for d in local if d.instrument == identity]
                    self.gap("freshness_unknown", identity, error=str(error))
                self.documents.extend(docs)
                try:
                    quote = self.source.quote(instrument, as_of)
                except Exception as error:  # noqa: BLE001 - isolate optional IO or top-level failure
                    previous = [
                        a
                        for a in self.store.list_artifacts("quote")
                        if a.get("metadata", {}).get("instrument") == identity
                    ]
                    previous.sort(
                        key=lambda a: a.get("metadata", {}).get("date", ""),
                        reverse=True,
                    )
                    quote = None
                    for item in previous:
                        if item.get("metadata", {}).get(
                            "date", ""
                        ) <= as_of.isoformat() and usable_artifact(
                            self.store, item["key"]
                        ):
                            quote = {
                                **load(Path(item["path"])),
                                "status": "stale_fallback",
                                "live_error": str(error),
                            }
                            self.gap(
                                "market_quote_stale_fallback",
                                identity,
                                date=quote.get("date"),
                                error=str(error),
                            )
                            break
                    if quote is None:
                        quote = {
                            "instrument": identity,
                            "price": None,
                            "date": None,
                            "status": "unavailable",
                            "error": str(error),
                        }
                quote.setdefault("instrument", identity)
                self.quotes.append(quote)
                if quote.get("price", quote.get("close")) is None:
                    self.gap(
                        "market_quote_unavailable", identity, error=quote.get("error")
                    )
                else:
                    quote_key = artifact_key(
                        "quote", identity, market_fingerprint([quote])
                    )
                    if not usable_artifact(self.store, quote_key):
                        path = self.store.root / "quotes" / (quote_key + ".json")
                        if path.exists():
                            path = path.parent / (
                                quote_key + "_" + uuid.uuid4().hex + ".json"
                            )
                        atomic_write(path, quote, exclusive=True)
                        self.store.put_artifact(
                            quote_key,
                            "quote",
                            path,
                            metadata={
                                "instrument": identity,
                                "date": quote.get("date", ""),
                            },
                        )
            for record in getattr(self.source, "gaps", []):
                self.gap(
                    "source_metadata_unresolved",
                    record.get("instrument", ""),
                    details=record,
                )
            # One unified financial-year window, not seven arbitrary available files.
            end = self.spec.end_year or max(
                (d.fiscal_year for d in self.documents), default=as_of.year - 1
            )
            start = self.spec.start_year or end - self.spec.years + 1
            selected = self._current_documents(
                [d for d in self.documents if start <= d.fiscal_year <= end]
            )
            provisional = {}
            for doc in selected:
                provisional.setdefault(doc.issuer_id, []).append(doc)
            cached_companies = {
                issuer
                for issuer, docs in provisional.items()
                if any(
                    usable_artifact(
                        self.store, artifact_key(stage, issuer, annual_versions(docs))
                    )
                    for stage in ("context", "summary")
                )
            }
            parsed_cache = self._parse_cache()
            acquired = []
            for doc in selected:
                try:
                    siblings = [
                        d
                        for d in provisional[doc.issuer_id]
                        if d.fiscal_year == doc.fiscal_year
                    ]
                    annual = usable_artifact(
                        self.store,
                        artifact_key(
                            "annual", doc.issuer_id, annual_versions(siblings)
                        ),
                    )
                    cached_parse = parsed_cache.get(doc.sha256) if doc.sha256 else None
                    if doc.issuer_id in cached_companies or annual or cached_parse:
                        acquired.append(doc)
                        self.event(
                            "original_reference_reused",
                            company=doc.instrument,
                            year=doc.fiscal_year,
                            original_available=bool(
                                doc.local_path and Path(doc.local_path).is_file()
                            ),
                        )
                    else:
                        acquired.append(self.source.acquire(doc))
                        self.event(
                            "original_ready",
                            company=doc.instrument,
                            year=doc.fiscal_year,
                            document_id=doc.document_id,
                        )
                except Exception as error:  # noqa: BLE001 - isolate optional IO or top-level failure
                    acquired.append(doc)
                    self.gap(
                        "original_unavailable",
                        doc.instrument,
                        year=doc.fiscal_year,
                        document_id=doc.document_id,
                        error=str(error),
                    )
            self.documents = acquired
            self.store.update_run(self.run_id, start_year=start, end_year=end)
            atomic_write(
                self.spec.output / "documents.json",
                [d.to_dict() for d in self.documents],
                exclusive=True,
            )
            atomic_write(
                self.spec.output / "market_snapshot.json", self.quotes, exclusive=True
            )
        for identity in self.spec.companies:
            selected = [d for d in self.documents if d.instrument == identity]
            issuer = self.spec.issuer_map.get(identity) or next(
                (d.issuer_id for d in selected), identity
            )
            subject = self.subjects.setdefault(
                issuer,
                {
                    "issuer": issuer,
                    "instruments": [],
                    "documents": [],
                    "versions": {},
                    "gaps": [],
                },
            )
            subject["instruments"].append(identity)
            subject["documents"].extend(selected)
        for issuer, subject in self.subjects.items():
            subject["documents"] = self._current_documents(subject["documents"])
            subject["versions"] = annual_versions(subject["documents"])
            for year, hashes in subject["versions"].items():
                annual = usable_artifact(
                    self.store, artifact_key("annual", issuer, {year: hashes})
                )
                if annual and annual["status"] == "partial":
                    self.gap(
                        "cached_annual_partial",
                        issuer,
                        year=int(year),
                        artifact_key=annual["key"],
                        reason="cached_execution_gap_or_limited_source_scope_is_preserved",
                    )
            for stage in ("context", "summary"):
                cached = usable_artifact(
                    self.store, artifact_key(stage, issuer, subject["versions"])
                )
                if cached and cached["status"] == "partial":
                    self.gap(
                        "cached_" + stage + "_partial",
                        issuer,
                        artifact_key=cached["key"],
                    )
            run = self.store.get_run(self.run_id)
            start, end = run.get("start_year"), run.get("end_year")
            subject["applicability"] = {}
            if start is not None:
                for year in range(start, end + 1):
                    if str(year) not in subject["versions"]:
                        from .applicability import declared_applicability

                        declarations = {
                            identity: declared_applicability(
                                self.spec,
                                identity,
                                year,
                                datetime.now(ZoneInfo("Asia/Shanghai")).date(),
                            )
                            for identity in subject["instruments"]
                        }
                        subject["applicability"][str(year)] = declarations
                        if declarations and all(declarations.values()):
                            self.event(
                                "annual_not_applicable",
                                company=issuer,
                                year=year,
                                declarations=declarations,
                            )
                            continue
                        self.gap(
                            "annual_original_not_found",
                            issuer,
                            year=year,
                            evidence="not_found_not_undisclosed; prelisting_not_inferred",
                        )
            subject["gaps"] = [
                g
                for g in self.gaps
                if g["company"] in [issuer, *subject["instruments"]]
            ]
        if not self.resuming:
            atomic_write(
                self.spec.output / "source_coverage.json",
                self.coverage(),
                exclusive=True,
            )
            manifest = self.spec.output / "discovery_freeze.json"
            files = {
                str(self.spec.output / name): file_sha(self.spec.output / name)
                for name in (
                    "documents.json",
                    "market_snapshot.json",
                    "source_coverage.json",
                )
            }
            atomic_write(manifest, {"files": files}, exclusive=True)
            bindings = self.store.get_run(self.run_id).get("integrity_manifests", [])
            self.store.update_run(
                self.run_id,
                integrity_manifests=[
                    *bindings,
                    {"path": str(manifest), "sha256": file_sha(manifest)},
                ],
            )
        else:
            atomic_write(
                self.spec.output / "source_coverage_current.json", self.coverage()
            )

    @staticmethod
    def _current_documents(documents):
        # Retain all report series; only supersede a full report within its series.
        groups = {}
        for d in documents:
            series = d.provenance.get(
                "report_series", Instrument.parse(d.instrument).market
            )
            groups.setdefault((d.issuer_id, d.fiscal_year, series), []).append(d)
        selected = []
        for values in groups.values():
            full = [
                d
                for d in values
                if d.kind not in {"amendment", "correction", "supplement"}
            ]
            if full:
                latest = max(d.published_at for d in full)
                selected.extend(d for d in full if d.published_at == latest)
            selected.extend(
                d for d in values if d.kind in {"amendment", "correction", "supplement"}
            )
        unique = {}
        for d in selected:
            unique[(d.issuer_id, d.fiscal_year, d.sha256 or d.document_id)] = d
        return list(unique.values())

    def coverage(self):
        return {
            "requested_instruments": list(self.spec.companies),
            "gaps": self.gaps,
            "companies": {
                issuer: {k: v for k, v in subject.items() if k != "documents"}
                for issuer, subject in self.subjects.items()
            },
            "quality_note": "任务完成率、JSON通过率、原文字段提取率与财务验证分别看待；null检查位/空附件不算提取失败。",
        }

    def final_key(self):
        assumptions = None
        if self.spec.assumptions:
            path = Path(self.spec.assumptions)
            try:
                assumptions = load(path)
            except ValueError:
                assumptions = path.read_text("utf-8")
        base_key = artifact_key(
            "research",
            sorted(self.subjects),
            {
                "versions": {
                    k: v["versions"] for k, v in sorted(self.subjects.items())
                },
                "instruments": sorted(self.spec.companies),
                "market": market_fingerprint(self.quotes),
                "focus": self.spec.focus.strip(),
                "assumptions": assumptions,
            },
        )
        # An explicit new study is a new authorization, not a replay of old POSTs.
        return (
            fingerprint({"research": base_key, "reinvestigation": self.run_id})
            if self.spec.reinvestigate
            else base_key
        )

    def evidence(self):
        self.phase("extraction")
        cache = self._parse_cache()
        previous_work = (
            load(self.spec.output / "annual_work.json")
            if (self.spec.output / "annual_work.json").is_file()
            else []
        )
        pending = set()
        if self.resuming:
            for annual in previous_work:
                if any(
                    (self.store.get_attempt(k) or {}).get("status", "not_sent")
                    == "not_sent"
                    for k in annual["jobs"]
                ):
                    pending.add((annual["issuer"], str(annual["year"])))
        work = list(previous_work)
        refresh_context = set()
        prepared = []
        for issuer, subject in self.subjects.items():
            versions = subject["versions"]
            context = usable_artifact(
                self.store, artifact_key("context", issuer, versions)
            )
            summary = usable_artifact(
                self.store, artifact_key("summary", issuer, versions)
            )
            state = self.company_artifacts.setdefault(
                issuer, {"context": context, "summary": summary, "annuals": []}
            )
            outstanding = any(item[0] == issuer for item in pending)
            if (summary or context) and not outstanding:
                self.reused += int(bool(summary)) + int(bool(context))
                self.event(
                    "company_cache_reused",
                    company=issuer,
                    summary=bool(summary),
                    context=bool(context),
                )
                continue
            if outstanding:
                refresh_context.add(issuer)
                state["context"] = None
                if summary:
                    self.gap(
                        "summary_retains_prior_partial_input",
                        issuer,
                        source_context=summary.get("metadata", {}).get(
                            "source_context"
                        ),
                        reason="previously_sent_summary_is_not_reissued",
                    )
            for year in versions:
                version = {year: versions[year]}
                annual = usable_artifact(
                    self.store, artifact_key("annual", issuer, version)
                )
                if annual and (issuer, year) not in pending:
                    state["annuals"].append(annual)
                    self.reused += 1
                    continue
                docs = [d for d in subject["documents"] if str(d.fiscal_year) == year]
                content, profiles = [], []
                try:
                    for doc in docs:
                        parsed = cache.get(doc.sha256) if doc.sha256 else None
                        if not parsed and not doc.local_path:
                            raise LocalStageError("annual_original_unavailable")
                        if not parsed:
                            parsed = cache.read(Path(doc.local_path), doc.content_type)
                        self.event(
                            "parse_cache_hit"
                            if parsed["cache_hit"]
                            else "document_parsed",
                            company=issuer,
                            year=int(year),
                            sha256=parsed["sha256"],
                        )
                        content.append(
                            {
                                "type": "text",
                                "text": "原件身份、真实期间及修订关系："
                                + json.dumps(
                                    {
                                        k: v
                                        for k, v in doc.to_dict().items()
                                        if k not in {"local_path", "provenance"}
                                    },
                                    ensure_ascii=False,
                                ),
                            }
                        )
                        content.extend(parsed["content"])
                        profiles.extend(doc.profiles)
                    profiles = list(dict.fromkeys(profiles))
                    if not profiles:
                        self.gap(
                            "industry_profile_unresolved_base_only",
                            issuer,
                            year=int(year),
                        )
                    code = Instrument.parse(subject["instruments"][0]).code
                    jobs = self.adapters.extraction_requests(
                        issuer, code, int(year), version, profiles, content
                    )
                    entry = {
                        "issuer": issuer,
                        "year": int(year),
                        "jobs": [job["key"] for job in jobs],
                    }
                    if entry not in work:
                        work.append(entry)
                    prepared.append(
                        {
                            "issuer": issuer,
                            "year": int(year),
                            "code": code,
                            "versions": version,
                            "jobs": jobs,
                            "parts": {},
                            "failures": [],
                        }
                    )
                except (LocalStageError, OSError, ValueError) as error:
                    self.gap(
                        "annual_preparation_failed",
                        issuer,
                        year=int(year),
                        error=str(error),
                    )
        atomic_write(self.spec.output / "annual_work.json", work)
        with self.dispatch_pool() as pool:
            futures = {}
            for annual in prepared:
                for job in annual["jobs"]:
                    cached = usable_artifact(self.store, job["key"])
                    if cached:
                        annual["parts"][job["block"]] = load(Path(cached["path"]))
                        annual["failures"].append(
                            {
                                "block": job["block"],
                                "status": cached["status"],
                                "reused": True,
                            }
                        )
                        continue
                    if self.stopped():
                        annual["failures"].append(
                            {
                                "block": job["block"],
                                "status": "not_sent",
                                "reason": "cancelled_or_paused",
                            }
                        )
                        continue
                    future = pool.submit(
                        self.adapters.call,
                        job["key"],
                        job["request"],
                        "extract",
                        annual["issuer"],
                        FutureHold(4 + len(self.subjects)),
                    )
                    futures[future] = (annual, job)
            for future in as_completed(futures):
                annual, job = futures[future]
                try:
                    receipt = future.result()
                    part, outcome = self.adapters.accept_extraction(
                        job, receipt, annual["issuer"], annual["versions"]
                    )
                    if part is not None:
                        annual["parts"][job["block"]] = part
                    annual["failures"].append(outcome)
                    if receipt.get("status") == "not_sent":
                        self.blocked.append(receipt.get("reason"))
                except (LocalStageError, OSError, ValueError) as error:
                    annual["failures"].append(
                        {"block": job["block"], "status": "failed", "error": str(error)}
                    )
                    self.gap(
                        "extraction_failed",
                        annual["issuer"],
                        year=annual["year"],
                        error=str(error),
                    )
        for annual in prepared:
            self.gaps = [
                gap
                for gap in self.gaps
                if not (
                    gap.get("kind") == "extraction_block_gap"
                    and gap.get("company") == annual["issuer"]
                    and gap.get("year") == annual["year"]
                )
            ]
            artifact = self.adapters.assemble_annual(
                annual["issuer"],
                annual["code"],
                annual["year"],
                annual["versions"],
                annual["parts"],
                annual["failures"],
            )
            self.company_artifacts[annual["issuer"]]["annuals"].append(artifact)
            for failure in annual["failures"]:
                if failure["status"] != "complete":
                    self.gap(
                        "extraction_block_gap",
                        annual["issuer"],
                        year=annual["year"],
                        **failure,
                    )
        self.phase("context")
        for issuer, state in self.company_artifacts.items():
            if state["context"] or (state["summary"] and issuer not in refresh_context):
                continue
            try:
                state["context"] = self.adapters.context(
                    issuer,
                    self.subjects[issuer]["versions"],
                    state["annuals"],
                    {
                        "issuer": issuer,
                        "versions": self.subjects[issuer]["versions"],
                        "gaps": [g for g in self.gaps if g["company"] == issuer],
                    },
                    force=issuer in refresh_context,
                )
            except (LocalStageError, OSError, ValueError) as error:
                self.gap("company_context_failed", issuer, error=str(error))

    def summaries(self):
        self.phase("summary")
        plans = {}
        with self.dispatch_pool() as pool:
            futures = {}
            for issuer, state in self.company_artifacts.items():
                if state["summary"]:
                    continue
                if not state["context"]:
                    self.gap("summary_not_generated", issuer)
                    continue
                summary_key = artifact_key(
                    "summary", issuer, self.subjects[issuer]["versions"]
                )
                prior = self.store.get_attempt(summary_key)
                if prior and prior["status"] not in {"queued", "sending", "not_sent"}:
                    receipt = {
                        **(prior.get("receipt") or {}),
                        "capture_dir": prior["capture_dir"],
                    }
                    try:
                        text = visible(receipt)
                    except LocalStageError as error:
                        text = ""
                        self.gap(
                            "prior_summary_content_unavailable",
                            issuer,
                            error=str(error),
                        )
                    if text:
                        state["summary"] = self.adapters.save_text_artifact(
                            summary_key,
                            "summary",
                            text,
                            "complete"
                            if prior["status"] == "complete"
                            and state["context"]["status"] == "complete"
                            else "partial",
                            {
                                "issuer": issuer,
                                "versions": self.subjects[issuer]["versions"],
                                "reused_prior_attempt": True,
                                "original_capture_dir": prior["capture_dir"],
                                "context_lineage": "retained_prior_request_not_relabelled_as_new_input",
                                "part_attempt_keys": [summary_key],
                            },
                        )
                    else:
                        self.gap(
                            "prior_summary_attempt_not_reissued",
                            issuer,
                            status=prior["status"],
                        )
                    continue
                try:
                    subject = self.subjects[issuer]
                    plan = self.adapters.summary_plan(
                        issuer,
                        subject["versions"],
                        state["context"],
                        {
                            "issuer": issuer,
                            "versions": subject["versions"],
                            "gaps": [g for g in self.gaps if g["company"] == issuer],
                        },
                    )
                    plan["receipts"] = [None] * len(plan["pieces"])
                    plans[issuer] = plan
                    for i, piece in enumerate(plan["pieces"]):
                        key = (
                            plan["key"]
                            if len(plan["pieces"]) == 1
                            else fingerprint({"summary": plan["key"], "part": i})
                        )
                        header = (
                            plan["header"]
                            + f"\n本部分{i + 1}/{len(plan['pieces'])}；局部分段不能推翻全局覆盖。\n"
                        )
                        future = pool.submit(
                            self.adapters.call,
                            key,
                            self.adapters.request(plan["system"], header + piece),
                            "summary_part",
                            issuer,
                            FutureHold(4 + int(len(plan["pieces"]) > 1)),
                        )
                        futures[future] = (issuer, i)
                except (LocalStageError, OSError, ValueError) as error:
                    self.gap("summary_preparation_failed", issuer, error=str(error))
            for future in as_completed(futures):
                issuer, index = futures[future]
                try:
                    receipt = future.result()
                    plans[issuer]["receipts"][index] = receipt
                    if receipt.get("status") == "not_sent":
                        self.blocked.append(receipt.get("reason"))
                except (LocalStageError, OSError, ValueError) as error:
                    plans[issuer]["receipts"][index] = {
                        "status": "failed",
                        "error": str(error),
                    }
                    self.gap("summary_part_failed", issuer, error=str(error))
        for issuer, plan in plans.items():
            receipts = plan["receipts"]
            statuses = [
                r.get("status", "not_sent") if r else "not_sent" for r in receipts
            ]
            if len(receipts) == 1:
                text = visible(receipts[0] or {})
                final_receipt = receipts[0] or {}
            else:
                material = plan["header"]
                for i, receipt in enumerate(receipts):
                    material += f"\n\n# 摘要部分{i + 1}，状态={statuses[i]}\n" + (
                        visible(receipt or {})
                        or "本段未生成，属于执行缺口，不是源年报未提供。"
                    )
                if not any(visible(r or {}) for r in receipts):
                    self.gap("summary_no_visible_parts", issuer)
                    continue
                try:
                    final_receipt = self.adapters.call(
                        plan["key"],
                        self.adapters.request(
                            plan["system"],
                            material
                            + "\n整合完整七问，保留上述全局年度及分段缺口；不输入当前行情。",
                        ),
                        "summary_merge",
                        issuer,
                        FutureHold(4),
                    )
                except Exception as error:  # noqa: BLE001 - isolate optional IO or top-level failure
                    final_receipt = {"status": "failed", "error": str(error)}
                text = visible(final_receipt)
                if not text:
                    # This is an explicit partial concatenation, never a claimed model merge.
                    text = (
                        "> 整合摘要未生成；下列仅为已有分段原文，未补写。\n" + material
                    )
                    statuses.append("merge_not_generated")
            state = (
                "complete"
                if final_receipt.get("status") == "complete"
                and all(s == "complete" for s in statuses)
                and not plan["source_partial"]
                else "partial"
            )
            artifact = self.adapters.save_text_artifact(
                plan["key"],
                "summary",
                text,
                state,
                {
                    "issuer": issuer,
                    "versions": self.subjects[issuer]["versions"],
                    "part_statuses": statuses,
                    "legacy_price_dependency": False,
                    "source_context": plan["context"]["path"],
                    "part_attempt_keys": [
                        plan["key"]
                        if len(plan["pieces"]) == 1
                        else fingerprint({"summary": plan["key"], "part": i})
                        for i in range(len(plan["pieces"]))
                    ],
                    "merge_attempt_key": plan["key"]
                    if len(plan["pieces"]) > 1
                    else None,
                },
            )
            self.company_artifacts[issuer]["summary"] = artifact
            if state != "complete":
                self.gap("summary_partial", issuer, part_statuses=statuses)

    def peer(self):
        self.phase("peer")
        versions = {k: v["versions"] for k, v in sorted(self.subjects.items())}
        key = artifact_key("peer", sorted(self.subjects), versions)
        cached = usable_artifact(self.store, key)
        if cached:
            self.reused += 1
            return cached
        sections = [
            "# 请求公司与年报覆盖（公司不能因缺摘要静默消失）\n"
            + json.dumps(self.coverage(), ensure_ascii=False)
        ]
        available = 0
        for issuer, state in self.company_artifacts.items():
            summary, context = state.get("summary"), state.get("context")
            sections.append("# 公司 " + issuer)
            if summary:
                available += 1
                sections.append(
                    "摘要状态与来源："
                    + json.dumps(summary["metadata"], ensure_ascii=False)
                    + "\n"
                    + Path(summary["path"]).read_text("utf-8")
                )
            else:
                sections.append(
                    "摘要未生成。保留该公司及已有资料身份，不能伪称完成研究。"
                )
            if context:
                paths = context.get("metadata", {}).get("source_data", [])
                metadata_path = context.get("metadata", {}).get("metadata_path")
                metadata_sha = context.get("metadata", {}).get("metadata_sha256")
                if (
                    metadata_path
                    and metadata_sha
                    and Path(metadata_path).is_file()
                    and file_sha(Path(metadata_path)) == metadata_sha
                ):
                    sections.append(
                        "原年度业务/分部/口径：\n"
                        + Path(metadata_path).read_text("utf-8")
                    )
                else:
                    self.gap("peer_metadata_sidecar_unavailable_or_unverified", issuer)
                    for path in paths:
                        try:
                            expected = (
                                context.get("metadata", {})
                                .get("source_data_sha256", {})
                                .get(path)
                            )
                            if not expected:
                                registered = next(
                                    (
                                        item
                                        for item in self.store.list_artifacts("annual")
                                        if item["path"] == path
                                    ),
                                    None,
                                )
                                expected = registered["sha256"] if registered else None
                            if not expected or file_sha(Path(path)) != expected:
                                raise LocalStageError(
                                    "peer_annual_metadata_integrity_unverified"
                                )
                            data = load(Path(path))
                            sections.append(
                                json.dumps(
                                    {
                                        k: data.get(k)
                                        for k in (
                                            "company",
                                            "reports",
                                            "metadata",
                                            "policies",
                                            "issues",
                                        )
                                    },
                                    ensure_ascii=False,
                                )
                            )
                        except (OSError, ValueError):
                            self.gap(
                                "peer_metadata_unavailable", issuer, path=str(path)
                            )
        if not available:
            return None
        body = "\n\n---\n\n".join(sections)
        atomic_write(
            self.spec.output / "stage3_input/模型输入.md",
            body,
            exclusive=not (self.spec.output / "stage3_input/模型输入.md").exists(),
        )
        system = (self.spec.repo / "config/prompts/peer_research.md").read_text("utf-8")
        system += "\n本轮横评是年报分析资产，不使用现价，不回答基于旧价格的当前买入回报。请求公司清单始终保留，先划真实经济可比范围，不强行同榜。分段执行缺口不是年报缺失；旧摘要中的行情只作明确历史时点参考。"
        if len(self.subjects) == 1:
            return self.adapters.save_text_artifact(
                key,
                "peer",
                body,
                "partial" if self.gaps else "complete",
                {"versions": versions, "single_issuer_handoff": True, "api_calls": 0},
            )
        receipt = self.adapters.call(
            key, self.adapters.request(system, body), "peer", hold_usd=FutureHold(3)
        )
        if receipt.get("status") == "not_sent":
            self.blocked.append(receipt.get("reason"))
        return self.adapters.save_text_artifact(
            key,
            "peer",
            visible(receipt),
            "complete"
            if receipt.get("status") == "complete" and not self.gaps
            else "partial",
            {
                "versions": versions,
                "requested_issuers": sorted(self.subjects),
                "legacy_price_dependency": False,
            },
        )

    def research(self, peer, key):
        from .investigation import (
            investigate,
            model_charts,
            review_requests_rework,
            verify_bundle,
        )

        versions = {
            issuer: subject["versions"] for issuer, subject in self.subjects.items()
        }
        for reference in self.store.list_artifacts("legacy_reference"):
            metadata = reference.get("metadata", {})
            if (
                not self.spec.reinvestigate
                and metadata.get("scope_unresolved")
                and metadata.get("versions") == versions
                and usable_artifact(self.store, reference["key"])
            ):
                self.final_artifacts.append(reference)
                self.gap(
                    "legacy_research_identity_unresolved_no_duplicate_charge",
                    path=reference["path"],
                    reason="register_prior_market_focus_assumptions_before_new_research",
                )
                self.phase("research_scope_unresolved")
                return None
        self.phase("research")
        base = "# 本轮明确研究重点\n" + self.spec.focus
        base += (
            "\n\n# 本轮冻结行情（原时间、原币种，缺少换算则限制结论）\n"
            + json.dumps(self.quotes, ensure_ascii=False)
        )
        base += "\n\n# 请求公司及权威年报覆盖\n" + json.dumps(
            self.coverage(), ensure_ascii=False
        )
        base += (
            "\n\n# 横评原文与状态\n"
            + json.dumps(peer["metadata"], ensure_ascii=False)
            + "\n"
            + Path(peer["path"]).read_text("utf-8")
        )
        for issuer, state in self.company_artifacts.items():
            if state.get("summary"):
                summary = state["summary"]
                base += f"\n\n# {issuer}完整单公司摘要（{summary['status']}）\n" + Path(
                    summary["path"]
                ).read_text("utf-8")
        if self.spec.assumptions:
            base += "\n\n# 明确估值假设\n" + Path(self.spec.assumptions).read_text(
                "utf-8"
            )
        atomic_write(
            self.spec.output / "research_inputs.md",
            base,
            exclusive=not (self.spec.output / "research_inputs.md").exists(),
        )
        investigation = investigate(self, key, base)
        self.final_artifacts.append(investigation)
        manifest = verify_bundle(Path(investigation["metadata"]["bundle"]))
        if not manifest.get("selected_model"):
            self.gap(
                "investigation_model_unavailable_no_writing",
                model_status=manifest.get("model_status"),
                error=manifest.get("error", ""),
            )
            self.phase("investigation_partial")
            return None
        investigation_versions = [investigation]
        investigation_base = base
        base += "\n\n" + Path(investigation["path"]).read_text("utf-8")
        previous, pictures, statuses, final = "", {}, [], None
        rework_issue = None
        for index, phase in enumerate(("draft", "review", "write")):
            if self.stopped():
                break
            self.store.update_run(self.run_id, phase="research_" + phase)
            for version in investigation_versions:
                verify_bundle(Path(version["metadata"]["bundle"]))
            request = self.adapters.research_request(index, base, previous, pictures)
            task = fingerprint({"research": key, "round": phase})
            receipt = self.adapters.call(
                task, request, "research_" + phase, hold_usd=FutureHold(2 - index)
            )
            status = receipt.get("status", "failed")
            statuses.append(status)
            text = visible(receipt)
            if status == "not_sent":
                self.blocked.append(receipt.get("reason"))
            if not text:
                break
            previous = text
            final = (text, phase)
            if index == 0:
                pictures = self.adapters.render_charts(
                    model_charts(investigation_versions[-1]),
                    self.spec.output / "research/views" / phase,
                    status,
                )
                # The same frozen computation needs one set of figures. Only a
                # model rework below replaces it with newly calculated data.
            if phase == "review":
                rework = review_requests_rework(text)
                if rework and rework.get("error"):
                    rework_issue = rework["error"]
                    base += "\n审阅返工标记无效，不能声称模型问题已解决：" + rework_issue
                elif rework and rework.get("required") and not self.stopped():
                    revision = investigate(
                        self,
                        key,
                        investigation_base,
                        revision="review_rework",
                        feedback="\n".join(rework["issues"]),
                        parent=investigation["metadata"]["bundle"],
                    )
                    investigation_versions.append(revision)
                    self.final_artifacts.append(revision)
                    revised = verify_bundle(Path(revision["metadata"]["bundle"]))
                    if not revised.get("selected_model"):
                        rework_issue = "rework_model_unavailable_no_final_write"
                        self.gap(rework_issue, error=revised.get("error", ""))
                        break
                    base = investigation_base + "\n\n" + Path(
                        revision["path"]
                    ).read_text("utf-8")
                    base += "\n本轮为审阅后重算。旧稿与旧图片只说明前一模型；定稿必须以本次计算结果为准，不声称第二轮已审阅本次结果。"
                    # Rebuild model-authored chart data, not a fourth writing call.
                    pictures = self.adapters.render_charts(
                        model_charts(revision),
                        self.spec.output / "research/views/rework",
                        revision["status"],
                    )
        if final is None:
            return None
        status = (
            "complete"
            if len(statuses) == 3
            and all(s == "complete" for s in statuses)
            and not self.gaps
            and peer["status"] == "complete"
            and investigation_versions[-1]["status"] == "complete"
            and not rework_issue
            else "partial"
        )
        artifact = self.adapters.save_text_artifact(
            key,
            "research",
            final[0],
            status,
            {
                "round_statuses": statuses,
                "delivered_round": final[1],
                "versions": {k: v["versions"] for k, v in self.subjects.items()},
                "market_fingerprint": market_fingerprint(self.quotes),
                "peer": peer["path"],
                "source_gaps": self.gaps,
                "investigation_versions": investigation_versions,
                "reinvestigate": self.spec.reinvestigate,
                "rework_issue": rework_issue,
            },
        )
        self.adapters.finalize_images(final[0], pictures, Path(artifact["path"]).parent)
        return artifact

    def deliver(self, artifact=None):
        from .audit import audit_run, hash_manifest

        delivery = self.spec.output / "delivery"
        artifacts = []
        for issuer, state in self.company_artifacts.items():
            for year, versions in self.subjects[issuer]["versions"].items():
                annual = usable_artifact(
                    self.store, artifact_key("annual", issuer, {year: versions})
                )
                if annual:
                    artifacts.append({"company": issuer, "kind": "annual", **annual})
            for kind in ("context", "summary"):
                item = state.get(kind)
                if item:
                    artifacts.append({"company": issuer, "kind": kind, **item})
        artifacts.extend(self.final_artifacts)
        if artifact and not any(a.get("key") == artifact["key"] for a in artifacts):
            artifacts.append(artifact)
        links = []
        for index, item in enumerate(artifacts):
            source = Path(item["path"])
            target = (
                delivery
                / "files"
                / f"{index + 1}_{file_sha(source)[:12]}_{source.name}"
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copyfile(source, target)
            for image in (source.parent / "assets").glob("*.png"):
                destination = target.parent / "assets" / image.name
                try:
                    if not destination.exists():
                        shutil.copyfile(image, destination)
                except OSError as error:
                    self.event(
                        "presentation_failed",
                        phase="delivery_image",
                        path=str(image),
                        error=str(error),
                    )
            links.append(
                {
                    "kind": item.get("kind", item.get("stage")),
                    "key": item["key"],
                    "company": item.get("company"),
                    "status": item["status"],
                    "source": str(source),
                    "path": str(target),
                    "sha256": file_sha(target),
                    "metadata": item.get("metadata", {}),
                }
            )
            if item.get("stage", item.get("kind")) == "investigation":
                from .investigation import reproducibility_files, verify_bundle

                manifest_path = Path(item["metadata"]["bundle"])
                if file_sha(manifest_path) != item["metadata"]["bundle_sha256"]:
                    raise ValueError("investigation_bundle_integrity_changed")
                bundle = verify_bundle(manifest_path)
                bundle_dir = delivery / "investigation" / item["key"]
                for name, digest in bundle["files"].items():
                    dest = bundle_dir / name
                    if not dest.exists():
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(manifest_path.parent / name, dest)
                    if file_sha(dest) != digest:
                        raise ValueError("investigation_delivery_integrity_changed")
                atomic_write(bundle_dir / "bundle.json", manifest_path.read_bytes())
                links[-1]["bundle_path"] = str(bundle_dir / "bundle.json")
                _, public_manifest = reproducibility_files(bundle_dir / "bundle.json")
                atomic_write(bundle_dir / "reproducibility.json", public_manifest)
                links[-1]["reproducibility_path"] = str(bundle_dir / "reproducibility.json")
            page = Path(
                item.get("metadata", {}).get("reading_html")
                or source.with_suffix(".html")
            )
            if page.is_file():
                destination = target.with_suffix(".html")
                try:
                    if not destination.exists():
                        shutil.copyfile(page, destination)
                    links[-1]["html_path"] = str(destination)
                except OSError as error:
                    self.event(
                        "presentation_failed",
                        phase="delivery_html",
                        path=str(page),
                        error=str(error),
                    )
        result = {
            "id": self.run_id,
            "artifacts": links,
            "source_coverage": self.coverage(),
            "budget": self.store.budget(self.run_id),
            "reused_artifacts": self.reused,
            "blocked_reasons": sorted({x for x in self.blocked if x}),
            "model_complete_is_financial_verification": False,
        }
        accounting = audit_run(self.store, self.run_id, links)
        atomic_write(delivery / "accounting.json", accounting)
        result["accounting_path"] = str(delivery / "accounting.json")
        atomic_write(delivery / "manifest.json", result)
        lines = ["# 研究完整交付", "执行状态、缓存复用和财务验证分别看待。"]
        lines.append(
            "[逐公司/阶段 usage、未知项与本地正文 token](accounting.json) · [文件哈希清单](hashes.json)"
        )
        for item in links:
            row = f"- [{item.get('company') or ''} {item['kind']} · {item['status']}](files/{Path(item['path']).name})"
            if item.get("html_path"):
                row += f" · [阅读页](files/{Path(item['html_path']).name})"
            if item.get("bundle_path"):
                row += f" · [可复算模型与来源清单](investigation/{item['key']}/reproducibility.json)"
            lines.append(row)
        lines += [
            "\n## 费用与未完成原因",
            "```json\n"
            + json.dumps(
                {
                    "budget": result["budget"],
                    "blocked": result["blocked_reasons"],
                    "coverage": self.coverage(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n```",
        ]
        atomic_write(delivery / "交付说明.md", "\n\n".join(lines))
        try:
            from scripts.research_render import html

            atomic_write(delivery / "index.html", html("\n\n".join(lines)))
        except Exception as error:  # noqa: BLE001 - isolate optional IO or top-level failure
            self.event("presentation_failed", phase="delivery", error=str(error))
        atomic_write(
            delivery / "hashes.json",
            hash_manifest(delivery, root=delivery, exclude=[delivery / "hashes.json"]),
        )
        self.store.update_run(
            self.run_id,
            artifacts=links,
            gaps=self.gaps,
            blocked_reasons=result["blocked_reasons"],
            reused_artifacts=self.reused,
        )
        return result

    def execute(self):
        process = psutil.Process()
        if not self.store.claim_run(self.run_id, process.pid, process.create_time()):
            raise ValueError("research_run_already_owned_or_requires_resume")
        self.store.update_run(
            self.run_id,
            state="running",
            requires_resume=False,
            owner_pid=process.pid,
            owner_birth=process.create_time(),
            heartbeat_at=now(),
        )
        self.publisher.start()
        self.event("run_started", simulated=self.executor.post is not None)
        artifact = None
        try:
            self.discover()
            key = self.final_key()
            artifact = usable_artifact(self.store, key)
            if artifact and not self.resuming:
                self.reused += 1
                self.event("research_cache_reused", artifact_key=key)
                self.final_artifacts.extend(
                    artifact.get("metadata", {}).get("investigation_versions", [])
                )
                # Include existing upstream artifacts without invoking model/tokenizer.
                for issuer, subject in self.subjects.items():
                    self.company_artifacts[issuer] = {
                        kind: usable_artifact(
                            self.store, artifact_key(kind, issuer, subject["versions"])
                        )
                        for kind in ("context", "summary")
                    }
            elif (
                any(subject["documents"] for subject in self.subjects.values())
                and not self.stopped()
            ):
                self.model_work_needed = True
                self.adapters = StageAdapters(
                    self.spec.repo,
                    self.store,
                    self.run_id,
                    self.executor,
                    self.spec.output,
                    self.event,
                )
                self.evidence()
                self.summaries()
                peer = self.peer()
                if peer:
                    self.final_artifacts.append(peer)
                    artifact = usable_artifact(self.store, key) or self.research(
                        peer, key
                    )
                else:
                    self.gap("no_usable_summary_evidence_no_financial_request")
            result = self.deliver(artifact)
            current = self.store.get_run(self.run_id)
            state = (
                "cancelled"
                if current.get("cancel_requested")
                else current["state"]
                if current.get("requires_resume")
                else "budget_stop"
                if "budget_stop" in self.blocked
                else "complete"
                if artifact and artifact["status"] == "complete" and not self.gaps
                else "partial"
            )
            self.store.update_run(
                self.run_id,
                state=state,
                phase="finished",
                owner_pid=None,
                owner_birth=None,
            )
            self.event("run_finished", state=state, artifacts=len(result["artifacts"]))
            return {**result, "state": state}
        except Exception as error:
            reason = (
                "integrity_error"
                if "integrity" in str(error) or "freeze" in str(error)
                else "resource_blocked"
                if isinstance(error, OSError) and error.errno == 28
                else "program_error"
            )
            try:
                self.store.update_run(
                    self.run_id,
                    state="failed",
                    phase="failed",
                    error=str(error),
                    owner_pid=None,
                    owner_birth=None,
                    requires_resume=True,
                    pause_reason=reason,
                )
                self.event(
                    "run_failed", error=str(error), error_type=type(error).__name__
                )
            except Exception as persistence_error:
                self.publisher.emergency(persistence_error, spec=self.spec)
                raise error from persistence_error
            # The host owns tools, budget and actual test receipts. No candidate
            # application or replay is authorised by this diagnostic handoff.
            if (
                self.spec.origin.get("channel") == "feishu"
                and self.model_work_needed
                and reason == "program_error"
            ):
                try:
                    from .repair import RepairManager

                    manager = RepairManager(
                        self.store, self.run_id, self.spec.repo, executor=self.executor
                    )
                    incident = manager.diagnose(
                        str(error),
                        {
                            "phase": self.store.get_run(self.run_id).get("phase"),
                            "error_type": type(error).__name__,
                        },
                    )
                    if self.executor.post is None:
                        manager.run_agent(incident["id"])
                except Exception as repair_error:  # noqa: BLE001 - preserve original failure
                    self.event(
                        "repair_failed",
                        error=str(repair_error),
                        original_error_type=type(error).__name__,
                    )
            raise
        finally:
            try:
                self.executor.close()
            finally:
                self.publisher.close()


def _execute_existing(
    run_id: str,
    store_root: Path,
    *,
    source=None,
    post=None,
    key=None,
    rates=None,
    account_reader=None,
    sender=None,
    resuming=False,
):
    store = Store(store_root)
    run = store.get_run(run_id)
    root = _verified_root(store, run)
    spec = ResearchSpec.from_dict(run["spec"])
    spec.repo = root
    if sender is None and spec.origin:
        from .observability import FeishuOriginSender

        sender = FeishuOriginSender(spec.credential_root, store)
    return Pipeline(
        spec,
        store,
        run_id,
        source=source,
        post=post,
        key=key,
        rates=rates,
        account_reader=account_reader,
        sender=sender,
        resuming=resuming,
    ).execute()


def _verified_root(store, run):
    try:
        return _verify_runtime(run)
    except (ValueError, OSError, KeyError) as error:
        store.update_run(
            run["id"],
            state="paused",
            requires_resume=True,
            pause_reason="integrity_error",
            error=str(error),
        )
        store.event(
            run["id"], "integrity_blocked", error=str(error), new_POSTs_allowed=False
        )
        raise


def run_research(spec: ResearchSpec, **kwargs) -> dict:
    if not spec.paid:
        return plan_research(spec)
    store, run = _new_run(spec, freeze=True)
    if not kwargs:
        _launch(store, run)
        return wait_research(run["id"], store_root=store.root)
    return _execute_existing(run["id"], store.root, **kwargs)


def wait_research(run_id: str, *, store_root: Path, event_callback=None) -> dict:
    """Wait for the independent host, using the same bounded persisted events."""
    store, cursor = Store(store_root), 0
    while True:
        run = store.get_run(run_id)
        for event in store.events(run_id, after=cursor, limit=100):
            cursor = event["seq"]
            if event_callback:
                event_callback(event)
        host = run.get("host_launch") or {}
        alive = False
        if host:
            try:
                process = psutil.Process(host["pid"])
                alive = (
                    process.is_running()
                    and process.status() != psutil.STATUS_ZOMBIE
                    and (
                        host.get("create_time") is None
                        or abs(process.create_time() - host["create_time"]) < 0.01
                    )
                )
            except psutil.NoSuchProcess:
                pass
        if not alive:
            store.reconcile()
            run = store.get_run(run_id)
            if run["state"] in {"created", "running", "waiting_balance"}:
                run = store.update_run(
                    run_id,
                    state="paused",
                    requires_resume=True,
                    pause_reason="host_error",
                    error="host_exited_without_terminal_state",
                )
                store.event(
                    run_id,
                    "worker_lost",
                    cause="host_exited_without_terminal_state",
                    automatic_restart=False,
                )
            manifest = Path(run["spec"]["output"]) / "delivery/manifest.json"
            return {
                **(
                    load(manifest)
                    if manifest.is_file()
                    else get_status(run_id, store_root=store.root)
                ),
                "state": run["state"],
            }
        time.sleep(0.5)


def _launch(store: Store, run: dict, *, resuming=False):
    root = _verified_root(store, run)
    output = Path(run["spec"]["output"])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    command = [
        sys.executable,
        "-B",
        "-X",
        "utf8",
        str(root / "scripts/financial_pipeline.py"),
        "_host",
        "--source",
        str(output),
    ]
    if resuming:
        command.append("--resuming")
    kwargs = (
        {"creationflags": subprocess.CREATE_NO_WINDOW}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    with (output / f"stdout_{stamp}.log").open("xb") as stdout, (
        output / f"stderr_{stamp}.log"
    ).open("xb") as stderr:
        try:
            process = subprocess.Popen(
                command,
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                **kwargs,
            )
        except OSError as error:
            store.update_run(
                run["id"],
                state="failed",
                requires_resume=True,
                pause_reason="host_error",
                error=str(error),
            )
            store.event(
                run["id"],
                "run_failed",
                cause="background_launch_failed",
                error=str(error),
            )
            raise ValueError(
                "background_launch_failed: " + run["id"] + ": " + str(error)
            ) from error
    try:
        birth = psutil.Process(process.pid).create_time()
    except psutil.NoSuchProcess:
        birth = None
    launch = {
        "pid": process.pid,
        "create_time": birth,
        "command": command,
        "started_at": now(),
        "automatic_restart": False,
    }
    atomic_write(output / (f"launch_{stamp}.json"), launch, exclusive=True)
    store.update_run(run["id"], host_launch=launch)
    return {**store.get_run(run["id"]), "output": str(output)}


def start_research(spec: ResearchSpec, background=True, **kwargs) -> dict:
    if not background or not spec.paid:
        return run_research(spec, **kwargs)
    if kwargs:
        raise ValueError("injected_io_boundaries_are_foreground_test_only")
    store, run = _new_run(spec, freeze=True)
    ProgressPublisher(store, run["id"], spec.output).publish()
    return _launch(store, run)


def get_status(run_id: str, *, store_root: Path) -> dict:
    return progress_snapshot(Store(store_root), run_id)


def cancel_research(run_id: str, *, store_root: Path) -> dict:
    store = Store(store_root)
    store.update_run(
        run_id, cancel_requested=True, requires_resume=True, state="cancelled"
    )
    store.event(run_id, "cancel_requested")
    return store.get_run(run_id)


def resume_research(
    run_id: str, *, store_root: Path, background=True, **kwargs
) -> dict:
    store = Store(store_root)
    run = store.get_run(run_id)
    for launch in (run.get("host_launch") or {}, run.get("launch") or {}):
        if not launch:
            continue
        try:
            if (
                launch.get("create_time") is None
                or abs(
                    psutil.Process(launch["pid"]).create_time() - launch["create_time"]
                )
                < 0.01
            ):
                raise ValueError("research_worker_still_alive")
        except psutil.NoSuchProcess:
            pass
    if run.get("state") == "awaiting_approval" and not run.get("execution_revision"):
        raise ValueError("repair_confirmation_required")
    _verified_root(store, run)
    store.update_run(
        run_id, requires_resume=False, cancel_requested=False, state="created"
    )
    store.event(run_id, "resume_authorized", original_budget_unchanged=True)
    if background or not kwargs:
        launched = _launch(store, store.get_run(run_id), resuming=True)
        if not background:
            return wait_research(run_id, store_root=store.root)
        return launched
    return _execute_existing(run_id, store.root, resuming=True, **kwargs)
