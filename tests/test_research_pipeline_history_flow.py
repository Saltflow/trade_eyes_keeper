"""Fixed real-history cells through the formal DAG; only HTTP/SSE are mocked.

The financial fixture is deliberately a two-row excerpt, not a full company.
No PDF, original acquisition, extraction POST, or external socket is permitted.
"""

from __future__ import annotations

import json
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlsplit

import pytest

from src.research_pipeline.common import (
    annual_versions,
    artifact_key,
    file_sha,
    fingerprint,
    load,
)
from src.research_pipeline.engine import run_research
from src.research_pipeline.models import AnnualDocument
from src.research_pipeline.parsing import ParsedCache
from src.research_pipeline.sources import SourceService, register_document
from src.research_pipeline.store import Store
from tests.test_research_pipeline_flow import Model, fixture_spec

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/research_pipeline"


def pointer(value, path):
    for part in path.strip("/").split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def test_fixed_history_excerpt_preserves_selected_original_values():
    manifest = load(FIXTURES / "manifest.json")
    path = FIXTURES / manifest["fixture"]["path"]
    assert file_sha(path) == manifest["fixture"]["sha256"]
    data = load(path)
    assert manifest["financial_validation"] == "not_independently_verified"
    assert len(data["tables"]) == 1 and len(data["tables"][0]["rows"]) == 2
    for selection in manifest["selections"]:
        value = pointer(data, selection["fixture_pointer"])
        assert fingerprint(value) == selection["value_sha256"]
    # Development check against retained evidence; portable CI needs only the
    # committed fixture and its hashes, not the multi-gigabyte history tree.
    original = ROOT / manifest["source"]["annual_json"]
    if original.is_file():
        assert file_sha(original) == manifest["source"]["annual_json_sha256"]
        source = load(original)
        for selection in manifest["selections"]:
            assert pointer(data, selection["fixture_pointer"]) == pointer(
                source, selection["source_pointer"]
            )


class DirectoryResponse:
    status_code = 200
    headers: ClassVar = {}

    def __init__(self, value=None, text=""):
        self.value, self.text = value, text

    def json(self):
        return self.value

    def raise_for_status(self):
        pass


class DirectoryHTTP:
    """CNINFO + quote IO boundary, explicitly synthetic directory responses."""

    def __init__(self, source):
        self.source, self.calls = source, []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url))
        if url.endswith("/new/information/topSearch/query"):
            return DirectoryResponse([{"code": "600348", "orgId": "fixture-org"}])
        assert url.endswith("/new/hisAnnouncement/query")
        assert kwargs["data"]["stock"] == "600348,fixture-org"
        return DirectoryResponse(
            {
                "totalpages": 1,
                "announcements": [
                    {
                        "secCode": "600348",
                        "announcementTitle": "2025年年度报告",
                        "announcementId": self.source["document_id"].split(":", 1)[1],
                        "adjunctUrl": urlsplit(self.source["official_url"]).path.lstrip(
                            "/"
                        ),
                        "announcementTime": int(
                            datetime(2026, 4, 27, tzinfo=timezone.utc).timestamp()
                            * 1000
                        ),
                    }
                ],
            }
        )

    def get(self, url, **kwargs):
        self.calls.append(("GET", url))
        assert url == "https://qt.gtimg.cn/q=sh600348", "PDF acquisition is forbidden"
        parts = [""] * 31
        parts[3], parts[30] = "10", "20261030150000"
        return DirectoryResponse(text='v_sh600348="' + "~".join(parts) + '";')


def test_cached_real_history_excerpt_keeps_partial_provenance_and_reuses_all(
    tmp_path, monkeypatch
):
    manifest = load(FIXTURES / "manifest.json")
    source = manifest["source"]
    body = FIXTURES / manifest["fixture"]["path"]
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (body, FIXTURES / "manifest.json")
    }
    store = Store(tmp_path / "shared")
    document = AnnualDocument(
        source["instrument"],
        source["issuer_id"],
        source["fiscal_year"],
        source["document_id"],
        url=source["official_url"],
        published_at="2026-04-27T08:00:00+08:00",
        sha256=source["pdf_sha256"],
        period_start="2025-01-01",
        period_end="2025-12-31",
        profiles=["extractives"],
        provenance={
            "fixture_scope": "two-row historical extraction excerpt; original PDF not loaded"
        },
    )
    register_document(store.root, document)
    versions = annual_versions([document])
    store.put_artifact(
        artifact_key("annual", document.issuer_id, versions),
        "annual",
        body,
        status="partial",
        metadata={
            "issuer": document.issuer_id,
            "year": document.fiscal_year,
            "versions": versions,
            "documents": [document.to_dict()],
            "fixture_scope": manifest["scope"],
            "accepted_blocks": 1,
            "failures": [
                {
                    "block": "fixture_scope",
                    "status": "limited_excerpt",
                    "reason": "Other historical tables are intentionally outside this fixture; not an extraction failure.",
                }
            ],
        },
    )

    def forbidden(*args, **kwargs):
        pytest.fail("real network, original download, or PDF parse is forbidden")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(ParsedCache, "read", forbidden)
    monkeypatch.setattr(SourceService, "acquire", forbidden)
    model = Model()
    first_spec = fixture_spec(tmp_path, name="history-first")
    http = DirectoryHTTP(source)
    service = SourceService(first_spec, store, http=http, minimum_interval=0)
    first = run_research(first_spec, source=service, post=model)
    assert len(model.calls) == 4, (
        "Only one summary and three research rounds are permitted"
    )
    assert all("[block:" not in json.dumps(request) for request in model.calls)
    assert first["state"] == "partial"
    context = store.get_artifact(artifact_key("context", document.issuer_id, versions))
    assert context["status"] == "partial"
    audit = context["metadata"]["retention"]
    assert audit["passed"] and audit["errors"] == []
    assert (
        audit["input_cells"]
        == audit["mapped_cells"]
        == audit["displayed_input_cells"]
        == 4
    )
    text = Path(context["path"]).read_text("utf-8")
    row = load(body)["tables"][0]["rows"][0]
    assert all(value in text for value in row["values"].values())
    assert "原表空白" in text

    # Cache provenance must remain visible in coverage; otherwise upstream
    # partial can disappear when a later summary cache is consumed directly.
    assert any(
        gap.get("kind") == "cached_annual_partial" and gap.get("year") == 2025
        for gap in first["source_coverage"]["gaps"]
    )

    second_spec = fixture_spec(tmp_path, name="history-cache", budget="0")
    second = run_research(
        second_spec,
        source=SourceService(second_spec, store, http=http, minimum_interval=0),
        post=model,
    )
    assert second["state"] == "partial" and len(model.calls) == 4
    assert any(
        gap.get("kind") == "cached_annual_partial" and gap.get("year") == 2025
        for gap in second["source_coverage"]["gaps"]
    )
    assert second["budget"]["known_usage_usd"] == "0"
    assert before == {
        path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before
    }
    assert len([call for call in http.calls if call[0] == "POST"]) == 4
