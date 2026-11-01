"""Read-only import of declared historical research evidence, never runners."""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

from .common import annual_versions, artifact_key, file_sha, fingerprint, load
from .models import AnnualDocument, Instrument
from .parsing import ParsedCache
from .sources import document_from_record, register_document


class LegacyImporter:
    def __init__(self, store, roots, *, spec=None):
        self.store = store
        self.roots = tuple(Path(p).resolve() for p in roots)
        self.spec = spec
        self.root = Path(store.root)
        self.parse_root = self.root / "parsed"
        self.parse_authority = None
        if self.spec is not None:
            from src.platform_composition import authority_for

            settings = load(self.spec.config) if self.spec.config and Path(self.spec.config).is_file() else {}
            authority = authority_for(self.spec.repo, settings, explicit_root=getattr(self.spec, "authority_root", None), isolated=getattr(self.spec, "authority_isolated", False))
            self.parse_authority = authority
            self.parse_root = ParsedCache.bind_root(self.parse_root, authority.materials_root / "parsed")
        self._documents: list[AnnualDocument] = []
        self._by_report = {}
        self._by_data_path = {}
        self._metadata_files = None
        self._report = {
            "documents": 0,
            "artifacts": 0,
            "attempts": 0,
            "source_readonly": True,
            "missing_originals": [],
            "parsed_imports": 0,
            "unassigned": [],
            "errors": [],
        }

    def _error(self, path, error):
        self._report["errors"].append({"path": str(path), "error": str(error)})

    def _manifests(self):
        return [
            path
            for path in self._discover_metadata()
            if path.name in {"sources.json", "plan.json"}
        ]

    def _discover_metadata(self):
        """Find semantic archive boundaries, never company IDs from folder names."""
        if self._metadata_files is not None:
            return self._metadata_files
        result = set()
        names = {
            "sources.json",
            "plan.json",
            "results.json",
            "summary_results.json",
            "summary.json",
            "manifest.json",
            "peer_request.json",
        }
        for root in self.roots:
            if root.is_file():
                result.add(root)
            elif root.is_dir():
                for directory, children, files in os.walk(root, followlinks=False):
                    folder = Path(directory).resolve()
                    # Explicit stores, frozen runtimes and new unified runs have
                    # their own registries; their copied fixtures are not archives.
                    if (
                        folder == self.root
                        or folder.is_relative_to(self.root)
                        or "research.sqlite3" in files
                        or "runtime_manifest.json" in files
                        or ("spec.json" in files and "run.json" in files)
                    ):
                        children[:] = []
                        continue
                    children[:] = [
                        name
                        for name in children
                        if name not in {"__pycache__", ".pytest_cache", ".git"}
                    ]
                    result.update(folder / name for name in files if name in names)
        self._metadata_files = sorted(result)
        return self._metadata_files

    def _batch_roots(self):
        roots = {root if root.is_dir() else root.parent for root in self.roots}
        for path in self._discover_metadata():
            if path.name in {
                "sources.json",
                "plan.json",
                "results.json",
                "summary_results.json",
                "peer_request.json",
            }:
                roots.add(path.parent)
            if path.name == "plan.json" and path.parent.name == "research":
                roots.add(path.parent.parent)
            if path.name == "manifest.json" and path.parent.name == "peer_input":
                roots.add(path.parent.parent)
            if path.name == "summary.json" and path.parent.parent.name == "contexts":
                roots.add(path.parent.parent.parent)
        return sorted(roots)

    def _record_spec(self, record):
        if self.spec is not None:
            return self.spec
        identity = record.get("instrument")
        code = str(record.get("code", ""))
        if not identity and record.get("market"):
            identity = f"{record['market']}:{code}"
        if not identity and re.fullmatch(r"\d{6}", code):
            # The historical V2 sources.json convention explicitly uses A-share
            # six-digit codes; never infer identity from a directory or PDF name.
            identity = "a_share:" + code
        if not identity:
            raise ValueError("legacy_market_identity_not_declared")
        return SimpleNamespace(
            companies=(Instrument.parse(identity).key,), issuer_map={}, profiles={}
        )

    def _scan_documents(self):
        seen = set()
        for manifest in self._manifests():
            try:
                value = load(manifest)
                records = (
                    value
                    if isinstance(value, list)
                    else value.get("records", value.get("documents", []))
                )
                if not isinstance(records, list):
                    raise TypeError("legacy_document_records_not_a_list")
            except (OSError, ValueError, TypeError) as error:
                self._error(manifest, error)
                continue
            for record in records:
                try:
                    document = document_from_record(
                        record, manifest.parent, self._record_spec(record)
                    )
                    if not re.fullmatch(r"[0-9a-f]{64}", document.sha256):
                        raise ValueError("legacy_original_identity_unverifiable")
                    if (
                        not document.local_path
                        or not Path(document.local_path).is_file()
                    ):
                        document.provenance["original_available"] = False
                        document.provenance["identity_basis"] = (
                            "declared_legacy_sha256_original_unavailable"
                        )
                        self._report["missing_originals"].append(document.to_dict())
                    identity = (
                        document.instrument,
                        document.fiscal_year,
                        document.sha256,
                    )
                    if identity not in seen:
                        seen.add(identity)
                        self._documents.append(document)
                        register_document(self.root, document)
                    code = Instrument.parse(document.instrument).code
                    report = f"{code}_{document.fiscal_year}"
                    self._by_report[(manifest.parent, report)] = document
                    try:
                        self._import_parse(manifest.parent, record, document, report)
                    except (OSError, ValueError, TypeError, KeyError) as error:
                        self._error(manifest, "parse_import_gap:" + str(error))
                    candidates = [
                        manifest.parent / "stage1" / report / "data.json",
                        manifest.parent
                        / "annuals"
                        / report
                        / "stage1"
                        / report
                        / "data.json",
                    ]
                    if record.get("stage1_source"):
                        source = Path(record["stage1_source"])
                        source = (
                            source if source.is_absolute() else manifest.parent / source
                        )
                        candidates.insert(0, source / "data.json")
                    for path in candidates:
                        if path.is_file():
                            self._by_data_path[path.resolve()] = document
                            self._import_annual(path, document)
                except (OSError, ValueError, TypeError, KeyError) as error:
                    self._report["unassigned"].append(
                        {
                            "manifest": str(manifest),
                            "record": record,
                            "reason": str(error),
                        }
                    )

    def _put(self, key, stage, path, status="complete", metadata=None):
        if self.store.get_artifact(key) is None:
            self.store.put_artifact(
                key,
                stage,
                path,
                status=status,
                metadata={"legacy": True, **(metadata or {})},
            )
            self._report["artifacts"] += 1

    def _import_annual(self, path, document):
        data = load(path)
        if not isinstance(data, dict):
            raise TypeError("legacy_annual_json_object_required")
        versions = annual_versions([document])
        self._put(
            artifact_key("annual", document.issuer_id, versions),
            "annual",
            path,
            status="partial" if data.get("unstructured_blocks") else "complete",
            metadata={
                "document": document.to_dict(),
                "documents": [document.to_dict()],
                "versions": versions,
                "source_data": str(path.resolve()),
            },
        )
        blocks = path.parent / "blocks"
        for block in blocks.iterdir() if blocks.is_dir() else []:
            if not block.is_dir() or not (block / "attempt.json").is_file():
                continue
            receipt = (
                load(block / "receipt.json")
                if (block / "receipt.json").is_file()
                else {"status": "unknown"}
            )
            key = artifact_key("extract:" + block.name, document.issuer_id, versions)
            self._attempt(
                key,
                {
                    "path": str(block.resolve()),
                    "receipt": receipt,
                    "block": block.name,
                    "document": document.to_dict(),
                },
            )
            parsed = next(
                (
                    block / name
                    for name in ["parsed.json", "data.json"]
                    if (block / name).is_file()
                ),
                None,
            )
            if parsed and receipt.get("status") in {"complete", "completed"}:
                self._put(
                    key,
                    "extract:" + block.name,
                    parsed,
                    metadata={"document": document.to_dict(), "versions": versions},
                )

    def _import_parse(self, folder, record, document, report):
        prepared = record.get("source")
        candidates = [folder / "prepared" / report / "source_content.json"]
        if prepared:
            path = Path(prepared)
            candidates.insert(0, path if path.is_absolute() else folder / path)
        pages = record.get("pages") or record.get("pdf_pages")
        cache = ParsedCache(self.parse_root, authority=self.parse_authority)
        for path in candidates:
            if path.is_file() and path.name == "source_content.json" and pages:
                cache.import_prepared(
                    document.sha256, load(path), int(pages), str(path)
                )
                self._report["parsed_imports"] += 1
                return
        # Captured V2 requests already contain exact text and thin-page images.
        blocks = folder / "stage1" / report / "blocks"
        summary = blocks.parent / "summary.json"
        if summary.is_file():
            pages = load(summary).get("pdf_pages")
        if pages:
            request = next(iter(sorted(blocks.glob("*/request.json"))), None)
            if request:
                content = load(request)["messages"][1]["content"]
                cache.import_prepared(
                    document.sha256, content, int(pages), str(request)
                )
                self._report["parsed_imports"] += 1

    def _attempt(self, key, record):
        existing = self.store.get_attempt(key)
        if existing is not None:

            def capture(value):
                path = value.get("capture_dir") or value.get("path")
                return str(Path(path).resolve()) if path else ""

            if capture(existing) == capture(record):
                return
            # One logical block may have multiple historical paid producers.
            # Preserve all real receipts without changing the dispatch identity.
            record = {"logical_key": key, **record}
            key = fingerprint({"legacy_attempt": capture(record), "logical_key": key})
        if self.store.get_attempt(key) is None:
            self.store.import_attempt(
                key, {"historical": True, "bill_to_current_run": False, **record}
            )
            self._report["attempts"] += 1

    def _root_documents(self, root):
        result = {}
        for (folder, _), document in self._by_report.items():
            if folder == root or folder.is_relative_to(root):
                result[(document.instrument, document.fiscal_year, document.sha256)] = (
                    document
                )
        return list(result.values())

    def _find_doc(self, root, record):
        code = str(record.get("code", ""))
        declared = record.get("report") or record.get("report_id")
        candidates = self._root_documents(root)
        candidates = [
            d
            for d in candidates
            if Instrument.parse(d.instrument).code == code
            and (
                declared == f"{code}_{d.fiscal_year}"
                or str(record.get("year")) == str(d.fiscal_year)
            )
        ]
        unique = {(d.issuer_id, d.fiscal_year, d.sha256): d for d in candidates}
        if len(unique) != 1:
            raise ValueError("legacy_attempt_source_identity_ambiguous")
        return next(iter(unique.values()))

    def _scan_results(self, root):
        path = root / "results.json"
        if not path.is_file():
            return
        records = load(path)
        if not isinstance(records, list):
            raise TypeError("legacy_results_not_a_list")
        for record in records:
            try:
                document = self._find_doc(root, record)
                block = record["block"]
                capture = Path(record["path"])
                capture = capture if capture.is_absolute() else root / capture
                if not (capture / "attempt.json").exists():
                    continue
                receipt = (
                    load(capture / "receipt.json")
                    if (capture / "receipt.json").exists()
                    else record
                )
                versions = annual_versions([document])
                key = artifact_key("extract:" + block, document.issuer_id, versions)
                self._attempt(
                    key,
                    {
                        **record,
                        "path": str(capture),
                        "receipt": receipt,
                        "document": document.to_dict(),
                    },
                )
                parsed = capture / "parsed.json"
                if (
                    record.get("accepted")
                    and parsed.is_file()
                    and isinstance(load(parsed), dict)
                ):
                    self._put(
                        key,
                        "extract:" + block,
                        parsed,
                        metadata={"document": document.to_dict(), "versions": versions},
                    )
            except (OSError, ValueError, KeyError, TypeError) as error:
                self._error(path, error)

    def _company_documents(self, root, code, years):
        years = {int(year) for year in years}
        documents = [
            d
            for d in self._root_documents(root)
            if Instrument.parse(d.instrument).code == code and d.fiscal_year in years
        ]
        if {d.fiscal_year for d in documents} != set(years):
            raise ValueError("legacy_company_source_versions_incomplete")
        issuers = {d.issuer_id for d in documents}
        if len(issuers) != 1:
            raise ValueError("legacy_company_issuer_ambiguous")
        return documents

    def _scan_companies(self, root):
        for summary in sorted((root / "contexts").glob("*/summary.json")):
            try:
                value = load(summary)
                code = str(value["company_code"])
                inputs = value["inputs"]
                documents = [
                    self._by_report[(root / "inputs", name)]
                    if (root / "inputs", name) in self._by_report
                    else self._by_report[(root, name)]
                    for name in inputs
                ]
                if any(Instrument.parse(d.instrument).code != code for d in documents):
                    raise ValueError("legacy_context_source_company_conflict")
                if not value.get("retention", {}).get("passed"):
                    raise ValueError("legacy_context_retention_not_passed")
                body = summary.parent / "模型正文.md"
                if body.is_file() and body.stat().st_size:
                    versions = annual_versions(documents)
                    self._put(
                        artifact_key("context", documents[0].issuer_id, versions),
                        "context",
                        body,
                        metadata={
                            "documents": [d.to_dict() for d in documents],
                            "versions": versions,
                            "retention": value["retention"],
                            "source_data": [
                                str(p)
                                for p, d in self._by_data_path.items()
                                if d in documents
                            ],
                        },
                    )
            except (OSError, ValueError, KeyError, TypeError) as error:
                self._error(summary, error)
        result = root / "summary_results.json"
        if not result.is_file():
            return
        value = load(result)
        companies = value.get("companies", []) if isinstance(value, dict) else value
        for company in companies:
            try:
                code = str(company["code"])
                documents = self._company_documents(root, code, company["years"])
                path = Path(
                    company.get("path") or root / "summaries" / code / "完整摘要.md"
                )
                if not path.is_absolute():
                    path = root / path
                if not path.is_file() or not path.stat().st_size:
                    raise ValueError("legacy_summary_body_unavailable")
                versions = annual_versions(documents)
                status = company.get("status", "partial")
                self._put(
                    artifact_key("summary", documents[0].issuer_id, versions),
                    "summary",
                    path,
                    status=status,
                    metadata={
                        "documents": [d.to_dict() for d in documents],
                        "versions": versions,
                        "part_statuses": company.get("part_statuses", {}),
                    },
                )
            except (OSError, ValueError, KeyError, TypeError) as error:
                self._error(result, error)

    def _scan_summary_attempts(self, root):
        plan_path = root / "plan.json"
        if not plan_path.is_file():
            return
        for company in load(plan_path).get("companies", []):
            if not isinstance(company, dict) or not company.get("years"):
                continue
            try:
                code = str(company["code"])
                documents = self._company_documents(root, code, company["years"])
                versions = annual_versions(documents)
                declared_jobs = [(company.get("direct", code + "_summary"), "summary")]
                declared_jobs += [
                    (identity, f"summary_part:{i}")
                    for i, identity in enumerate(company.get("parts", []), 1)
                ]
                for identity, stage in declared_jobs:
                    capture = root / "execution/jobs" / identity
                    if not (capture / "attempt.json").is_file():
                        continue
                    receipt = (
                        load(capture / "receipt.json")
                        if (capture / "receipt.json").is_file()
                        else {"status": "unknown"}
                    )
                    self._attempt(
                        artifact_key(stage, documents[0].issuer_id, versions),
                        {
                            "path": str(capture.resolve()),
                            "receipt": receipt,
                            "stage": stage,
                            "job": identity,
                            "versions": versions,
                            "documents": [d.to_dict() for d in documents],
                        },
                    )
            except (OSError, ValueError, TypeError, KeyError) as error:
                self._error(plan_path, error)

    def _scan_peer(self, root):
        for path in [root / "peer/完整分析.md", root / "完整分析.md"]:
            if not path.is_file() or not path.stat().st_size:
                continue
            manifest = root / "peer_input/manifest.json"
            try:
                request = next(
                    iter(sorted((root / "execution/jobs").glob("*peer*/request.json"))),
                    None,
                )
                request = request or (
                    root / "peer_request.json"
                    if (root / "peer_request.json").exists()
                    else None
                )
                text = ""
                if request:
                    payload = load(request)
                    content = (
                        payload["messages"][1]["content"]
                        if "messages" in payload
                        else payload.get("content", "")
                    )
                    text = (
                        content
                        if isinstance(content, str)
                        else "\n".join(part.get("text", "") for part in content)
                    )
                if manifest.exists():
                    meta = load(manifest)
                    sources = meta.get("source_hashes", {})
                    cohort = [str(c["company"]) for c in meta.get("companies", [])]
                else:
                    marker = "# 本轮摘要真实执行状态与分段状态"
                    if marker not in text or not (root / "freeze.json").exists():
                        raise ValueError("legacy_peer_dependency_manifest_missing")
                    declared, _ = json.JSONDecoder().raw_decode(
                        text.split(marker, 1)[1].lstrip()
                    )
                    cohort = [str(c["code"]) for c in declared]
                    references = re.findall(
                        r"(?m)^\s*-?\s*source:\s*(.+?data\.json)\s*$", text
                    )
                    frozen = load(root / "freeze.json").get("files", {})
                    sources = {
                        str(Path(name.strip(" \"'")).resolve()): frozen.get(
                            str(Path(name.strip(" \"'")).resolve())
                        )
                        for name in references
                    }
                    if not sources or any(not digest for digest in sources.values()):
                        raise ValueError("legacy_peer_annual_references_not_frozen")
                documents = [
                    self._by_data_path[Path(name).resolve()]
                    for name in sources
                    if Path(name).resolve() in self._by_data_path
                ]
                if not documents:
                    raise ValueError("legacy_peer_annual_dependencies_unknown")
                if set(cohort) != {
                    Instrument.parse(d.instrument).code for d in documents
                }:
                    raise ValueError("legacy_peer_cohort_dependency_mismatch")
                # Old peers remain valid under the user's annual-only policy;
                # preserve and expose their original price date rather than
                # charging again merely because the new prompt is price-free.
                price_dependent = False
                if request and any(
                    marker in text
                    for marker in [
                        "market_snapshot",
                        "行情快照",
                        "已知本地未复权",
                        "|收盘价|",
                    ]
                ):
                    price_dependent = True
                grouped = defaultdict(list)
                for document in documents:
                    grouped[document.issuer_id].append(document)
                versions = {
                    issuer: annual_versions(docs)
                    for issuer, docs in sorted(grouped.items())
                }
                for name, digest in sources.items():
                    if (
                        Path(name).resolve() in self._by_data_path
                        and file_sha(Path(name)) != digest
                    ):
                        raise ValueError("legacy_peer_annual_dependency_changed")
                quote_dates = []
                snapshot = root / "market_snapshot.json"
                if snapshot.is_file():
                    quote_dates = sorted(
                        {str(r["date"]) for r in load(snapshot) if r.get("date")}
                    )
                elif price_dependent:
                    quote_dates = sorted(
                        set(
                            re.findall(
                                r"\|\s*[A-Za-z0-9.\-]+\s*\|\s*(20\d{2}-\d{2}-\d{2})\s*\|\s*[\d.]+\s*\|\s*(?:CNY|HKD|USD)\s*\|",
                                text,
                            )
                        )
                    )
                key = artifact_key("peer", sorted(grouped), versions)
                self._put(
                    key,
                    "peer",
                    path,
                    metadata={
                        "documents": [d.to_dict() for d in documents],
                        "versions": versions,
                        "price_free": not price_dependent,
                        "legacy_price_dependency": price_dependent,
                        "quote_dates": quote_dates,
                        "warning": "Historical peer retains original valuation date; it is not a current quote."
                        if price_dependent
                        else "",
                    },
                )
                if request and (request.parent / "attempt.json").is_file():
                    receipt = (
                        load(request.parent / "receipt.json")
                        if (request.parent / "receipt.json").is_file()
                        else {"status": "unknown"}
                    )
                    self._attempt(
                        key,
                        {
                            "path": str(request.parent),
                            "receipt": receipt,
                            "stage": "peer",
                            "versions": versions,
                        },
                    )
            except (OSError, ValueError, TypeError, KeyError) as error:
                self._error(path, error)

    def _research_dependencies(self, plan, folder):
        """Follow declared, SHA-verified business inputs to annual identities."""
        records = (
            self.store.list_artifacts()
            if hasattr(self.store, "list_artifacts")
            else list(self.store.artifacts.values())
        )
        by_path = {Path(row["path"]).resolve(): row for row in records}
        documents = {}
        verified = {}
        sources = plan.get("source_hashes", {})
        if not isinstance(sources, dict):
            raise TypeError("legacy_research_source_hashes_mapping_required")
        for name, expected in sources.items():
            path = Path(name)
            path = path.resolve() if path.is_absolute() else (folder / path).resolve()
            row = by_path.get(path)
            declared = []
            if path in self._by_data_path:
                declared = [self._by_data_path[path].to_dict()]
            elif row and row.get("stage") in {"context", "summary", "peer", "annual"}:
                metadata = row.get("metadata", {})
                declared = metadata.get("documents", [])
                if not declared and metadata.get("document"):
                    declared = [metadata["document"]]
            if not declared:
                continue
            if not path.is_file() or file_sha(path) != expected:
                raise ValueError(
                    "legacy_research_business_dependency_changed: " + str(path)
                )
            verified[str(path)] = expected
            for document in declared:
                identity = (
                    document["issuer_id"],
                    int(document["fiscal_year"]),
                    document["sha256"],
                )
                documents[identity] = document
        if not documents:
            raise ValueError("legacy_research_annual_dependencies_unknown")
        grouped = defaultdict(list)
        for document in documents.values():
            grouped[document["issuer_id"]].append(document)
        versions = {
            issuer: annual_versions(docs) for issuer, docs in sorted(grouped.items())
        }
        return list(documents.values()), versions, verified

    def _scan_research(self, root):
        folder = root / "research"
        plan_path = folder / "plan.json"
        if not plan_path.is_file():
            return
        try:
            plan = load(plan_path)
            documents, versions, sources = self._research_dependencies(plan, folder)
            identity = plan.get("research_identity")
            required = {"versions", "instruments", "market", "focus", "assumptions"}
            identity_known = (
                isinstance(identity, dict)
                and set(identity) == required
                and identity["versions"] == versions
                and isinstance(identity["instruments"], list)
                and set(identity["instruments"]) == {d["instrument"] for d in documents}
                and isinstance(identity["market"], list)
                and isinstance(identity["focus"], str)
            )
            final_key = (
                artifact_key("research", sorted(versions), identity)
                if identity_known
                else None
            )
            captures, statuses = [], {}
            for job in plan.get("jobs", []):
                phase = job.get("id") if isinstance(job, dict) else job
                if phase not in {"draft", "review", "write"}:
                    continue
                capture = folder / "jobs" / phase
                attempt_path = capture / "attempt.json"
                if not attempt_path.is_file():
                    continue
                request = capture / "request.json"
                if not request.is_file():
                    request = folder / "requests" / (phase + ".json")
                try:
                    attempt = load(attempt_path)
                    if not request.is_file():
                        raise ValueError("legacy_research_attempt_request_missing")
                    digest = file_sha(request)
                    if (
                        attempt.get("request_sha256")
                        and attempt["request_sha256"] != digest
                    ):
                        raise ValueError("legacy_research_attempt_request_changed")
                    receipt = (
                        load(capture / "receipt.json")
                        if (capture / "receipt.json").is_file()
                        else {"status": "unknown"}
                    )
                except (OSError, ValueError, TypeError, KeyError) as error:
                    statuses[phase] = "unassigned"
                    self._report["unassigned"].append(
                        {
                            "manifest": str(plan_path),
                            "capture": str(capture),
                            "stage": "research_" + phase,
                            "reason": str(error),
                        }
                    )
                    continue
                key = (
                    fingerprint({"research": final_key, "round": phase})
                    if final_key
                    else fingerprint(
                        {
                            "legacy_research_attempt": str(capture.resolve()),
                            "request_sha256": digest,
                        }
                    )
                )
                self._attempt(
                    key,
                    {
                        "path": str(capture.resolve()),
                        "receipt": receipt,
                        "stage": "research_" + phase,
                        "phase": "research_" + phase,
                        "job": phase,
                        "versions": versions,
                        "documents": documents,
                        "request_sha256": digest,
                        "source_hashes": sources,
                        "scope_unresolved": not identity_known,
                    },
                )
                captures.append(str(capture.resolve()))
                statuses[phase] = receipt.get("status", "unknown")
            body = folder / "最终报告.md"
            if not body.is_file():
                body = folder / "正文.md"
            if body.is_file() and body.stat().st_size:
                key = final_key or artifact_key(
                    "legacy_reference:research",
                    sorted(versions),
                    {
                        "versions": versions,
                        "body_sha256": file_sha(body),
                        "source_hashes": sources,
                    },
                )
                self._put(
                    key,
                    "research" if identity_known else "legacy_reference",
                    body,
                    status="complete"
                    if set(statuses) == {"draft", "review", "write"}
                    and all(
                        s in {"complete", "completed", "succeeded"}
                        for s in statuses.values()
                    )
                    else "partial",
                    metadata={
                        "documents": documents,
                        "versions": versions,
                        "source_hashes": sources,
                        "producer_captures": captures,
                        "round_statuses": statuses,
                        "scope_unresolved": not identity_known,
                        "research_identity": identity if identity_known else None,
                        "warning": "Original research scope/quote/focus/assumptions are not proven to match a new run; no active research cache key was invented."
                        if not identity_known
                        else "",
                    },
                )
            if not identity_known:
                self._report["unassigned"].append(
                    {
                        "manifest": str(plan_path),
                        "stage": "research",
                        "captures": captures,
                        "reason": "legacy_research_final_cache_identity_unresolved",
                        "annual_dependencies_verified": True,
                        "attempts_imported": len(captures),
                    }
                )
        except (OSError, ValueError, TypeError, KeyError) as error:
            self._report["unassigned"].append(
                {
                    "manifest": str(plan_path),
                    "stage": "research",
                    "reason": str(error),
                    "captures": [
                        str(p.parent) for p in (folder / "jobs").glob("*/attempt.json")
                    ],
                }
            )

    def run(self) -> dict:
        self._documents = []
        self._scan_documents()
        roots = self._batch_roots()
        # Producer order is explicit: all company dependencies exist before
        # peers, all peers before research, even across continuation batches.
        for scanners in (
            (self._scan_results, self._scan_companies, self._scan_summary_attempts),
            (self._scan_peer,),
            (self._scan_research,),
        ):
            for root in roots:
                if not root.is_dir():
                    continue
                for scanner in scanners:
                    try:
                        scanner(root)
                    except (OSError, ValueError, KeyError, TypeError) as error:
                        self._error(root, error)
        self._report["documents"] = len(self._documents)
        return dict(self._report)

    def documents(self) -> list[AnnualDocument]:
        if not self._documents:
            for path in sorted((self.root / "documents").glob("*.json")):
                try:
                    self._documents.append(AnnualDocument(**load(path)))
                except (OSError, ValueError, TypeError) as error:
                    self._error(path, error)
        return list(self._documents)
