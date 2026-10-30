"""Slow, depth-first A-share source acquisition; no model or research calls."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import re
import shutil
import signal
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import date, datetime, timezone
from pathlib import Path
from queue import Empty, Queue

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data.baostock_access import BaostockAccessBlocked
from src.data.dataset_catalog import DatasetCatalog
from src.data.dataset_documents import (
    DatasetDocumentWorker,
    DocumentBlocked,
    DocumentFailure,
    document_worker_lock,
)
from src.data.listing_dates import ListingDateStore
from src.data.point_in_time_backfill import PointInTimeBackfillService
from src.data.source_screening import complete_pending
from src.instruments.point_in_time import CninfoAnnualReportProvider

ROOT = Path(__file__).resolve().parents[1]
ENERGY = {"B06", "B07", "B11", "C25", "D44", "D45"}


class CampaignBlocked(RuntimeError):
    """Provider-wide access or resource failure; never bypass by skipping a company."""


class DocumentBatches:
    """One archive owner, at most three PDF downloads across all companies."""

    def __init__(self, worker):
        self.worker, self.lock = worker, threading.Lock()

    def run(self, **kwargs):
        with self.lock:
            return self.worker.run(**kwargs)


def check_campaign_failure(error):
    response = getattr(error, "response", None)
    if (
        isinstance(error, BaostockAccessBlocked)
        or (response is not None and response.status_code in {401, 403, 429})
        or re.search(
            r"(?:source_http_|HTTP(?: Error)?[: _-]*)(?:401|403|429)\b|disk_space_reserve_reached",
            str(error),
        )
    ):
        raise CampaignBlocked(str(error)) from error
    if isinstance(error, OSError) and not isinstance(error, requests.RequestException):
        raise error


def load(path):
    return json.loads(Path(path).read_text("utf-8-sig"))


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf8")
    temporary.replace(path)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def theme(company):
    industry = company.get("industry_code")
    if industry == "J66" and re.search("银行|商行|张家港行|长沙银", company["name"]):
        return "bank"
    if industry == "J68":
        return "insurance"
    return "energy" if industry in ENERGY else "remaining_a_shares"


def prepare(output, screening, codes=None, reuse_run=None, company_workers=3):
    if output.exists():
        raise ValueError("new output directory required")
    if type(company_workers) is not int or company_workers not in {1, 2, 3}:
        raise ValueError("company_workers must be 1, 2 or 3")
    universe = ROOT / "data/fundamental_quant/source_cache/universe.json"
    inventory = ROOT / "data/acceptance/ashare_inventory_20261024/inventory.json"
    source = load(universe)
    snapshot = load(screening)
    if snapshot.get("current_universe_as_of") != source["as_of"]:
        raise ValueError("screening/universe dates differ")
    rows = {r["code"]: r for r in snapshot["rows"]}
    if len(rows) != len(snapshot["rows"]):
        raise ValueError("duplicate screening code")
    universe_codes = {
        c["code"] for c in source["companies"] if c.get("market") == "a_share"
    }
    if set(rows) != universe_codes:
        raise ValueError(
            "screening must explicitly retain every A-share as passed/rejected/pending"
        )
    if codes and any(rows.get(c, {}).get("status") != "passed" for c in codes):
        raise ValueError("small batch only accepts screened-passed codes")
    companies = [
        dict(c, theme=theme(c))
        for c in source["companies"]
        if c.get("market") == "a_share"
        and rows[c["code"]]["status"] != "rejected"
        and (not codes or c["code"] in codes)
    ]
    order = {
        k: i
        for i, k in enumerate(["bank", "insurance", "energy", "remaining_a_shares"])
    }
    companies.sort(key=lambda c: (order[c["theme"]], c["code"]))
    config = {
        "provider_access": {
            "baostock": {
                "state_dir": "data/provider_state/baostock",
                "min_request_interval_seconds": 2,
                "daily_request_budget": 40000,
            }
        },
        "instrument_audit": {"timeout_seconds": 20},
        "point_in_time_data": {
            "output_dir": str(output / "dataset"),
            "official_statement_crawlers": False,
            "market_history": {"baostock_socket_timeout_seconds": 20},
        },
    }
    dependencies = [
        Path(__file__),
        universe,
        inventory,
        ROOT / "src/data/dataset_documents.py",
        ROOT / "src/data/source_screening.py",
        ROOT / "src/data/cninfo_reports.py",
        ROOT / "src/data/point_in_time_backfill.py",
        ROOT / "src/data/baostock_access.py",
        ROOT / "src/instruments/point_in_time.py",
    ]
    plan = {
        "years": list(range(2019, 2026)),
        "price_start": "2019-01-01",
        "price_end": "2026-10-23",
        "universe_as_of": source["as_of"],
        "companies": companies,
        "config": config,
        "min_interval": 5,
        "retry_seconds": [60, 300],
        "minimum_free_bytes": 10 * 1024**3,
        "max_document_bytes": None,
        "download_workers": 3,
        "company_workers": company_workers,
        "document_timeout_seconds": 600,
        "company_download_seconds": 2100,
        "completion_policy": "best_effort; local gaps are annotations, not queue barriers",
        "screening": str(output / "screening.json"),
        "screening_counts": snapshot["counts"],
        "field_completion": not bool(codes),
        "reuse_run": str(reuse_run) if reuse_run else None,
        "paid_api_allowed": False,
        "inventory": str(inventory),
        "frozen_files": {str(p): sha(p) for p in dependencies},
    }
    save(output / "screening.json", snapshot)
    if reuse_run:
        for relative in ["screening_fields", "dataset/listing_dates"]:
            source_dir = Path(reuse_run) / relative
            if source_dir.is_dir():
                shutil.copytree(source_dir, output / relative)
    plan["frozen_files"][str(output / "screening.json")] = sha(
        output / "screening.json"
    )
    save(output / "plan.json", plan)
    (output / "plan.sha256").write_text(sha(output / "plan.json"), "ascii")
    return collections.Counter(c["theme"] for c in companies)


def cached_report(catalog, code, year):
    """Use the latest catalogued full annual report, never a half-year/summary."""
    with catalog._connect() as db:
        candidates = db.execute(
            "SELECT DISTINCT d.* FROM documents d JOIN document_sources s ON d.document_id=s.document_id WHERE s.code=? ORDER BY d.publication_date DESC,d.document_id",
            (code,),
        ).fetchall()
    for row in candidates:
        title = re.sub(r"\s+|<[^>]+>", "", row["title"] or "")
        if re.search(rf"{year}年?年度报告", title) and "摘要" not in title:
            return catalog.document_jobs(job_ids=[row["document_id"]], limit=1)[
                "items"
            ][0]
    return None


def pdf_result(job):
    if not job:
        return None
    for artifact in job.get("artifact_paths", []):
        path = ROOT / artifact["relative_path"]
        if artifact["kind"] == "pdf" and path.is_file():
            with path.open("rb") as handle:
                if not handle.read(8).startswith(b"%PDF-"):
                    continue
            return {
                "status": "original_available",
                "document_id": job["job_id"],
                "pdf": str(path),
                "sha256": sha(path),
                "title": job["title"],
                "publication_date": job["publication_date"],
                "text_path": job.get("text_path"),
                "text_status": (job.get("download_result") or {}).get(
                    "status", "not_certified"
                ),
            }
    return None


def annual(catalog, worker, provider, company, year, folder, *, defer=False):
    code = company["code"]
    job = cached_report(catalog, code, year)
    found = pdf_result(job)
    if found:
        return found
    if job is None:
        try:
            report = provider.discover_report(code, "annual", year)
        except LookupError:
            listing = ListingDateStore(folder / "dataset").resolve(code)
            if listing.listing_date.year > year:
                return {
                    "status": "not_applicable_before_listing",
                    "listing": listing.as_dict(),
                }
            raise
        identity = [code, "annual", year, report["announcement_id"]]
        document_id = (
            "cninfo_"
            + hashlib.sha256(
                json.dumps(identity, ensure_ascii=True, separators=(",", ":")).encode()
            ).hexdigest()
        )
        catalog.register_document_source(
            document_id,
            report["source_url"],
            report["title"],
            report["publication_date"],
            code,
        )
        job = catalog.document_jobs(job_ids=[document_id], limit=1)["items"][0]
    if defer:
        return {"status": "download_pending", "document_id": job["job_id"]}
    worker.run(max_documents=1, max_seconds=660, job_ids=[job["job_id"]])
    return annual_result(catalog, job["job_id"], code, year)


def annual_result(catalog, job_id, code, year):
    job = catalog.document_jobs(job_ids=[job_id], limit=1)["items"][0]
    found = pdf_result(job)
    if not found:
        error = (
            job.get("download_error") or f"annual original unavailable: {code}/{year}"
        )
        if job.get("download_status") == "blocked":
            raise DocumentBlocked(error)
        raise DocumentFailure(
            error, max(0, (job.get("next_attempt_at") or 0) - time.time())
        )
    return found


def complete_valid(result, years):
    if result.get("status") != "complete" or set(result.get("years", {})) != {
        str(y) for y in years
    }:
        return False
    files = [
        v
        for v in result["years"].values()
        if v.get("status") != "not_applicable_before_listing"
    ]
    files.append(
        {
            "pdf": result.get("market", {}).get("path"),
            "sha256": result.get("market", {}).get("sha256"),
        }
    )
    files.append(
        {
            "pdf": result.get("financial", {}).get("path"),
            "sha256": result.get("financial", {}).get("sha256"),
        }
    )
    return all(
        v.get("pdf") and Path(v["pdf"]).is_file() and sha(v["pdf"]) == v.get("sha256")
        for v in files
    )


def processed_valid(result, years):
    if complete_valid(result, years):
        return True
    if (
        result.get("status") != "partial"
        or not result.get("gaps")
        or set(result.get("years", {})) != {str(y) for y in years}
    ):
        return False
    files = [
        (v["pdf"], v.get("sha256")) for v in result["years"].values() if v.get("pdf")
    ]
    files += [
        (v["path"], v.get("sha256"))
        for v in [result.get("market", {}), result.get("financial", {})]
        if v.get("path")
    ]
    return all(Path(path).is_file() and sha(path) == digest for path, digest in files)


def collect(plan, company, folder, catalog, worker, provider, stop, publish):
    code = company["code"]
    path = folder / "companies" / code / "progress.json"
    result = (
        load(path)
        if path.exists()
        else {
            "code": code,
            "name": company["name"],
            "theme": company["theme"],
            "years": {},
        }
    )
    result["status"] = "collecting"
    result["gaps"] = []
    save(path, result)
    pending = {}
    for year in plan["years"]:
        if stop.is_set():
            raise InterruptedError("stop requested")
        previous = result["years"].get(str(year), {})
        intact = previous.get("status") == "not_applicable_before_listing" or (
            previous.get("pdf")
            and Path(previous["pdf"]).is_file()
            and sha(previous["pdf"]) == previous.get("sha256")
        )
        if not intact:
            publish("annual", company, year=year)
            try:
                result["years"][str(year)] = annual(
                    catalog, worker, provider, company, year, folder, defer=True
                )
            except (
                LookupError,
                DocumentFailure,
                DocumentBlocked,
                requests.RequestException,
            ) as error:
                check_campaign_failure(error)
                result["years"][str(year)] = {
                    "status": "unavailable",
                    "error": f"{type(error).__name__}: {error}",
                }
            if result["years"][str(year)].get("status") == "download_pending":
                pending[year] = result["years"][str(year)]["document_id"]
            save(path, result)
    if pending:
        publish(
            "annual_downloads",
            company,
            years=list(pending),
            workers=plan.get("download_workers", 3),
        )
        receipt = worker.run(
            max_documents=len(pending),
            max_seconds=plan.get("company_download_seconds", 2100),
            job_ids=list(pending.values()),
            workers=plan.get("download_workers", 3),
        )
        save(folder / "companies" / code / "downloads.json", receipt)
        for year, job_id in pending.items():
            try:
                result["years"][str(year)] = annual_result(catalog, job_id, code, year)
            except (DocumentFailure, DocumentBlocked) as error:
                check_campaign_failure(error)
                result["years"][str(year)]["error"] = str(error)
            save(path, result)
    result["gaps"].extend(
        {
            "kind": "annual_original_unavailable",
            "year": year,
            "error": value.get("error", "original unavailable"),
        }
        for year, value in result["years"].items()
        if value.get("status")
        not in {"original_available", "not_applicable_before_listing"}
    )
    if stop.is_set() or (folder / "STOP").exists():
        raise InterruptedError("stop requested")
    publish("market_and_financial", company)
    interface_path = folder / "companies" / code / "interfaces.json"
    row = load(interface_path) if interface_path.exists() else {}
    reusable = (
        row.get("code") == code
        and all(
            row.get(key, {}).get("status") == "success"
            and Path(row[key].get("output", "")).is_file()
            for key in ["market_history", "statements"]
        )
        and row["market_history"].get("requested_start") == plan["price_start"]
        and row["market_history"].get("requested_end") == plan["price_end"]
    )
    if not reusable:
        service = PointInTimeBackfillService(plan["config"])
        # Independent source files remain useful even if another source has gaps.
        try:
            row = service._backfill_one(
                code,
                date.fromisoformat(plan["price_start"]),
                date.fromisoformat(plan["price_end"]),
            )
        except requests.RequestException as error:
            check_campaign_failure(error)
            row = {
                "code": code,
                "market_history": {"status": "failed", "reason": str(error)},
                "statements": {"status": "failed", "reason": str(error)},
            }
    save(folder / "companies" / code / "interfaces.json", row)
    if row.get("source_access", {}).get("blocked"):
        raise CampaignBlocked(str(row["source_access"]))
    for source, target in [("market_history", "market"), ("statements", "financial")]:
        value = row.get(source, {})
        check_campaign_failure(RuntimeError(value.get("reason", "")))
        available = bool(value.get("output") and Path(value["output"]).is_file())
        result[target] = {"status": value.get("status", "unavailable")}
        if available:
            result[target].update(path=value["output"], sha256=sha(value["output"]))
        if value.get("status") != "success" or not available:
            result["gaps"].append(
                {
                    "kind": f"{target}_unavailable",
                    "reason": value.get("reason", "source file unavailable"),
                }
            )
    prices = Path(result["market"]["path"]) if result["market"].get("path") else None
    import csv

    dates = []
    if prices:
        try:
            with prices.open(encoding="utf-8-sig", newline="") as handle:
                dates = sorted(
                    x["date"][:10]
                    for x in csv.DictReader(handle)
                    if float(x.get("raw_close") or x.get("close") or 0) > 0
                )
        except (ValueError, csv.Error) as error:
            result["gaps"].append(
                {"kind": "price_content_unreadable", "reason": str(error)}
            )
    listing = None
    if not dates or dates[0] > "2019-01-08":
        try:
            listing = ListingDateStore(folder / "dataset").resolve(code)
        except (LookupError, ValueError, requests.RequestException) as error:
            check_campaign_failure(error)
            result["gaps"].append(
                {"kind": "listing_date_unresolved", "reason": str(error)}
            )
    expected = max(
        date.fromisoformat(plan["price_start"]),
        listing.listing_date if listing else date.fromisoformat(plan["price_start"]),
    )
    if (
        not dates
        or (date.fromisoformat(dates[0]) - expected).days > 7
        or (date.fromisoformat(plan["price_end"]) - date.fromisoformat(dates[-1])).days
        > 7
    ):
        result["gaps"].append(
            {
                "kind": "price_window_partial",
                "expected_start": expected.isoformat(),
                "actual_start": dates[0] if dates else None,
                "expected_end": plan["price_end"],
                "actual_end": dates[-1] if dates else None,
            }
        )
    result["market"].update(
        first=dates[0] if dates else None,
        last=dates[-1] if dates else None,
        listing=listing.as_dict() if listing else None,
    )
    result.update(
        status="partial" if result["gaps"] else "complete",
        financial_status=row.get("statements", {}).get("status", "unavailable"),
        attempted_at=datetime.now(timezone.utc).isoformat(),
    )
    save(path, result)
    return result


def run(folder):
    plan_path = folder / "plan.json"
    plan = load(plan_path)
    if (
        sha(plan_path) != (folder / "plan.sha256").read_text("ascii").strip()
        or any(sha(p) != h for p, h in plan["frozen_files"].items())
        or plan["paid_api_allowed"]
    ):
        raise ValueError("frozen plan/source changed or paid calls enabled")
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    catalog = DatasetCatalog(ROOT)
    worker = DatasetDocumentWorker(
        catalog,
        stop=stop,
        min_interval=plan["min_interval"],
        max_document_bytes=plan.get("max_document_bytes"),
        minimum_free_bytes=plan["minimum_free_bytes"],
        document_timeout_seconds=plan.get("document_timeout_seconds", 600),
    )
    worker._initialize()
    downloads = DocumentBatches(worker)
    completed = {}
    partial = {}
    active = {}
    updates = Queue()

    def publish(state, company=None, **extra):
        if company:
            if state in {"company_complete", "company_partial"}:
                active.pop(company["code"], None)
            elif (
                state
                in {
                    "company_started",
                    "annual",
                    "annual_downloads",
                    "market_and_financial",
                    "retry_wait",
                }
                and company["code"] not in completed
                and company["code"] not in partial
            ):
                active[company["code"]] = dict(
                    code=company["code"],
                    name=company["name"],
                    theme=company["theme"],
                    state=state,
                    **extra,
                )
        counts = collections.Counter(
            c["theme"] for c in plan["companies"] if c["code"] in completed
        )
        required = collections.Counter(c["theme"] for c in plan["companies"])
        partial_counts = collections.Counter(
            c["theme"] for c in plan["companies"] if c["code"] in partial
        )
        themes_complete = all(
            counts[k] == required[k] for k in ["bank", "insurance", "energy"]
        )
        record = dict(
            state=state,
            pid=os.getpid(),
            updated_at=datetime.now(timezone.utc).isoformat(),
            current={k: company.get(k) for k in ["code", "name", "theme"]}
            if company
            else None,
            active_companies=list(active.values()),
            company_workers=plan.get("company_workers", 1),
            completed=len(completed),
            processed=len(completed) + len(partial),
            partial=len(partial),
            total=len(plan["companies"]),
            screening=plan.get("screening_counts"),
            theme_completed=dict(counts),
            theme_partial=dict(partial_counts),
            theme_required=dict(required),
            themes_complete=themes_complete,
            themes_processed=all(
                counts[k] + partial_counts[k] == required[k]
                for k in ["bank", "insurance", "energy"]
            ),
            paid_api_calls=0,
            **extra,
        )
        save(folder / "status.json", record)
        with (folder / "events.jsonl").open("a", encoding="utf8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def enqueue(state, company=None, **extra):
        updates.put((state, company, dict(extra, thread_id=threading.get_ident())))

    def drain_updates():
        while True:
            try:
                state, company, extra = updates.get_nowait()
            except Empty:
                return
            publish(state, company, **extra)

    def do_company(company):
        # A requests.Session belongs to one company task, never three threads.
        provider = CninfoAnnualReportProvider({})
        original_request = provider.http.request

        def limited_request(*args, **kwargs):
            worker._wait_request(time.monotonic() + 180)
            return original_request(*args, **kwargs)

        provider.http.request = limited_request
        try:
            for attempt in range(len(plan["retry_seconds"]) + 1):
                if stop.is_set() or (folder / "STOP").exists():
                    raise InterruptedError("stop requested")
                if shutil.disk_usage(folder).free < plan["minimum_free_bytes"]:
                    raise CampaignBlocked("disk_space_reserve_reached")
                try:
                    return collect(
                        plan,
                        company,
                        folder,
                        catalog,
                        downloads,
                        provider,
                        stop,
                        enqueue,
                    )
                except (DocumentFailure, requests.RequestException) as error:
                    check_campaign_failure(error)
                    if attempt == len(plan["retry_seconds"]):
                        raise
                    delay = max(
                        plan["retry_seconds"][attempt], getattr(error, "retry_after", 0)
                    )
                    enqueue(
                        "retry_wait",
                        company,
                        attempt=attempt + 1,
                        error=f"{type(error).__name__}: {error}",
                        retry_at=time.time() + delay,
                    )
                    until = time.monotonic() + delay
                    while (
                        time.monotonic() < until
                        and not stop.is_set()
                        and not (folder / "STOP").exists()
                    ):
                        stop.wait(min(1, until - time.monotonic()))
        finally:
            getattr(provider.http, "close", lambda: None)()

    with document_worker_lock(folder / "campaign.lock") as acquired:
        if not acquired:
            raise RuntimeError("campaign already running")
        if stop.is_set() or (folder / "STOP").exists():
            publish("stopped")
            return 2
        if plan.get("field_completion"):
            try:
                screened = complete_pending(
                    load(plan["screening"]), folder / "screening_fields", stop, publish
                )
            except InterruptedError:
                publish("stopped")
                return 2
            except Exception as error:  # noqa: BLE001 - persist source block without retrying the field queue
                publish(
                    "blocked_screening_source", error=f"{type(error).__name__}: {error}"
                )
                return 2
            screened["counts"] = dict(
                collections.Counter(r["status"] for r in screened["rows"])
            )
            save(folder / "screening_resolved.json", screened)
            passed = {r["code"] for r in screened["rows"] if r["status"] == "passed"}
            plan["companies"] = [c for c in plan["companies"] if c["code"] in passed]
            plan["screening_counts"] = screened["counts"]
        if plan.get("reuse_run"):
            for company in plan["companies"]:
                code = company["code"]
                target = folder / "companies" / code
                old = Path(plan["reuse_run"]) / "companies" / code
                if (
                    not (target / "progress.json").exists()
                    and (old / "progress.json").exists()
                ):
                    # Copy progress into this run; original files and hashes remain authoritative.
                    save(target / "progress.json", load(old / "progress.json"))
                    if (old / "interfaces.json").exists():
                        shutil.copyfile(
                            old / "interfaces.json", target / "interfaces.json"
                        )
        for company in plan["companies"]:
            path = folder / "companies" / company["code"] / "progress.json"
            if path.exists() and complete_valid(load(path), plan["years"]):
                completed[company["code"]] = True
            elif path.exists() and processed_valid(load(path), plan["years"]):
                partial[company["code"]] = True
        publish("running")
        remaining = iter(
            c
            for c in plan["companies"]
            if c["code"] not in completed and c["code"] not in partial
        )
        company_workers = plan.get("company_workers", 1)
        if type(company_workers) is not int or company_workers not in {1, 2, 3}:
            raise ValueError("company_workers must be 1, 2 or 3")
        failure = None
        with ThreadPoolExecutor(
            max_workers=company_workers, thread_name_prefix="source-company"
        ) as pool:
            futures = {}
            exhausted = False
            while futures or not exhausted:
                if (folder / "STOP").exists():
                    stop.set()
                while (
                    not exhausted
                    and not stop.is_set()
                    and len(futures) < company_workers
                ):
                    company = next(remaining, None)
                    if company is None:
                        exhausted = True
                        break
                    publish("company_started", company)
                    futures[pool.submit(do_company, company)] = company
                if stop.is_set():
                    exhausted = True
                drain_updates()
                if not futures:
                    break
                done, _ = wait(futures, timeout=0.25, return_when=FIRST_COMPLETED)
                drain_updates()
                for future in done:
                    company = futures.pop(future)
                    try:
                        result = future.result()
                    except InterruptedError:
                        active.pop(company["code"], None)
                        stop.set()
                    except Exception as error:  # noqa: BLE001 - preserve the first global failure and drain active tasks
                        if failure is None:
                            failure = (company, f"{type(error).__name__}: {error}")
                        active.pop(company["code"], None)
                        stop.set()
                        publish("stopping", company, error=failure[1])
                    else:
                        if result["status"] == "partial":
                            partial[company["code"]] = True
                            publish("company_partial", company, gaps=result["gaps"])
                        else:
                            completed[company["code"]] = True
                            publish("company_complete", company)
        if failure:
            active.clear()
            publish("blocked", failure[0], error=failure[1])
            return 2
        if stop.is_set() or (folder / "STOP").exists():
            active.clear()
            publish("stopped")
            return 2
        publish("complete_with_gaps" if partial else "complete")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "run", "status"])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--company-workers",
        type=int,
        choices=[1, 2, 3],
        default=3,
        help="Company concurrency frozen by prepare; PDFs remain globally bounded to 3",
    )
    parser.add_argument(
        "--screening",
        type=Path,
        help="Frozen A-share screening JSON; required for prepare",
    )
    parser.add_argument(
        "--company", action="append", help="Passed codes only; small-batch acceptance"
    )
    parser.add_argument(
        "--reuse-run", type=Path, help="Read-only reuse of existing company progress"
    )
    arguments = parser.parse_args()
    output = arguments.output.resolve()
    if arguments.action == "prepare":
        if arguments.screening is None:
            parser.error(
                "prepare requires --screening; unscreened full-market download is retired"
            )
        print(
            json.dumps(
                prepare(
                    output,
                    arguments.screening.resolve(),
                    arguments.company,
                    arguments.reuse_run.resolve() if arguments.reuse_run else None,
                    arguments.company_workers,
                )
            )
        )
    elif arguments.action == "status":
        print(json.dumps(load(output / "status.json"), ensure_ascii=False, indent=2))
    else:
        raise SystemExit(run(output))
