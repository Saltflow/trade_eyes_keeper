"""Authenticated report submission and receipts over the existing report runtime."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from ...core import report_execution
from ...core.execution import get_runtime
from ...data.dataset_catalog import (
    DatasetCatalog,
    _checked_file,
    _sha256,
    eligibility_reason,
)
from .data_tools import _safe, paginate_rows


class ReportTools:
    """Bind trusted identity once; model arguments cannot select another owner."""

    def __init__(
        self,
        config,
        *,
        owner,
        chat_id,
        runtime=None,
        config_path=report_execution.DEFAULT_CONFIG_PATH,
    ):
        if not isinstance(owner, str) or not owner.strip():
            raise PermissionError("报告操作缺少认证用户")
        if not isinstance(chat_id, str) or not chat_id.strip():
            raise PermissionError("报告操作缺少认证会话")
        self.config = config
        self.owner = owner
        self.chat_id = chat_id
        self.runtime = runtime or get_runtime(report_execution.runtime_path(config))
        self.config_path = config_path
        config_directory = Path(config_path).resolve().parent
        self.root = (
            config_directory.parent
            if config_directory.name == "config"
            else config_directory
        )

    def _owned(self, run):
        if not run or run.get("job_id") not in report_execution.REPORT_JOB_IDS.values():
            return False
        payload = run.get("payload")
        if not isinstance(payload, dict):
            return False
        snapshot = payload.get("config_snapshot")
        if not isinstance(snapshot, dict):
            return False
        bindings = [
            snapshot[key]
            for key in ("_report_context", "_trigger_context")
            if key in snapshot
        ]
        # Unbound historical/CLI runs are not public. Conflicting bindings fail closed.
        return bool(bindings) and all(
            isinstance(binding, dict)
            and binding.get("owner") == self.owner
            and binding.get("chat_id") == self.chat_id
            for binding in bindings
        )

    def _project(self, run, *, include_archives=True):
        projected = _safe(
            {
                "run_id": run["id"],
                "report_type": run["job_id"].removeprefix("reports."),
                **{
                    key: run.get(key)
                    for key in (
                        "request_id",
                        "status",
                        "created",
                        "updated",
                        "started",
                        "finished",
                        "stage",
                        "reason",
                        "result",
                    )
                },
            }
        )
        if include_archives:
            archives, gaps = self._archives(run)
            projected["archives"] = _safe(archives)
            projected["coverage_gaps"] = gaps
        return projected

    def _archives(self, run):
        """Index only explicit, existing report artifacts within catalog policy."""
        result = run.get("result")
        candidates, gaps = [], []

        def collect(value, evidence, *, artifacts=False, depth=0, digest=None):
            if depth > 8 or len(candidates) >= 100:
                gaps.append("archive_reference_scan_limit")
                return
            if isinstance(value, str):
                if (
                    value
                    and (artifacts or evidence.endswith(".archive"))
                    and (
                        not artifacts
                        or Path(value).suffix.lower()
                        in {
                            ".html",
                            ".htm",
                            ".pdf",
                            ".txt",
                            ".md",
                            ".json",
                            ".csv",
                            ".xlsx",
                            ".png",
                            ".jpg",
                            ".jpeg",
                            ".svg",
                        }
                    )
                ):
                    candidates.append((value, evidence, digest))
            elif isinstance(value, list):
                for index, child in enumerate(value[:100]):
                    collect(
                        child,
                        f"{evidence}[{index}]",
                        artifacts=artifacts,
                        depth=depth + 1,
                        digest=digest,
                    )
                if len(value) > 100:
                    gaps.append("archive_reference_scan_limit")
            elif isinstance(value, dict):
                for key, child in value.items():
                    if (
                        artifacts
                        and key not in {"log", "receipt", "data_readiness", "sha256"}
                        and not key.endswith("_sha256")
                    ):
                        collect(
                            child,
                            f"{evidence}.{key}",
                            artifacts=True,
                            depth=depth + 1,
                            digest=value.get(key + "_sha256") or value.get("sha256"),
                        )
                    elif not artifacts and key in {"archive", "pieces"}:
                        collect(
                            child,
                            f"{evidence}.{key}",
                            depth=depth + 1,
                            digest=value.get("archive_sha256")
                            if key == "archive"
                            else None,
                        )

        if isinstance(result, dict):
            collect(result.get("artifacts"), "result.artifacts", artifacts=True)
            deliveries = result.get("deliveries")
            if isinstance(deliveries, dict):
                collect(deliveries.get("email"), "result.deliveries.email")
        if not candidates:
            return [], list(dict.fromkeys([*gaps, "archive_reference_missing"]))

        trusted = [
            self.root / value
            for value in ("data/email_archive", "reports", "output", "outputs")
        ]
        configured = (self.config.get("email") or {}).get("archive_dir")
        if isinstance(configured, str) and configured:
            directory = Path(configured)
            trusted.append(
                directory if directory.is_absolute() else self.root / directory
            )
        catalog, archives, seen = None, [], set()
        for value, evidence, expected_hash in candidates:
            path = Path(value)
            if ".." in path.parts:
                gaps.append("archive_path_traversal")
                continue
            path = path if path.is_absolute() else self.root / path
            try:
                relative = path.relative_to(self.root).as_posix()
            except ValueError:
                gaps.append("archive_outside_project")
                continue
            if relative in seen:
                continue
            if not any(path.is_relative_to(base) for base in trusted):
                gaps.append("archive_outside_trusted_roots")
                continue
            if eligibility_reason(relative):
                gaps.append("archive_outside_catalog_policy")
                continue
            try:
                checked = _checked_file(self.root, relative)
                if not isinstance(expected_hash, str) or not re.fullmatch(
                    r"[0-9a-f]{64}", expected_hash
                ):
                    gaps.append("archive_version_unverified")
                    continue
                if _sha256(checked) != expected_hash:
                    gaps.append("archive_content_changed")
                    continue
                seen.add(relative)
                catalog = catalog or DatasetCatalog(self.root)
                indexed = catalog.index_paths([relative])["files"][0]
                if not indexed.get("file_id"):
                    gaps.append("archive_registration_failed")
                    continue
                # Re-check the exact bytes and path after registration.
                _, metadata = catalog.resolve_file(indexed["file_id"])
                if metadata["sha256"] != expected_hash:
                    gaps.append("archive_content_changed")
                    continue
                archives.append(
                    {
                        "data_id": indexed["file_id"],
                        "source": relative,
                        "sha256": metadata["sha256"],
                        "product": "archived_artifact",
                        "evidence": evidence,
                    }
                )
            except FileNotFoundError:
                gaps.append("archive_file_missing")
            except (OSError, ValueError, sqlite3.Error):
                gaps.append("archive_unavailable_or_unsafe")
        return archives, list(dict.fromkeys(gaps))

    def submit(
        self,
        *,
        request_id,
        kind="daily",
        report_id="morning_snapshot",
        force=False,
        source="bot",
    ):
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("报告请求必须使用认证事件或已确认提案的稳定 request_id")
        if kind not in {"daily", "brief"} or report_id not in {
            "morning_snapshot",
            "afternoon_snapshot",
        }:
            raise ValueError("invalid report kind or report_id")
        if type(force) is not bool:
            raise TypeError("force must be a boolean")
        config = {
            **self.config,
            "_report_context": {"owner": self.owner, "chat_id": self.chat_id},
        }
        run = report_execution.submit_report(
            config,
            kind=kind,
            report_id=report_id,
            force=force,
            source=source,
            request_id=request_id,
            config_path=self.config_path,
            runtime=self.runtime,
        )
        run_id = run if isinstance(run, str) else run["id"]
        return self.get(run_id)

    def get(self, run_id):
        run = self.runtime.store.get(run_id)
        if not self._owned(run):
            raise PermissionError("报告执行记录不可用或不属于当前认证用户和会话")
        return self._project(run)

    def for_request(self, kind, request_id):
        run = self.runtime.store.get_request(
            report_execution.REPORT_JOB_IDS[kind], request_id
        )
        if run is None:
            return None
        if not self._owned(run):
            raise PermissionError("报告请求不属于当前认证用户和会话")
        return self._project(run)

    def runs(
        self, *, report_type=None, status=None, limit=50, offset=0, scan_limit=1000
    ):
        if (
            report_type is not None
            and report_type not in report_execution.REPORT_JOB_IDS
        ):
            raise ValueError("report_type must be daily or brief")
        if type(scan_limit) is not int or not 1 <= scan_limit <= 9999:
            raise ValueError("scan_limit must be between 1 and 9999")
        rows, gaps = [], []
        kinds = (report_type,) if report_type else ("daily", "brief")
        for kind in kinds:
            runs = self.runtime.store.list_runs(
                job_id=report_execution.REPORT_JOB_IDS[kind],
                status=status,
                limit=scan_limit + 1,
            )
            if len(runs) > scan_limit:
                gaps.append(f"reports.{kind}:scan_limit_reached")
            rows.extend(run for run in runs[:scan_limit] if self._owned(run))
        rows.sort(key=lambda run: run["seq"], reverse=True)
        page = paginate_rows(
            (
                self._project(run, include_archives=offset <= index < offset + limit)
                for index, run in enumerate(rows)
            ),
            limit=limit,
            offset=offset,
        )
        return {
            **page,
            "source": "execution_store",
            "scope": "authenticated_owner_chat",
            "coverage_gaps": gaps,
        }
