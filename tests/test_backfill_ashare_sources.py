"""Offline acceptance of depth-first source acquisition and completion claims."""

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace

import pytest

from scripts import backfill_ashare_sources as task


@pytest.fixture
def collection_inputs(tmp_path, monkeypatch):
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-1.7 offline source fixture")
    prices = tmp_path / "prices.csv"
    prices.write_text("date,raw_close\n2019-01-02,5\n2026-10-23,6\n", encoding="utf8")
    financial = tmp_path / "financial.json"
    task.save(financial, {"fixture": True})
    row = {
        "code": "000001",
        "market_history": {
            "status": "success",
            "output": str(prices),
            "requested_start": "2019-01-01",
            "requested_end": "2026-10-23",
        },
        "statements": {"status": "success", "output": str(financial)},
    }
    plan = {
        "years": [2019, 2020, 2021],
        "price_start": "2019-01-01",
        "price_end": "2026-10-23",
        "config": {},
    }
    company = {"code": "000001", "name": "bank", "theme": "bank"}
    item = {"status": "original_available", "pdf": str(pdf), "sha256": task.sha(pdf)}
    monkeypatch.setattr(task, "annual", lambda *a, **k: dict(item))
    monkeypatch.setattr(
        task,
        "PointInTimeBackfillService",
        lambda _: SimpleNamespace(_backfill_one=lambda *a: row),
    )
    listing = SimpleNamespace(
        listing_date=date(2019, 3, 1), as_dict=lambda: {"listing_date": "2019-03-01"}
    )
    monkeypatch.setattr(
        task, "ListingDateStore", lambda _: SimpleNamespace(resolve=lambda _: listing)
    )
    return plan, company, row, prices, item


def test_priority_excludes_nonbank_financials_and_covers_energy():
    for name in ["张家港行", "青农商行", "XD长沙银", "平安银行"]:
        assert task.theme({"industry_code": "J66", "name": name}) == "bank"
    assert (
        task.theme({"industry_code": "J66", "name": "江苏金租"}) == "remaining_a_shares"
    )
    assert task.theme({"industry_code": "J68", "name": "中粮资本"}) == "insurance"
    assert all(
        task.theme({"industry_code": c, "name": "x"}) == "energy" for c in task.ENERGY
    )


def test_complete_marker_requires_all_years_and_intact_source_files(tmp_path):
    pdf = tmp_path / "original.pdf"
    pdf.write_bytes(b"%PDF-1.7 real fixture bytes")
    item = {"pdf": str(pdf), "sha256": task.sha(pdf)}
    market = {"path": str(pdf), "sha256": task.sha(pdf)}
    result = {
        "status": "complete",
        "years": {str(y): item for y in range(2019, 2026)},
        "market": market,
        "financial": market,
    }
    assert task.complete_valid(result, range(2019, 2026))
    del result["years"]["2020"]
    assert not task.complete_valid(result, range(2019, 2026))
    result["years"]["2020"] = item
    pdf.write_bytes(b"changed")
    assert not task.complete_valid(result, range(2019, 2026))


def test_catalog_latest_full_report_wins_over_half_year_and_summary():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        "CREATE TABLE documents(document_id,title,publication_date); CREATE TABLE document_sources(document_id,code);"
    )
    for i, title in enumerate(
        [
            "2025年年度报告",
            "2025年半年度报告",
            "2025年年度报告摘要",
            "2025年年度报告（修订版）",
        ]
    ):
        db.execute(
            "INSERT INTO documents VALUES (?,?,?)",
            (str(i), title, f"2026-04-{i + 1:02}"),
        )
        db.execute("INSERT INTO document_sources VALUES (?,?)", (str(i), "000001"))
    catalog = SimpleNamespace(
        _connect=lambda: db,
        document_jobs=lambda **kw: {"items": [{"id": kw["job_ids"][0]}]},
    )
    assert task.cached_report(catalog, "000001", 2025) == {"id": "3"}
    assert task.cached_report(catalog, "000001", 2024) is None


def test_missing_report_is_not_silently_not_applicable(tmp_path, monkeypatch):
    monkeypatch.setattr(task, "cached_report", lambda *a: None)
    provider = SimpleNamespace(
        discover_report=lambda *a: (_ for _ in ()).throw(LookupError("not found"))
    )
    evidence = SimpleNamespace(
        listing_date=date(2010, 1, 1), as_dict=lambda: {"listing_date": "2010-01-01"}
    )
    monkeypatch.setattr(
        task, "ListingDateStore", lambda _: SimpleNamespace(resolve=lambda _: evidence)
    )
    with pytest.raises(LookupError):
        task.annual(None, None, provider, {"code": "000001"}, 2019, tmp_path)
    evidence.listing_date = date(2021, 1, 1)
    assert (
        task.annual(None, None, provider, {"code": "000001"}, 2019, tmp_path)["status"]
        == "not_applicable_before_listing"
    )


