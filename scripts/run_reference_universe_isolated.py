#!/usr/bin/env python3
"""Low-memory supervisor: run each reference company in a fresh process."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / "config" / ".env")

from src.data.reference_universe_backfill import (
    REFERENCE_BATCH_CONTRACT,
    ReferenceUniverseBackfillService,
    _atomic_json,
    _worker_error_row,
    load_reference_manifest,
)


def _checkpoint(
    service: ReferenceUniverseBackfillService,
    checkpoint_dir: Path,
    *,
    code: str,
    batch_id: str,
    fingerprint: str,
) -> dict | None:
    return service._load_checkpoint(
        checkpoint_dir / f"{code}.json",
        batch_id=batch_id,
        fingerprint=fingerprint,
    )


def _write_timeout_checkpoint(
    service: ReferenceUniverseBackfillService,
    checkpoint_dir: Path,
    *,
    code: str,
    batch_id: str,
    fingerprint: str,
    timeout_seconds: int,
) -> None:
    row = _worker_error_row(
        code,
        TimeoutError(f"company process exceeded {timeout_seconds} seconds"),
    )
    payload = service._checkpoint_payload(
        batch_id=batch_id,
        fingerprint=fingerprint,
        row=row,
    )
    _atomic_json(checkpoint_dir / f"{code}.json", payload)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run every reference company in a fresh child process so PDF heap "
            "memory is returned to the operating system after each company"
        )
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--evaluation-date",
        type=date.fromisoformat,
        default=date.today(),
    )
    parser.add_argument("--attempts-per-run", type=int, default=2)
    parser.add_argument("--company-timeout-seconds", type=int, default=1200)
    args = parser.parse_args()
    if args.attempts_per_run < 1:
        parser.error("--attempts-per-run must be positive")
    if args.company_timeout_seconds < 60:
        parser.error("--company-timeout-seconds must be at least 60")

    with Path(args.config).open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    manifest = load_reference_manifest(args.manifest)
    output_dir = Path(args.output_dir).resolve()
    service = ReferenceUniverseBackfillService(
        config,
        manifest,
        output_dir=output_dir,
        workers=1,
    )
    batch_id, fingerprint = service._batch_identity(args.evaluation_date)
    batch_dir = output_dir / "reference_batches" / batch_id
    checkpoint_dir = batch_dir / "codes"
    progress_path = batch_dir / "supervisor_progress.json"
    companies = manifest["companies"]
    started_at = datetime.now().isoformat()
    counters = {"success": 0, "failed": 0, "reused": 0}

    def write_progress(state: str, code: str | None = None) -> None:
        completed = counters["success"] + counters["failed"]
        _atomic_json(
            progress_path,
            {
                "contract": REFERENCE_BATCH_CONTRACT,
                "mode": "fresh-process-per-company",
                "batch_id": batch_id,
                "fingerprint": fingerprint,
                "state": state,
                "started_at": started_at,
                "updated_at": datetime.now().isoformat(),
                "total": len(companies),
                "completed": completed,
                "success": counters["success"],
                "failed": counters["failed"],
                "reused": counters["reused"],
                "pending": max(0, len(companies) - completed),
                "current_code": code,
            },
        )

    write_progress("running")
    for index, company in enumerate(companies, start=1):
        code = company["code"]
        existing = _checkpoint(
            service,
            checkpoint_dir,
            code=code,
            batch_id=batch_id,
            fingerprint=fingerprint,
        )
        if existing is not None and existing.get("status") == "success":
            counters["success"] += 1
            counters["reused"] += 1
            write_progress("running", code)
            continue

        write_progress("running", code)
        for attempt in range(1, args.attempts_per_run + 1):
            command = [
                sys.executable,
                str(ROOT / "scripts" / "backfill_reference_universe.py"),
                "--config",
                str(Path(args.config).resolve()),
                "--manifest",
                str(Path(args.manifest).resolve()),
                "--output-dir",
                str(output_dir),
                "--workers",
                "1",
                "--evaluation-date",
                args.evaluation_date.isoformat(),
                "--codes",
                code,
            ]
            attempt_started = time.perf_counter()
            try:
                completed = subprocess.run(
                    command,
                    cwd=ROOT,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    timeout=args.company_timeout_seconds,
                )
                return_code = completed.returncode
            except subprocess.TimeoutExpired:
                return_code = 124
                _write_timeout_checkpoint(
                    service,
                    checkpoint_dir,
                    code=code,
                    batch_id=batch_id,
                    fingerprint=fingerprint,
                    timeout_seconds=args.company_timeout_seconds,
                )
            elapsed = time.perf_counter() - attempt_started
            current = _checkpoint(
                service,
                checkpoint_dir,
                code=code,
                batch_id=batch_id,
                fingerprint=fingerprint,
            )
            status = current.get("status") if current else "missing"
            print(
                f"[{index}/{len(companies)}] {code} attempt={attempt} "
                f"status={status} exit={return_code} elapsed={elapsed:.1f}s",
                flush=True,
            )
            if status == "success":
                break

        final = _checkpoint(
            service,
            checkpoint_dir,
            code=code,
            batch_id=batch_id,
            fingerprint=fingerprint,
        )
        if final is not None and final.get("status") == "success":
            counters["success"] += 1
        else:
            counters["failed"] += 1
        write_progress("running", code)

    report = service.run(
        evaluation_date=args.evaluation_date,
        retry_failures=False,
    )
    state = "complete" if not report["failed_codes"] else "partial"
    write_progress(state)
    print(
        json.dumps(
            {
                "state": state,
                "batch_id": batch_id,
                "success": counters["success"],
                "failed": counters["failed"],
                "reused": counters["reused"],
                "report": report["output_file"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if state == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
