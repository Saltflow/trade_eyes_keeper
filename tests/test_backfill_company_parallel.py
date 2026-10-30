"""Company concurrency, central status writer and safe shutdown, without network."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from scripts import backfill_ashare_sources as task


@pytest.fixture
def campaign(tmp_path, monkeypatch):
    providers = []
    monkeypatch.setattr(task, "DatasetCatalog", lambda _: None)
    monkeypatch.setattr(
        task,
        "DatasetDocumentWorker",
        lambda *a, **k: SimpleNamespace(_initialize=lambda: None),
    )
    monkeypatch.setattr(task.signal, "signal", lambda *a: None)

    def provider(_):
        value = SimpleNamespace(http=SimpleNamespace(request=lambda *a, **k: None))
        providers.append(value)
        return value

    @contextmanager
    def lock(_):
        yield True

    monkeypatch.setattr(task, "CninfoAnnualReportProvider", provider)
    monkeypatch.setattr(task, "document_worker_lock", lock)

    def prepare(workers=3, count=6):
        companies = [
            {"code": f"{i:06}", "name": str(i), "theme": "bank"} for i in range(count)
        ]
        plan = {
            "companies": companies,
            "years": [2025],
            "frozen_files": {},
            "paid_api_allowed": False,
            "min_interval": 5,
            "retry_seconds": [],
            "minimum_free_bytes": 1,
            "company_workers": workers,
        }
        task.save(tmp_path / "plan.json", plan)
        (tmp_path / "plan.sha256").write_text(task.sha(tmp_path / "plan.json"), "ascii")
        return companies, providers

    return prepare


@pytest.mark.parametrize("workers", [2, 3])
def test_company_overlap_partial_continues_and_only_main_writes_status(
    tmp_path, monkeypatch, campaign, workers
):
    companies, providers = campaign(workers)
    barrier, guard = threading.Barrier(workers), threading.Lock()
    active, peak, calls, writers = 0, 0, [], set()
    original_save = task.save

    def save(path, value):
        if path.name == "status.json":
            writers.add(threading.get_ident())
        original_save(path, value)

    def collect(plan, company, folder, catalog, downloads, provider, stop, publish):
        nonlocal active, peak
        with guard:
            calls.append(company["code"])
            active += 1
            peak = max(peak, active)
            first = len(calls) <= workers
        if first:
            barrier.wait(5)
        for year in range(4):
            publish("annual", company, year=year)
            time.sleep(0.01)
        with guard:
            active -= 1
        return {
            "status": "partial" if company["code"] == "000000" else "complete",
            "gaps": [{"kind": "price_window_partial"}],
        }

    monkeypatch.setattr(task, "save", save)
    monkeypatch.setattr(task, "collect", collect)
    assert task.run(tmp_path) == 0
    assert peak == workers and len(calls) == len(companies)
    assert writers == {threading.get_ident()}
    assert len(providers) == len(companies) and len(
        {id(p.http) for p in providers}
    ) == len(companies)
    events = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text("utf8").splitlines()
    ]
    assert max(len(e["active_companies"]) for e in events) == workers
    result = events[-1]
    assert (result["processed"], result["completed"], result["partial"]) == (6, 5, 1)
    assert result["state"] == "complete_with_gaps" and result["active_companies"] == []


def test_global_block_drains_running_tasks_without_starting_more(
    tmp_path, monkeypatch, campaign
):
    campaign()
    barrier = threading.Barrier(3)
    calls = []

    def collect(plan, company, folder, catalog, downloads, provider, stop, publish):
        calls.append(company["code"])
        barrier.wait(5)
        if company["code"] == "000000":
            raise task.CampaignBlocked("source_http_403")
        assert stop.wait(5)
        raise InterruptedError("global failure stopped scheduling")

    monkeypatch.setattr(task, "collect", collect)
    assert task.run(tmp_path) == 2
    assert set(calls) == {"000000", "000001", "000002"}
    result = task.load(tmp_path / "status.json")
    assert result["state"] == "blocked" and "403" in result["error"]
    assert result["active_companies"] == []


def test_stop_file_stops_all_company_tasks_and_does_not_schedule_next(
    tmp_path, monkeypatch, campaign
):
    campaign()
    barrier = threading.Barrier(3)
    calls = []

    def collect(plan, company, folder, catalog, downloads, provider, stop, publish):
        calls.append(company["code"])
        barrier.wait(5)
        if company["code"] == "000000":
            (folder / "STOP").write_text("user stop", encoding="utf8")
        assert stop.wait(5)
        raise InterruptedError("stop requested")

    monkeypatch.setattr(task, "collect", collect)
    assert task.run(tmp_path) == 2
    assert len(calls) == 3
    result = task.load(tmp_path / "status.json")
    assert result["state"] == "stopped" and result["active_companies"] == []


def test_document_batches_share_one_archive_owner():
    guard = threading.Lock()
    active, peak, calls = 0, 0, []

    def run(**kwargs):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
            calls.append(kwargs)
        time.sleep(0.03)
        with guard:
            active -= 1
        return {"processed": kwargs["job_ids"]}

    batches = task.DocumentBatches(SimpleNamespace(run=run))
    with ThreadPoolExecutor(3) as pool:
        results = list(
            pool.map(lambda i: batches.run(job_ids=[str(i)], workers=3), range(3))
        )
    assert peak == 1 and len(results) == len(calls) == 3
    assert all(c["workers"] == 3 for c in calls)