def test_company_failure_preserves_earlier_year_and_never_starts_next(
    tmp_path, monkeypatch
):
    calls = []

    def annual(*args, **kwargs):
        year = args[-2]
        calls.append(year)
        if year == 2020:
            raise RuntimeError("network unavailable")
        return {"status": "original_available", "pdf": "fixture", "sha256": "fixture"}

    monkeypatch.setattr(task, "annual", annual)
    with pytest.raises(RuntimeError):
        task.collect(
            {"years": [2019, 2020, 2021]},
            {"code": "000001", "name": "平安银行", "theme": "bank"},
            tmp_path,
            None,
            None,
            None,
            threading.Event(),
            lambda *a, **k: None,
        )
    saved = task.load(tmp_path / "companies/000001/progress.json")
    assert calls == [2019, 2020]
    assert set(saved["years"]) == {"2019"}
    assert saved["status"] == "collecting"


def test_campaign_blocks_on_first_company_and_does_not_claim_theme_complete(
    tmp_path, monkeypatch
):
    companies = [
        {"code": code, "name": code, "theme": "bank"} for code in ["000001", "001227"]
    ]
    plan = {
        "companies": companies,
        "years": list(range(2019, 2026)),
        "frozen_files": {},
        "paid_api_allowed": False,
        "min_interval": 5,
        "retry_seconds": [],
        "minimum_free_bytes": 1,
    }
    task.save(tmp_path / "plan.json", plan)
    (tmp_path / "plan.sha256").write_text(task.sha(tmp_path / "plan.json"), "ascii")
    monkeypatch.setattr(task, "DatasetCatalog", lambda _: None)
    monkeypatch.setattr(
        task,
        "DatasetDocumentWorker",
        lambda *a, **k: SimpleNamespace(_initialize=lambda: None),
    )
    monkeypatch.setattr(
        task,
        "CninfoAnnualReportProvider",
        lambda _: SimpleNamespace(http=SimpleNamespace(request=lambda: None)),
    )
    monkeypatch.setattr(task.signal, "signal", lambda *a: None)

    @contextmanager
    def lock(_):
        yield True

    monkeypatch.setattr(task, "document_worker_lock", lock)
    started = []

    def fail(plan, company, *args):
        started.append(company["code"])
        raise RuntimeError("source blocked")

    monkeypatch.setattr(task, "collect", fail)
    assert task.run(tmp_path) == 2
    result = json.loads((tmp_path / "status.json").read_text("utf8"))
    assert started == ["000001"]
    assert result["state"] == "blocked"
    assert result["themes_complete"] is False
    assert result["paid_api_calls"] == 0


def test_parallel_company_batch_preserves_success_and_permanent_failure(
    tmp_path, monkeypatch, collection_inputs
):
    calls = []
    monkeypatch.setattr(
        task,
        "annual",
        lambda *a, **k: {"status": "download_pending", "document_id": str(a[-2])},
    )

    def result(catalog, job_id, *args):
        if job_id == "2020":
            raise task.DocumentBlocked("source_http_404")
        return {"status": "original_available", "pdf": "saved", "sha256": "preserved"}

    monkeypatch.setattr(task, "annual_result", result)
    worker = SimpleNamespace(run=lambda **kw: calls.append(kw) or {"processed": []})
    result = task.collect(
        collection_inputs[0],
        {"code": "000001", "name": "bank", "theme": "bank"},
        tmp_path,
        None,
        worker,
        None,
        threading.Event(),
        lambda *a, **k: None,
    )
    assert result["status"] == "partial"
    assert result["gaps"][0]["year"] == "2020"
    assert calls == [
        {
            "max_documents": 3,
            "max_seconds": 2100,
            "job_ids": ["2019", "2020", "2021"],
            "workers": 3,
        }
    ]
    saved = task.load(tmp_path / "companies/000001/progress.json")
    assert saved["years"]["2019"]["pdf"] == "saved"
    assert saved["years"]["2021"]["pdf"] == "saved"
    assert saved["years"]["2020"]["error"] == "source_http_404"


def test_provider_rate_limit_still_blocks_campaign(tmp_path, monkeypatch):
    monkeypatch.setattr(
        task,
        "annual",
        lambda *a, **k: {"status": "download_pending", "document_id": str(a[-2])},
    )

    def result(catalog, job_id, *args):
        raise task.DocumentFailure("source_http_429", 3600 if job_id == "2020" else 60)

    monkeypatch.setattr(task, "annual_result", result)
    with pytest.raises(task.CampaignBlocked, match="429"):
        task.collect(
            {"years": [2019, 2020]},
            {"code": "000001", "name": "bank", "theme": "bank"},
            tmp_path,
            None,
            SimpleNamespace(run=lambda **kw: {}),
            None,
            threading.Event(),
            lambda *a, **k: None,
        )


