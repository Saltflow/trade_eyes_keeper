from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest

from src.data_hub.materials import DataHub, MaterialError
from src.instruments.point_in_time import (
    CninfoAnnualReportProvider,
    HkexStatementProvider,
)
from src.shared_contracts import AuthorityRef


class HTTP:
    def __init__(self, authority):
        self.authority = authority
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)

        class Response:
            content = b"%PDF-1.7\nfixed source boundary fixture"

            def raise_for_status(self):
                return None

        return Response()


def test_hk_native_financial_projection_cross_projects_once(tmp_path, monkeypatch):
    authority = AuthorityRef(tmp_path / "authority")
    clients = [HTTP(authority), HTTP(authority)]
    providers = [
        HkexStatementProvider({"project_root": str(tmp_path / name)}, http=client)
        for name, client in zip(("a", "b"), clients)
    ]
    text = """Consolidated statement of profit or loss
RMB'000
Year ended 31 December 2024
Revenue 373,710,000
Profit attributable to owners of the Company 19,272,000
Consolidated statement of financial position
Equity attributable to owners 120,000,000
Consolidated statement of cash flows
Net cash generated from operating activities 50,000,000
Purchase of property, plant and equipment (8,000,000)
Basic earnings per share 1.20
Diluted earnings per share 1.18"""
    calls = []

    def extract(cls, content):
        calls.append(content)
        return text, "fixed-native-layout"

    monkeypatch.setattr(
        HkexStatementProvider, "_extract_pdf_text", classmethod(extract)
    )
    url = "https://www1.hkexnews.hk/reports/fixed.pdf"
    document = {"url": url, "kind": "year", "published_at": date(2025, 4, 15)}
    for provider in providers:
        monkeypatch.setattr(provider, "_announcements", lambda *_args: ([document], []))
    with ThreadPoolExecutor(2) as executor:
        results = list(
            executor.map(
                lambda p: p.fetch("700", date(2024, 1, 1), date(2025, 12, 31)),
                providers,
            )
        )
    assert sum(len(client.calls) for client in clients) == 1
    assert len(calls) == 1
    for result in results:
        assert not [x for x in result.attempts if x["status"] == "failed"]
        statement = result.statements[0]
        assert statement.revenue == 373_710_000_000.0
        assert statement.net_income_parent == 19_272_000_000.0
        assert statement.free_cash_flow == 42_000_000_000.0
        assert statement.period_end == date(2024, 12, 31)
        assert statement.currency == "CNY"
    hub = providers[0].material_hub
    original = hub.lookup(url)
    rendition = hub.get_rendition(original["sha256"], "hkex-statement-layout-1")
    assert rendition["contract_limited_projection"] is True
    assert not list((hub.root / "parsed").glob("*/parsed.json"))
    assert not list(tmp_path.glob("a/**/*.pdf"))
    assert not list(tmp_path.glob("b/**/*.pdf"))


def test_cninfo_old_values_read_only_migrate_without_raw_or_download(tmp_path):
    authority = AuthorityRef(tmp_path / "authority")
    http = HTTP(authority)
    old = tmp_path / "old_cache"
    old.mkdir()
    url = "https://static.cninfo.com.cn/finalpage/2025/report.pdf"
    content_sha = hashlib.sha256(b"old-original-no-longer-available").hexdigest()
    payload = {
        "contract": "cninfo-pdf-parse-5",
        "source_url": url,
        "content_sha256": content_sha,
        "values": {"revenue": 17_000_000.0},
        "parser": "pdfplumber",
    }
    legacy_path = old / (hashlib.sha256(url.encode()).hexdigest() + ".json")
    legacy_path.write_text(json.dumps(payload), encoding="utf-8")
    before = legacy_path.read_bytes()
    first = CninfoAnnualReportProvider(
        {"point_in_time_data": {"cninfo_pdf_cache_dir": str(old)}}, http=http
    )
    assert first._read_pdf_cache(url)["values"] == payload["values"]
    second = CninfoAnnualReportProvider(
        {"project_root": str(tmp_path / "other")}, http=http
    )
    assert second._read_pdf_cache(url)["values"] == payload["values"]
    assert http.calls == []
    assert legacy_path.read_bytes() == before
    assert second.material_hub.lookup(url) is None
    with pytest.raises(MaterialError, match="not_registered"):
        second.material_hub.resolve(content_sha)
    cached = second.material_hub.get_rendition(content_sha, "cninfo-pdf-parse-5")
    assert cached["provenance"]["original_available"] is False
    assert second._read_pdf_cache(url, version="cninfo:NEW-REVISION") is None


def test_rendition_new_revision_does_not_inherit_old_url_projection(tmp_path):
    hub = DataHub(AuthorityRef(tmp_path / "authority"))
    url = "https://static.cninfo.com.cn/report.pdf"
    first = hub.import_bytes(b"original-v1")
    hub.register_original(first["path"], url=url, version="v1")
    hub.import_rendition(first["sha256"], "explicit-layout-v1", {"value": 1})
    hub.alias_rendition(url, first["sha256"], "explicit-layout-v1")
    second = hub.import_bytes(b"original-v2")
    hub.register_original(second["path"], url=url, version="v2")
    assert hub.rendition_for_url(url, "explicit-layout-v1") is None
    assert hub.get_rendition(first["sha256"], "explicit-layout-v1")["value"] == {
        "value": 1
    }
    assert hub.rendition_for_url(url, "explicit-layout-v1", version="v2") is None


def test_cninfo_two_projects_share_one_original_and_contract(tmp_path, monkeypatch):
    authority = AuthorityRef(tmp_path / "authority")
    clients = [HTTP(authority), HTTP(authority)]
    providers = [
        CninfoAnnualReportProvider({"project_root": str(tmp_path / name)}, http=client)
        for name, client in zip(("a", "b"), clients)
    ]
    calls = []
    expected = {"revenue": 838_270_000_000.0, "net_income_parent": 368_562_000_000.0}

    def native(content):
        calls.append(content)
        return expected, "original-contract-fixture"

    for provider in providers:
        monkeypatch.setattr(provider, "_parse_pdf", native)
    url = "https://static.cninfo.com.cn/finalpage/2025/120001.pdf"
    announcement = {"announcementId": "120001"}
    with ThreadPoolExecutor(2) as executor:
        results = list(
            executor.map(lambda p: p._report_values(url, announcement), providers)
        )
    assert sum(len(client.calls) for client in clients) == 1
    assert len(calls) == 1
    assert all(result[0] == expected for result in results)
    assert sorted(result[2] for result in results) == [False, True]
    assert not list(tmp_path.glob("a/**/*.json"))
    assert not list(tmp_path.glob("b/**/*.json"))


def test_failed_layout_contract_is_not_reparsed_or_global_block(tmp_path):
    hub = DataHub(AuthorityRef(tmp_path / "authority"))
    first = hub.import_bytes(b"original-bad-layout")
    second = hub.import_bytes(b"original-good-layout")
    calls = []

    def fail():
        calls.append("failed")
        raise ValueError("native-layout-unavailable")

    with pytest.raises(ValueError, match="native-layout-unavailable"):
        hub.ensure_rendition(first["sha256"], "old-layout-v1", fail)
    with pytest.raises(MaterialError, match="not_retried:failed"):
        hub.ensure_rendition(first["sha256"], "old-layout-v1", fail)
    good = hub.ensure_rendition(
        second["sha256"], "old-layout-v1", lambda: {"revenue": 10}
    )
    assert good["value"] == {"revenue": 10}
    assert calls == ["failed"]
