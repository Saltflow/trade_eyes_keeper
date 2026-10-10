"""Offline targeted discovery/archive fixtures; not real source acceptance."""

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from src.fundamental_quant.session_sources import (
    SessionReportSource,
    SourceFailure,
    _Discovery,
)
from src.fundamental_quant.store import ResearchStore


@pytest.fixture
def service(tmp_path):
    return SimpleNamespace(
        root=tmp_path / "fq",
        store=ResearchStore(tmp_path / "fq"),
        sources=SimpleNamespace(project_root=tmp_path),
        settings={},
        config={},
        today=date(2026, 10, 6),
        acceptance=SimpleNamespace(
            enabled=True, state=lambda: {"status": "active", "limits": {}, "usage": {}}
        ),
    )


COMPANY = {"id": "a_share:600000", "code": "600000"}


def test_targeted_quarter_uses_archive_without_parsed_facts(service, monkeypatch):
    calls, archived = [], []

    def discover(code, kind, year, as_of):
        calls.append((code, kind, year, as_of))
        return {"code": code, "report_type": kind, "report_year": year}

    source = SessionReportSource(
        service,
        discovery=SimpleNamespace(discover_report=discover),
        archive=lambda report: archived.append(report),
    )
    monkeypatch.setattr(
        source,
        "_cached",
        lambda *args: [{"document_id": "raw-original"}] if archived else [],
    )
    result = source.collect(COMPANY, "third_quarter", 2025)
    assert result["status"] == "success"
    assert calls == [("600000", "third_quarter", 2025, service.today)]
    assert len(archived) == 1
    assert source.collect(COMPANY, "third_quarter", 2025)["status"] == "success"
    assert len(calls) == 1


def test_durable_failure_per_document_and_version(service, monkeypatch):
    calls = []

    def discover(*args):
        calls.append(args)
        raise SourceFailure("report_not_found")

    source = SessionReportSource(
        service, discovery=SimpleNamespace(discover_report=discover)
    )
    monkeypatch.setattr(source, "_cached", lambda *args: [])
    assert source.collect(COMPANY, "first_quarter", 2025)["status"] == "missing"
    assert source.collect(COMPANY, "first_quarter", 2025)["status"] == "missing"
    assert len(calls) == 1
    source.collect(COMPANY, "third_quarter", 2025)
    assert len(calls) == 2
    service.settings["source_revision"] = "fixed"
    source.collect(COMPANY, "first_quarter", 2025)
    assert len(calls) == 3


def test_provider_retry_after_is_persisted(service, monkeypatch):
    now, calls = [1000], []

    def discover(*args):
        calls.append(args)
        raise SourceFailure("cninfo_http_throttled", now[0] + 60)

    source = SessionReportSource(
        service,
        discovery=SimpleNamespace(discover_report=discover),
        clock=lambda: now[0],
    )
    monkeypatch.setattr(source, "_cached", lambda *args: [])
    assert source.collect(COMPANY, "annual", 2025)["retry_at"] == 1060
    source.collect(COMPANY, "annual", 2025)
    assert len(calls) == 1
    now[0] = 1061
    source.collect(COMPANY, "annual", 2025)
    assert len(calls) == 2


def test_unsupported_market_and_budget_are_visible(service, monkeypatch):
    source = SessionReportSource(service)
    assert (
        source.collect({"id": "hk:00700", "code": "00700"}, "annual", 2025)["errors"][
            0
        ]["code"]
        == "targeted_market_source_unavailable"
    )
    monkeypatch.setattr(source, "_cached", lambda *args: [])
    service.acceptance.state = lambda: {"status": "paused"}
    result = source.collect(COMPANY, "annual", 2025)
    assert result["errors"][0]["code"] == "active_research_budget_required"
    with service.store.transaction() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM pdf_report_source_attempts").fetchone()[0]
            == 0
        )


