"""Resumable process-parallel backfill for a reference-company universe."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import time
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

from .point_in_time_backfill import PointInTimeBackfillService

logger = logging.getLogger(__name__)

REFERENCE_MANIFEST_CONTRACT = "index-reference-universe-1"
REFERENCE_BATCH_CONTRACT = "reference-universe-backfill-1"


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def load_reference_manifest(path: str | Path) -> dict[str, Any]:
    """Load and validate a current-membership reference universe manifest."""
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("reference manifest must be a JSON object")
    if payload.get("contract") != REFERENCE_MANIFEST_CONTRACT:
        raise ValueError(
            "reference manifest contract must be "
            f"{REFERENCE_MANIFEST_CONTRACT!r}"
        )
    companies = payload.get("companies")
    if not isinstance(companies, list) or not companies:
        raise ValueError("reference manifest must contain a non-empty companies list")
    seen: set[str] = set()
    normalized = []
    for item in companies:
        if not isinstance(item, dict):
            raise ValueError("every reference company must be an object")
        code = str(item.get("code", "")).strip()
        if len(code) != 6 or not code.isdigit():
            raise ValueError(f"invalid A-share code in reference manifest: {code!r}")
        if code in seen:
            raise ValueError(f"duplicate company in reference manifest: {code}")
        seen.add(code)
        row = dict(item)
        row["code"] = code
        normalized.append(row)
    result = dict(payload)
    result["companies"] = sorted(normalized, key=lambda item: item["code"])
    result["company_count"] = len(normalized)
    result["source_file"] = str(source.resolve())
    return result


def _worker_error_row(code: str, exc: BaseException) -> dict[str, Any]:
    reason = f"{type(exc).__name__}: {exc}"
    return {
        "code": code,
        "market": "a_share",
        "instrument_type": "equity",
        "market_history": {"status": "failed", "reason": reason},
        "statements": {"status": "failed", "reason": reason},
    }


def run_reference_company_worker(payload: dict[str, Any]) -> dict[str, Any]:
    """Pure process entry point: fetch and persist exactly one company."""
    code = str(payload["code"])
    started = time.perf_counter()
    try:
        service = PointInTimeBackfillService(payload["config"])
        row = service._backfill_one(
            code,
            date.fromisoformat(payload["start"]),
            date.fromisoformat(payload["end"]),
        )
    except BaseException as exc:  # The parent must always receive a checkpointable row.
        logger.exception("Reference-company worker crashed for %s", code)
        row = _worker_error_row(code, exc)
    row["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    return row


def _row_succeeded(row: dict[str, Any]) -> bool:
    return (
        row.get("market_history", {}).get("status") == "success"
        and row.get("statements", {}).get("status") == "success"
    )


class ReferenceUniverseBackfillService:
    """Backfill an index reference universe with bounded process concurrency."""

    def __init__(
        self,
        config: dict[str, Any],
        manifest: dict[str, Any],
        *,
        output_dir: str | Path,
        workers: int = 2,
        worker: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ):
        if not 1 <= int(workers) <= 4:
            raise ValueError("workers must be between 1 and 4")
        self.workers = int(workers)
        self.worker = worker or run_reference_company_worker
        if worker is not None and self.workers != 1:
            raise ValueError("an injected worker is only supported with workers=1")
        self.output_dir = Path(output_dir).resolve()
        self.manifest = copy.deepcopy(manifest)
        self.manifest.pop("source_file", None)
        self.manifest_hash = _sha256(self.manifest)
        self.config = self._worker_config(config)
        self.history_years = max(
            1,
            int(
                self.config.get("point_in_time_data", {}).get(
                    "history_years", 6
                )
            ),
        )

    def _worker_config(self, config: dict[str, Any]) -> dict[str, Any]:
        prepared = copy.deepcopy(config)
        settings = prepared.setdefault("point_in_time_data", {})
        settings["output_dir"] = str(self.output_dir)
        settings["cninfo_pdf_cache_dir"] = str(
            self.output_dir / "provider_cache" / "cninfo"
        )
        return prepared

    def _batch_identity(self, end: date) -> tuple[str, str]:
        settings = self.config.get("point_in_time_data", {}) or {}
        contract_settings = {
            "history_years": self.history_years,
            "official_statement_crawlers": settings.get(
                "official_statement_crawlers", True
            ),
            "official_pdf_recent_years": settings.get(
                "official_pdf_recent_years", 3
            ),
            "market_history": settings.get("market_history", {}),
        }
        fingerprint = _sha256(
            {
                "contract": REFERENCE_BATCH_CONTRACT,
                "manifest_hash": self.manifest_hash,
                "evaluation_date": end.isoformat(),
                "settings": contract_settings,
            }
        )
        batch_id = f"{end:%Y%m%d}_{fingerprint[:12]}"
        return batch_id, fingerprint

    @staticmethod
    def _checkpoint_payload(
        *,
        batch_id: str,
        fingerprint: str,
        row: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "contract": REFERENCE_BATCH_CONTRACT,
            "batch_id": batch_id,
            "fingerprint": fingerprint,
            "code": row["code"],
            "status": "success" if _row_succeeded(row) else "failed",
            "completed_at": datetime.now().isoformat(),
            "row": row,
        }

    @staticmethod
    def _load_checkpoint(
        path: Path,
        *,
        batch_id: str,
        fingerprint: str,
    ) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        if (
            payload.get("contract") != REFERENCE_BATCH_CONTRACT
            or payload.get("batch_id") != batch_id
            or payload.get("fingerprint") != fingerprint
            or not isinstance(payload.get("row"), dict)
        ):
            return None
        return payload

    @staticmethod
    def _progress(
        *,
        batch_id: str,
        fingerprint: str,
        total: int,
        checkpoints: dict[str, dict[str, Any]],
        started_at: str,
        state: str,
        last_code: str | None = None,
    ) -> dict[str, Any]:
        successes = sum(
            item.get("status") == "success" for item in checkpoints.values()
        )
        failed = sum(
            item.get("status") == "failed" for item in checkpoints.values()
        )
        return {
            "contract": REFERENCE_BATCH_CONTRACT,
            "batch_id": batch_id,
            "fingerprint": fingerprint,
            "state": state,
            "started_at": started_at,
            "updated_at": datetime.now().isoformat(),
            "total": total,
            "completed": len(checkpoints),
            "success": successes,
            "failed": failed,
            "pending": max(0, total - len(checkpoints)),
            "last_code": last_code,
        }

    def _write_result(
        self,
        *,
        row: dict[str, Any],
        batch_id: str,
        fingerprint: str,
        checkpoint_dir: Path,
        checkpoints: dict[str, dict[str, Any]],
        progress_path: Path,
        total: int,
        started_at: str,
    ) -> None:
        code = str(row["code"])
        checkpoint = self._checkpoint_payload(
            batch_id=batch_id,
            fingerprint=fingerprint,
            row=row,
        )
        _atomic_json(checkpoint_dir / f"{code}.json", checkpoint)
        checkpoints[code] = checkpoint
        _atomic_json(
            progress_path,
            self._progress(
                batch_id=batch_id,
                fingerprint=fingerprint,
                total=total,
                checkpoints=checkpoints,
                started_at=started_at,
                state="running",
                last_code=code,
            ),
        )

    def _run_sequential(
        self,
        payloads: Iterable[dict[str, Any]],
        on_result: Callable[[dict[str, Any]], None],
    ) -> None:
        for payload in payloads:
            try:
                row = self.worker(payload)
            except BaseException as exc:
                row = _worker_error_row(str(payload["code"]), exc)
            on_result(row)

    def _run_parallel(
        self,
        payloads: Iterable[dict[str, Any]],
        on_result: Callable[[dict[str, Any]], None],
    ) -> None:
        iterator = iter(payloads)
        in_flight: dict[Future, str] = {}
        maximum_in_flight = self.workers * 2
        with ProcessPoolExecutor(max_workers=self.workers) as executor:
            while len(in_flight) < maximum_in_flight:
                try:
                    payload = next(iterator)
                except StopIteration:
                    break
                future = executor.submit(run_reference_company_worker, payload)
                in_flight[future] = str(payload["code"])
            while in_flight:
                completed, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                for future in completed:
                    code = in_flight.pop(future)
                    try:
                        row = future.result()
                    except BaseException as exc:
                        row = _worker_error_row(code, exc)
                    on_result(row)
                    try:
                        payload = next(iterator)
                    except StopIteration:
                        continue
                    replacement = executor.submit(
                        run_reference_company_worker, payload
                    )
                    in_flight[replacement] = str(payload["code"])

    def run(
        self,
        *,
        evaluation_date: date | None = None,
        codes: Iterable[str] | None = None,
        force: bool = False,
        retry_failures: bool = True,
    ) -> dict[str, Any]:
        end = evaluation_date or date.today()
        start = end - timedelta(days=int(self.history_years * 365.25))
        requested = {str(code).strip() for code in codes or [] if str(code).strip()}
        companies = [
            item
            for item in self.manifest["companies"]
            if not requested or item["code"] in requested
        ]
        if requested - {item["code"] for item in companies}:
            missing = sorted(requested - {item["code"] for item in companies})
            raise ValueError(f"codes not present in reference manifest: {missing}")
        batch_id, fingerprint = self._batch_identity(end)
        batch_dir = self.output_dir / "reference_batches" / batch_id
        checkpoint_dir = batch_dir / "codes"
        progress_path = batch_dir / "progress.json"
        started_at = datetime.now().isoformat()
        checkpoints: dict[str, dict[str, Any]] = {}
        pending = []
        for company in companies:
            code = company["code"]
            checkpoint = self._load_checkpoint(
                checkpoint_dir / f"{code}.json",
                batch_id=batch_id,
                fingerprint=fingerprint,
            )
            reusable = (
                not force
                and checkpoint is not None
                and (
                    checkpoint.get("status") == "success"
                    or not retry_failures
                )
            )
            if reusable:
                checkpoints[code] = checkpoint
            else:
                pending.append(code)

        _atomic_json(
            progress_path,
            self._progress(
                batch_id=batch_id,
                fingerprint=fingerprint,
                total=len(companies),
                checkpoints=checkpoints,
                started_at=started_at,
                state="running",
            ),
        )
        payloads = [
            {
                "code": code,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "config": self.config,
            }
            for code in pending
        ]

        def on_result(row: dict[str, Any]) -> None:
            self._write_result(
                row=row,
                batch_id=batch_id,
                fingerprint=fingerprint,
                checkpoint_dir=checkpoint_dir,
                checkpoints=checkpoints,
                progress_path=progress_path,
                total=len(companies),
                started_at=started_at,
            )

        interrupted = False
        try:
            if self.workers == 1:
                self._run_sequential(payloads, on_result)
            else:
                self._run_parallel(payloads, on_result)
        except KeyboardInterrupt:
            interrupted = True
            logger.warning("Reference-universe batch interrupted; checkpoints retained")

        ordered_rows = [
            checkpoints[item["code"]]["row"]
            for item in companies
            if item["code"] in checkpoints
        ]
        summary = PointInTimeBackfillService._summary(start, end, ordered_rows)
        failed_codes = sorted(
            code
            for code, item in checkpoints.items()
            if item.get("status") != "success"
        )
        state = (
            "interrupted"
            if interrupted
            else "complete"
            if len(checkpoints) == len(companies) and not failed_codes
            else "partial"
        )
        summary.update(
            {
                "contract": REFERENCE_BATCH_CONTRACT,
                "batch_id": batch_id,
                "fingerprint": fingerprint,
                "manifest_contract": self.manifest.get("contract"),
                "manifest_hash": self.manifest_hash,
                "manifest_as_of": self.manifest.get("as_of"),
                "weight_as_of": self.manifest.get("weight_as_of"),
                "state": state,
                "worker_processes": self.workers,
                "requested_company_count": len(companies),
                "checkpoint_count": len(checkpoints),
                "reused_checkpoint_count": len(companies) - len(pending),
                "failed_codes": failed_codes,
                "batch_dir": str(batch_dir),
            }
        )
        report_path = batch_dir / "report.json"
        latest_path = self.output_dir / "latest_reference_backfill.json"
        _atomic_json(report_path, summary)
        _atomic_json(latest_path, summary)
        _atomic_json(
            progress_path,
            self._progress(
                batch_id=batch_id,
                fingerprint=fingerprint,
                total=len(companies),
                checkpoints=checkpoints,
                started_at=started_at,
                state=state,
            ),
        )
        summary["output_file"] = str(report_path)
        return summary
