"""Bounded public-FR acquisition; never constructs a paid model transport."""

from __future__ import annotations

import json
import re
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from .fr_pack import write_json
from .session_sources import SOURCE_REQUEST_LEASE, SessionReportSource, SourceFailure

VERSION = "longitudinal-public-acquisition/1"


def acquisition_tasks(coverages: list) -> list:
    tasks = {}
    for coverage in coverages:
        for row in coverage["rows"]:
            if row["status"] != "missing_original":
                continue
            kind = "half_year" if row["kind"] == "interim" else row["kind"]
            key = f"{row['issuer_id']}:{kind}:{row['period_end']}"
            tasks[key] = {
                "key": key,
                "issuer_id": row["issuer_id"],
                "company_name": row["company_name"],
                "report_type": kind,
                "year": int(row["period_end"][:4]),
                "period_end": row["period_end"],
            }
    return list(tasks.values())


class PublicAcquisition:
    """Persist the request cap/deadline before any network attempt.

    The cap counts document-source operations, not model calls or individual HTTP
    requests. Each operation uses the existing bounded discovery and rate ledger.
    Reopening the same ID never resets reservations, deadline or retry receipts.
    """

    def __init__(
        self, folder, run_id, as_of, max_documents, max_seconds, *, clock=time.time
    ):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", run_id):
            raise ValueError("invalid_public_acquisition_id")
        if not 1 <= max_documents <= 100 or not 1 <= max_seconds <= 3600:
            raise ValueError("invalid_public_acquisition_limits")
        date.fromisoformat(as_of)
        self.clock = clock
        self.path = Path(folder) / "acquisition" / f"{run_id}.json"
        limits = {"max_document_requests": max_documents, "max_seconds": max_seconds}
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf8"))
            if self.state["limits"] != limits or self.state["as_of"] != as_of:
                raise ValueError(
                    "public_acquisition_limits_immutable_use_new_explicit_id"
                )
        else:
            started = clock()
            self.state = {
                "version": VERSION,
                "id": run_id,
                "as_of": as_of,
                "limits": limits,
                "started_at": started,
                "expires_at": started + max_seconds,
                "reserved_document_requests": 0,
                "attempts": [],
                "status": "ready",
                "paid_model_calls": 0,
            }
            self.save()

    def save(self):
        write_json(self.path, self.state)

    def guard(self):
        if self.clock() >= self.state["expires_at"]:
            raise SourceFailure("public_document_acquisition_deadline")

    def run(self, tasks, source):
        self.state["requested_tasks"] = tasks
        self.state["status"] = "running"
        self.save()
        for task in tasks:
            prior = next(
                (
                    x
                    for x in reversed(self.state["attempts"])
                    if x["task"]["key"] == task["key"]
                ),
                None,
            )
            if prior:
                result = prior.get("result")
                if result is None:
                    # Interrupted operation has unknown network work. Its reservation
                    # stays charged; source layer owns its lease and recovery.
                    if prior["started_at"] + SOURCE_REQUEST_LEASE > self.clock():
                        self.state["status"] = "waiting_interrupted_source_lease"
                        self.state["retry_at"] = (
                            prior["started_at"] + SOURCE_REQUEST_LEASE
                        )
                        continue
                    result = {"retryable": True, "retry_at": 0}
                if result.get("status") == "success":
                    continue
                if (
                    not result.get("retryable")
                    or (result.get("retry_at") or 0) > self.clock()
                ):
                    continue
            if self.clock() >= self.state["expires_at"]:
                self.state["status"] = "deadline_reached"
                break
            if (
                self.state["reserved_document_requests"]
                >= self.state["limits"]["max_document_requests"]
            ):
                self.state["status"] = "document_request_limit_reached"
                break
            attempt = {"task": task, "started_at": self.clock(), "result": None}
            self.state["attempts"].append(attempt)
            self.state["reserved_document_requests"] += 1
            self.save()
            company = {
                "id": task["issuer_id"],
                "code": task["issuer_id"].split(":", 1)[1],
                "name": task["company_name"],
                "market": "a_share",
            }
            try:
                result = source.collect(company, task["report_type"], task["year"])
            except Exception as exc:  # noqa: BLE001 -- persist a visible boundary failure
                # Unclassified failures remain visible and are not blindly retried.
                result = {
                    "status": "unavailable",
                    "errors": [{"code": type(exc).__name__, "message": str(exc)}],
                    "retryable": False,
                    "next_action": "inspect_source_failure",
                }
            attempt.update(result=result, finished_at=self.clock())
            self.save()
            codes = {
                item.get("code")
                for item in result.get("errors", [])
                if isinstance(item, dict)
            }
            if "cninfo_http_throttled" in codes:
                self.state["status"] = "waiting_source_retry"
                break
        latest = {entry["task"]["key"]: entry for entry in self.state["attempts"]}
        pending = []
        for task in tasks:
            result = latest.get(task["key"], {}).get("result") or {}
            if result.get("status") != "success":
                pending.append({"task": task, "result": result})
        self.state["pending_tasks"] = pending
        self.state["successful_document_requests"] = sum(
            (entry.get("result") or {}).get("status") == "success"
            for entry in latest.values()
        )
        if self.state["status"] == "running":
            self.state["status"] = (
                "completed_with_source_gaps" if pending else "acquisition_pass_complete"
            )
        self.save()
        return self.state


def public_report_source(config, store, run):
    service = SimpleNamespace(
        store=store,
        root=store.root,
        acceptance=SimpleNamespace(enabled=False),
        config=config,
        settings=config.get("fundamental_quant", {}),
        today=date.fromisoformat(run.state["as_of"]),
        sources=SimpleNamespace(project_root=Path.cwd()),
    )

    class DeadlineSource(SessionReportSource):
        def _guard(self):
            run.guard()

        def _archive(self, catalog, worker, report):
            class BoundedWorker:
                def __getattr__(self, name):
                    return getattr(worker, name)

                def run(self, **kwargs):
                    run.guard()
                    kwargs["max_seconds"] = min(
                        kwargs.get("max_seconds", 180),
                        run.state["expires_at"] - run.clock(),
                    )
                    return worker.run(**kwargs)

            return super()._archive(catalog, BoundedWorker(), report)

    return DeadlineSource(service)
