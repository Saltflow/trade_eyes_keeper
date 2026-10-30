#!/usr/bin/env python3
"""Bounded, resumable supervisor with a fresh process for each company."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / "config" / ".env")

from scripts.backfill_reference_universe import SHANGHAI_TZ, scoped_config
from src.data.baostock_access import (
    BaostockAccessController,
    BaostockAccessPolicy,
    BaostockTransientError,
)
from src.data.point_in_time_backfill import PointInTimeBackfillService
from src.data.reference_universe_backfill import (
    REFERENCE_BATCH_CONTRACT,
    ReferenceUniverseBackfillService,
    _atomic_json,
    _worker_error_row,
    load_reference_manifest,
)


@contextmanager
def _dataset_lock(output_dir: Path):
    """Reject overlapping supervisors, including different batches in one root."""
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / ".reference_supervisor.lock").open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError(
                f"another reference supervisor owns dataset {output_dir}"
            ) from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class IsolatedReferenceSupervisor:
    """Limit live children and attempts; generate the final report from disk only."""

    def __init__(
        self,
        config: dict,
        manifest: dict,
        *,
        config_path: Path,
        manifest_path: Path,
        output_dir: Path,
        evaluation_date: date,
        workers: int = 1,
        attempts_per_run: int = 2,
        company_timeout_seconds: int = 1200,
    ):
        if not 1 <= workers <= 4:
            raise ValueError("workers must be between 1 and 4")
        if attempts_per_run < 1:
            raise ValueError("attempts_per_run must be positive")
        if company_timeout_seconds < 60:
            raise ValueError("company_timeout_seconds must be at least 60")
        self.workers = workers
        self.attempts_per_run = attempts_per_run
        self.company_timeout_seconds = company_timeout_seconds
        self.config_path = config_path.resolve()
        self.manifest_path = manifest_path.resolve()
        self.evaluation_date = evaluation_date
        settings = config.get("point_in_time_data", {}) or {}
        # Pass effective depths explicitly so parent and every child agree.
        self.config = scoped_config(
            config,
            history_years=int(settings.get("history_years", 6)),
            official_pdf_recent_years=int(settings.get("official_pdf_recent_years", 3)),
        )
        self.access_policy = BaostockAccessPolicy.from_config(self.config)
        self.access_controller = BaostockAccessController(self.config)
        self.service = ReferenceUniverseBackfillService(
            self.config, manifest, output_dir=output_dir, workers=1
        )
        self.batch_id, self.fingerprint = self.service._batch_identity(evaluation_date)
        self.batch_dir = self.service.output_dir / "reference_batches" / self.batch_id
        self.checkpoint_dir = self.batch_dir / "codes"
        self.codes = [company["code"] for company in self.service.manifest["companies"]]
        # Finalized work and on-disk evidence differ while failed rows await retry.
        self.checkpoints: dict[str, dict] = {}
        self.observed_checkpoints: dict[str, dict] = {}
        self.pending: dict[str, dict] = {}
        self.running: dict[str, dict] = {}
        self.source_access: dict | None = None
        self.source_wait_until = 0.0
        self._last_progress_state: str | None = None
        self.reused = 0
        self.started_at = datetime.now(SHANGHAI_TZ).isoformat()

    def _checkpoint(self, code: str) -> dict | None:
        try:
            checkpoint = self.service._load_checkpoint(
                self.checkpoint_dir / f"{code}.json",
                batch_id=self.batch_id,
                fingerprint=self.fingerprint,
            )
        except (AttributeError, TypeError):
            return None
        if checkpoint is None or checkpoint.get("code") != code:
            return None
        if checkpoint["row"].get("code") != code:
            return None
        if checkpoint.get("status") not in {"success", "failed"} or not all(
            isinstance(checkpoint["row"].get(key), dict)
            for key in ("market_history", "statements")
        ):
            return None
        return checkpoint

    def _reusable(self, checkpoint: dict | None) -> bool:
        return bool(
            checkpoint
            and checkpoint.get("status") == "success"
            and checkpoint["row"].get("statements", {}).get("status") == "success"
            and self.service._checkpoint_covers_evaluation_end(
                checkpoint, self.evaluation_date
            )
        )

    def _failure(self, code: str, exc: BaseException, elapsed: float = 0) -> dict:
        row = _worker_error_row(code, exc)
        row["elapsed_seconds"] = round(elapsed, 3)
        checkpoint = self.service._checkpoint_payload(
            batch_id=self.batch_id, fingerprint=self.fingerprint, row=row
        )
        _atomic_json(self.checkpoint_dir / f"{code}.json", checkpoint)
        return checkpoint

    @staticmethod
    def _retry_delay(retry_at: str | None) -> float:
        if not retry_at:
            return 0.0
        try:
            target = datetime.fromisoformat(retry_at.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError):
            return 0.0
        if target.tzinfo is None:
            target = target.replace(tzinfo=SHANGHAI_TZ)
        return max(0.0, (target - datetime.now(SHANGHAI_TZ)).total_seconds())

    def _refresh_access(self, checkpoint: dict | None = None) -> bool:
        """Read the shared gate without contacting the provider."""
        try:
            status = {"source": "baostock", **self.access_controller.status()}
        except BaostockTransientError as exc:
            status = exc.to_dict()
            if not status.get("retry_at"):
                previous = self.source_access or {}
                if (
                    previous.get("reason") == status.get("reason")
                    and time.monotonic() < self.source_wait_until
                ):
                    status["retry_at"] = previous.get("retry_at")
                else:
                    status["retry_at"] = (
                        datetime.now(SHANGHAI_TZ)
                        + timedelta(
                            seconds=self.access_policy.retry_backoff_initial_seconds
                        )
                    ).isoformat()
        row_access = (checkpoint or {}).get("row", {}).get("source_access")
        if isinstance(row_access, dict) and row_access.get("blocked"):
            # A newly completed child may observe a block before another status read.
            status.update(row_access)
        previous_retry_at = (self.source_access or {}).get("retry_at")
        self.source_access = status
        if status.get("retry_at") != previous_retry_at:
            self.source_wait_until = time.monotonic() + self._retry_delay(
                status.get("retry_at")
            )
        return not status.get("blocked", False)

    def _pending_codes(self) -> list[str]:
        return [code for code in self.codes if code not in self.checkpoints]

    def _pending_failed_codes(self) -> list[str]:
        return [
            code
            for code in self._pending_codes()
            if self.observed_checkpoints.get(code, {}).get("status") == "failed"
        ]

    def _retry_schedule(self) -> list[dict]:
        return [
            {
                "code": code,
                "next_attempt": item["attempt"],
                "retry_at": item["retry_at"],
            }
            for code, item in self.pending.items()
            if item["retry_at"] is not None
        ]

    def _queue_retry(self, code: str, attempt: int, checkpoint: dict) -> None:
        delay = self.access_policy.retry_backoff_initial_seconds
        maximum = self.access_policy.retry_backoff_max_seconds
        for _ in range(attempt - 1):
            delay = min(maximum, delay * 2)
            if delay >= maximum:
                break
        row_access = checkpoint["row"].get("source_access")
        delay = max(
            min(maximum, delay),
            self._retry_delay(
                row_access.get("retry_at") if isinstance(row_access, dict) else None
            ),
        )
        self.pending[code] = {
            "attempt": attempt + 1,
            "ready": time.monotonic() + delay,
            "retry_at": (
                datetime.now(SHANGHAI_TZ) + timedelta(seconds=delay)
            ).isoformat(),
        }

    def _write_progress(self, state: str, code: str | None = None) -> None:
        self._last_progress_state = state
        success = sum(cp.get("status") == "success" for cp in self.checkpoints.values())
        running = [
            {
                "code": item_code,
                "pid": child["process"].pid,
                "attempt": child["attempt"],
                "started_at": child["started_at"],
            }
            for item_code, child in sorted(self.running.items())
        ]
        _atomic_json(
            self.batch_dir / "supervisor_progress.json",
            {
                "contract": REFERENCE_BATCH_CONTRACT,
                "mode": "fresh-process-per-company",
                "batch_id": self.batch_id,
                "fingerprint": self.fingerprint,
                "state": state,
                "supervisor_pid": os.getpid(),
                "workers": self.workers,
                "attempts_per_run": self.attempts_per_run,
                "company_timeout_seconds": self.company_timeout_seconds,
                "started_at": self.started_at,
                "updated_at": datetime.now(SHANGHAI_TZ).isoformat(),
                "total": len(self.codes),
                "completed": len(self.checkpoints),
                "success": success,
                "failed": len(self.checkpoints) - success,
                "reused": self.reused,
                "pending": len(self.codes) - len(self.checkpoints),
                "pending_codes": self._pending_codes(),
                "pending_failed_codes": self._pending_failed_codes(),
                "checkpoint_count": len(self.observed_checkpoints),
                "retry_schedule": self._retry_schedule(),
                "source_access": self.source_access,
                "current_code": code,
                "running": running,
                "running_codes": [item["code"] for item in running],
                "running_pids": {item["code"]: item["pid"] for item in running},
            },
        )

    def _command(self, code: str) -> list[str]:
        settings = self.config["point_in_time_data"]
        return [
            sys.executable,
            str(ROOT / "scripts" / "backfill_reference_universe.py"),
            "--config",
            str(self.config_path),
            "--manifest",
            str(self.manifest_path),
            "--output-dir",
            str(self.service.output_dir),
            "--workers",
            "1",
            "--history-years",
            str(settings["history_years"]),
            "--official-pdf-recent-years",
            str(settings["official_pdf_recent_years"]),
            "--evaluation-date",
            self.evaluation_date.isoformat(),
            "--codes",
            code,
            "--force",
        ]

    @staticmethod
    def _stop_child(process) -> None:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)

    def _report(self, interrupted: bool) -> dict:
        """Summarize checkpoints without calling any fetch or retry path."""
        rows = [
            self.observed_checkpoints[code]["row"]
            for code in self.codes
            if code in self.observed_checkpoints
        ]
        start = self.evaluation_date - timedelta(
            days=int(self.service.history_years * 365.25)
        )
        report = PointInTimeBackfillService._summary(start, self.evaluation_date, rows)
        failed = [
            code
            for code, cp in self.observed_checkpoints.items()
            if cp.get("status") != "success"
        ]
        state = (
            "source_blocked"
            if (self.source_access or {}).get("blocked")
            else "interrupted"
            if interrupted
            else "complete"
            if len(self.checkpoints) == len(self.codes) and not failed
            else "partial"
        )
        report.update(
            {
                "contract": REFERENCE_BATCH_CONTRACT,
                "batch_id": self.batch_id,
                "fingerprint": self.fingerprint,
                "manifest_contract": self.service.manifest.get("contract"),
                "manifest_hash": self.service.manifest_hash,
                "manifest_as_of": self.service.manifest.get("as_of"),
                "weight_as_of": self.service.manifest.get("weight_as_of"),
                "state": state,
                "worker_processes": self.workers,
                "requested_company_count": len(self.codes),
                "checkpoint_count": len(self.observed_checkpoints),
                "completed_company_count": len(self.checkpoints),
                "pending_company_count": len(self._pending_codes()),
                "pending_codes": self._pending_codes(),
                "pending_failed_codes": self._pending_failed_codes(),
                "retry_schedule": self._retry_schedule(),
                "source_access": self.source_access,
                "reused_checkpoint_count": self.reused,
                "failed_codes": sorted(failed),
                "batch_dir": str(self.batch_dir),
            }
        )
        path = self.batch_dir / "report.json"
        _atomic_json(path, report)
        _atomic_json(self.service.output_dir / "latest_reference_backfill.json", report)
        _atomic_json(
            self.batch_dir / "progress.json",
            {
                **self.service._progress(
                    batch_id=self.batch_id,
                    fingerprint=self.fingerprint,
                    total=len(self.codes),
                    checkpoints=self.checkpoints,
                    started_at=self.started_at,
                    state=state,
                ),
                "pending_codes": self._pending_codes(),
                "pending_failed_codes": self._pending_failed_codes(),
                "checkpoint_count": len(self.observed_checkpoints),
                "retry_schedule": self._retry_schedule(),
                "source_access": self.source_access,
            },
        )
        self._write_progress(state)
        report["output_file"] = str(path)
        return report

    def run(self) -> dict:
        with _dataset_lock(self.service.output_dir):
            return self._run_locked()

    def _run_locked(self) -> dict:
        self.checkpoints.clear()
        self.observed_checkpoints.clear()
        self.pending.clear()
        self.source_access = None
        self.source_wait_until = 0.0
        self.reused = 0
        self.started_at = datetime.now(SHANGHAI_TZ).isoformat()
        for code in self.codes:
            checkpoint = self._checkpoint(code)
            if checkpoint is not None:
                self.observed_checkpoints[code] = checkpoint
            if self._reusable(checkpoint):
                self.checkpoints[code] = checkpoint
                self.reused += 1
            else:
                row_access = (checkpoint or {}).get("row", {}).get("source_access")
                retry_at = (
                    row_access.get("retry_at") if isinstance(row_access, dict) else None
                )
                self.pending[code] = {
                    "attempt": 1,
                    "ready": time.monotonic() + self._retry_delay(retry_at),
                    "retry_at": retry_at,
                }
        self._write_progress("running")
        interrupted = False

        def finish(code: str, attempt: int, checkpoint: dict, elapsed: float) -> None:
            self.observed_checkpoints[code] = checkpoint
            status = checkpoint["status"]
            print(
                f"[{len(self.checkpoints)}/{len(self.codes)}] {code} "
                f"attempt={attempt} status={status} elapsed={elapsed:.1f}s",
                flush=True,
            )
            if status != "success" and attempt < self.attempts_per_run:
                self._queue_retry(code, attempt, checkpoint)
            else:
                self.checkpoints[code] = checkpoint
            self._write_progress("running", code)

        try:
            while self.pending or self.running:
                if not self._refresh_access():
                    break
                while self.pending and len(self.running) < self.workers:
                    if not self._refresh_access():
                        break
                    now = time.monotonic()
                    if now < self.source_wait_until:
                        break
                    code = next(
                        (
                            code
                            for code, item in self.pending.items()
                            if item["ready"] <= now
                        ),
                        None,
                    )
                    if code is None:
                        break
                    item = self.pending.pop(code)
                    attempt = item["attempt"]
                    previous_checkpoint = self._checkpoint(code)
                    started = time.monotonic()
                    try:
                        process = subprocess.Popen(
                            self._command(code),
                            cwd=ROOT,
                            stdout=subprocess.DEVNULL,
                            creationflags=(
                                subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                            ),
                        )
                    except OSError as exc:
                        if not self._refresh_access():
                            self.pending[code] = item
                            break
                        finish(code, attempt, self._failure(code, exc), 0)
                        continue
                    self.running[code] = {
                        "process": process,
                        "attempt": attempt,
                        "started": started,
                        "started_at": datetime.now(SHANGHAI_TZ).isoformat(),
                        "previous_checkpoint": previous_checkpoint,
                    }
                    self._write_progress("running", code)

                if (self.source_access or {}).get("blocked"):
                    break

                for code, child in list(self.running.items()):
                    process = child["process"]
                    elapsed = time.monotonic() - child["started"]
                    return_code = process.poll()
                    if return_code is None and elapsed < self.company_timeout_seconds:
                        continue
                    checkpoint = self._checkpoint(code)
                    if checkpoint is not None:
                        self.observed_checkpoints[code] = checkpoint
                    if not self._refresh_access(
                        checkpoint
                        if checkpoint != child["previous_checkpoint"]
                        else None
                    ):
                        break
                    if return_code is None:
                        self._stop_child(process)
                        if not self._refresh_access():
                            break
                        checkpoint = self._failure(
                            code,
                            TimeoutError(
                                f"company process exceeded {self.company_timeout_seconds} seconds"
                            ),
                            elapsed,
                        )
                    else:
                        if return_code != 0 and self._reusable(checkpoint):
                            checkpoint = self._failure(
                                code,
                                RuntimeError(f"child exited with code {return_code}"),
                                elapsed,
                            )
                        elif checkpoint is None:
                            checkpoint = self._failure(
                                code,
                                RuntimeError(
                                    f"child exited with code {return_code} without a matching checkpoint"
                                ),
                                elapsed,
                            )
                        elif checkpoint.get(
                            "status"
                        ) == "success" and not self._reusable(checkpoint):
                            checkpoint = self._failure(
                                code,
                                RuntimeError(
                                    "checkpoint market history ended before the allowed evaluation window"
                                ),
                                elapsed,
                            )
                    del self.running[code]
                    finish(code, child["attempt"], checkpoint, elapsed)
                if (self.source_access or {}).get("blocked"):
                    break
                if self.pending or self.running:
                    if (
                        not self.running
                        and self._last_progress_state != "waiting_retry"
                    ):
                        self._write_progress("waiting_retry")
                    time.sleep(0.2)
        except KeyboardInterrupt:
            interrupted = True
        finally:
            # A stopped supervisor must not leave download children behind.
            for code, child in list(self.running.items()):
                self._stop_child(child["process"])
                checkpoint = self._checkpoint(code)
                if not (self.source_access or {}).get("blocked"):
                    self._refresh_access(
                        checkpoint
                        if checkpoint != child["previous_checkpoint"]
                        else None
                    )
                if (self.source_access or {}).get("blocked"):
                    # Pausing a provider is not a failed company attempt. Keep the
                    # child's real checkpoint, including any cached market success.
                    if checkpoint is not None:
                        self.observed_checkpoints[code] = checkpoint
                    if self._reusable(checkpoint):
                        self.checkpoints[code] = checkpoint
                else:
                    checkpoint = self._failure(
                        code,
                        InterruptedError(
                            "supervisor stopped before company completion"
                        ),
                        time.monotonic() - child["started"],
                    )
                    self.checkpoints[code] = checkpoint
                    self.observed_checkpoints[code] = checkpoint
                del self.running[code]
            if interrupted:
                self._write_progress("interrupted")
        return self._report(interrupted)


def _interrupt(signum, frame) -> None:
    raise KeyboardInterrupt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--evaluation-date",
        type=date.fromisoformat,
        default=datetime.now(SHANGHAI_TZ).date(),
    )
    parser.add_argument("--history-years", type=int, default=None)
    parser.add_argument("--official-pdf-recent-years", type=int, default=None)
    parser.add_argument("--workers", type=int, default=1, choices=range(1, 5))
    parser.add_argument("--attempts-per-run", type=int, default=2)
    parser.add_argument("--company-timeout-seconds", type=int, default=1200)
    args = parser.parse_args()
    with Path(args.config).open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    try:
        config = scoped_config(
            config,
            history_years=args.history_years,
            official_pdf_recent_years=args.official_pdf_recent_years,
        )
        supervisor = IsolatedReferenceSupervisor(
            config,
            load_reference_manifest(args.manifest),
            config_path=Path(args.config),
            manifest_path=Path(args.manifest),
            output_dir=Path(args.output_dir),
            evaluation_date=args.evaluation_date,
            workers=args.workers,
            attempts_per_run=args.attempts_per_run,
            company_timeout_seconds=args.company_timeout_seconds,
        )
    except (TypeError, ValueError) as exc:
        parser.error(str(exc))
    previous_handler = signal.signal(signal.SIGTERM, _interrupt)
    try:
        report = supervisor.run()
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
    print(
        json.dumps(
            {
                "state": report["state"],
                "batch_id": report["batch_id"],
                "failed_codes": report["failed_codes"],
                "pending_company_count": report.get("pending_company_count", 0),
                "source_access": report.get("source_access"),
                "report": report["output_file"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return (
        130
        if report["state"] == "interrupted"
        else 0
        if report["state"] == "complete"
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