def test_price_start_gap_is_retained_and_does_not_retry_interfaces(
    tmp_path, monkeypatch, collection_inputs
):
    plan, company, row, prices, _ = collection_inputs
    prices.write_text("date,raw_close\n2019-03-11,5\n2026-10-23,6\n", encoding="utf8")
    task.save(tmp_path / "companies/000001/interfaces.json", row)
    monkeypatch.setattr(
        task,
        "PointInTimeBackfillService",
        lambda _: pytest.fail("saved interfaces must be reused"),
    )
    result = task.collect(
        plan,
        company,
        tmp_path,
        None,
        None,
        None,
        threading.Event(),
        lambda *a, **k: None,
    )
    assert result["status"] == "partial"
    assert result["market"]["first"] == "2019-03-11"
    assert result["gaps"] == [
        {
            "kind": "price_window_partial",
            "expected_start": "2019-03-01",
            "actual_start": "2019-03-11",
            "expected_end": "2026-10-23",
            "actual_end": "2026-10-23",
        }
    ]
    assert task.processed_valid(result, plan["years"])
    assert not task.complete_valid(result, plan["years"])
    prices.write_text("changed", encoding="utf8")
    assert not task.processed_valid(result, plan["years"])


def test_missing_annual_does_not_hide_later_years_or_interfaces(
    tmp_path, monkeypatch, collection_inputs
):
    plan, company, _, _, item = collection_inputs
    calls = []

    def annual(*args, **kwargs):
        year = args[-2]
        calls.append(year)
        if year == 2020:
            raise LookupError("annual not found")
        return dict(item)

    monkeypatch.setattr(task, "annual", annual)
    result = task.collect(
        plan,
        company,
        tmp_path,
        None,
        None,
        None,
        threading.Event(),
        lambda *a, **k: None,
    )
    assert calls == [2019, 2020, 2021]
    assert result["years"]["2021"]["pdf"] == item["pdf"]
    assert result["financial"]["status"] == "success"
    assert result["status"] == "partial"
    assert task.processed_valid(result, plan["years"])


def test_missing_financial_keeps_market_and_annuals(tmp_path, collection_inputs):
    plan, company, row, _, _ = collection_inputs
    row["statements"] = {"status": "failed", "reason": "empty free interface"}
    result = task.collect(
        plan,
        company,
        tmp_path,
        None,
        None,
        None,
        threading.Event(),
        lambda *a, **k: None,
    )
    assert result["status"] == "partial"
    assert result["market"]["path"]
    assert result["years"]["2019"]["pdf"]
    assert result["gaps"] == [
        {"kind": "financial_unavailable", "reason": "empty free interface"}
    ]


def test_baostock_blacklist_not_bypassed_by_best_effort(tmp_path, collection_inputs):
    plan, company, row, _, _ = collection_inputs
    row["source_access"] = {"blocked": True, "reason": "blacklist"}
    with pytest.raises(task.CampaignBlocked, match="blacklist"):
        task.collect(
            plan,
            company,
            tmp_path,
            None,
            None,
            None,
            threading.Event(),
            lambda *a, **k: None,
        )


def test_campaign_advances_after_partial_and_reuses_it_on_restart(
    tmp_path, monkeypatch, collection_inputs
):
    base, _, _, prices, item = collection_inputs
    companies = [
        {"code": code, "name": code, "theme": "bank"} for code in ["000001", "001227"]
    ]
    plan = dict(
        base,
        companies=companies,
        frozen_files={},
        paid_api_allowed=False,
        min_interval=5,
        retry_seconds=[],
        minimum_free_bytes=1,
    )
    task.save(tmp_path / "plan.json", plan)
    (tmp_path / "plan.sha256").write_text(task.sha(tmp_path / "plan.json"), "ascii")
    monkeypatch.setattr(task, "DatasetCatalog", lambda _: None)
    monkeypatch.setattr(
        task,
        "DatasetDocumentWorker",
        lambda *a, **k: SimpleNamespace(_initialize=lambda: None),
    )
    monkeypatch.setattr(
        task,
        "CninfoAnnualReportProvider",
        lambda _: SimpleNamespace(http=SimpleNamespace(request=lambda: None)),
    )
    monkeypatch.setattr(task.signal, "signal", lambda *a: None)

    @contextmanager
    def lock(_):
        yield True

    monkeypatch.setattr(task, "document_worker_lock", lock)
    calls = []

    def collect(plan, company, folder, *args):
        calls.append(company["code"])
        result = dict(
            company,
            status="partial" if company["code"] == "000001" else "complete",
            years={str(y): item for y in plan["years"]},
            market={"path": str(prices), "sha256": task.sha(prices)},
            financial={"path": str(prices), "sha256": task.sha(prices)},
            gaps=[{"kind": "price_window_partial"}]
            if company["code"] == "000001"
            else [],
        )
        task.save(folder / "companies" / company["code"] / "progress.json", result)
        return result

    monkeypatch.setattr(task, "collect", collect)
    assert task.run(tmp_path) == 0
    assert calls == ["000001", "001227"]
    result = task.load(tmp_path / "status.json")
    assert (result["processed"], result["completed"], result["partial"]) == (2, 1, 1)
    assert result["state"] == "complete_with_gaps"
    assert result["themes_processed"] is True
    assert result["themes_complete"] is False
    monkeypatch.setattr(
        task,
        "collect",
        lambda *a: pytest.fail("intact partial result should not be re-requested"),
    )
    assert task.run(tmp_path) == 0