def test_discovery_filters_wrong_period_abstract_and_future():
    epoch = int(datetime(2025, 10, 30, tzinfo=timezone.utc).timestamp() * 1000)
    rows = [
        {
            "secCode": "600000",
            "announcementTitle": title,
            "announcementTime": timestamp,
            "adjunctUrl": "finalpage/2025/report.pdf",
            "announcementId": str(i),
        }
        for i, (title, timestamp) in enumerate(
            [
                ("测试2025年第三季度报告", epoch),
                ("测试2025年第三季度报告摘要", epoch + 1),
                ("测试2025年半年度报告", epoch + 2),
                ("测试2025年第三季度报告", 9999999999999),
            ]
        )
    ]
    response = SimpleNamespace(
        json=lambda: {"announcements": rows, "totalpages": 1}, close=lambda: None
    )
    discovery = _Discovery.__new__(_Discovery)
    discovery.provider = SimpleNamespace(
        _org_id=lambda code: "org",
        http=SimpleNamespace(post=lambda *a, **k: response),
        ANNOUNCEMENT_URL="https://www.cninfo.com.cn/query",
        STATIC_ROOT="https://static.cninfo.com.cn/",
        _headers={},
        timeout=20,
    )
    result = discovery.discover_report(
        "600000", "third_quarter", 2025, date(2026, 10, 6)
    )
    assert result["announcement_id"] == "0"
    assert result["publication_date"] == "2025-10-30"


def test_missing_source_metadata_not_financial_failure(service, monkeypatch):
    source = SessionReportSource(service)
    monkeypatch.setattr(
        source, "_cached", lambda *args: (_ for _ in ()).throw(ValueError("hash"))
    )
    assert source.collect(COMPANY, "annual", 2025)["status"] == "conflict"


@pytest.mark.parametrize("failure", ["network", "interrupt"])
def test_transient_failure_restarts_wait_and_exhaust_same_attempt_budget(
    service, monkeypatch, failure
):
    import requests

    now, calls = [1000], []

    def discover(*args):
        calls.append(args)
        if failure == "interrupt":
            raise KeyboardInterrupt("offline crash fixture")
        raise requests.ConnectionError("offline connection fixture")

    def source():
        item = SessionReportSource(
            service,
            discovery=SimpleNamespace(discover_report=discover),
            clock=lambda: now[0],
        )
        monkeypatch.setattr(item, "_cached", lambda *args: [])
        return item

    for attempt in range(1, 4):
        if failure == "interrupt":
            with pytest.raises(KeyboardInterrupt):
                source().collect(COMPANY, "annual", 2025)
        else:
            source().collect(COMPANY, "annual", 2025)
        # A new instance represents process restart; no new request before due.
        result = source().collect(COMPANY, "annual", 2025)
        assert len(calls) == attempt
        assert result["attempt_count"] == attempt
        if attempt < 3:
            assert result["retryable"] is True
            now[0] = result["retry_at"] + 1
        else:
            assert result["retryable"] is False
            now[0] += 10000
            source().collect(COMPANY, "annual", 2025)
            assert len(calls) == 3


def test_legacy_network_failure_migrates_without_immediate_request(
    service, monkeypatch
):
    import json

    from src.fundamental_quant.contracts import fingerprint

    source = SessionReportSource(service, clock=lambda: 1000)
    monkeypatch.setattr(source, "_cached", lambda *args: [])
    from src.fundamental_quant.session_sources import VERSION

    with service.store.transaction() as db:
        db.execute(
            "INSERT INTO pdf_report_source_attempts VALUES(?,?,?,?,?)",
            (
                fingerprint([COMPANY["id"], "annual", 2025]),
                fingerprint([VERSION, COMPANY, "1"]),
                json.dumps(
                    source._envelope(
                        "unavailable", error="document_source_network_error"
                    )
                ),
                None,
                900,
            ),
        )
    result = source.collect(COMPANY, "annual", 2025)
    assert result["retry_at"] == 1300
    assert result["attempt_count"] == 1
    assert source.collect(COMPANY, "annual", 2025) == result
