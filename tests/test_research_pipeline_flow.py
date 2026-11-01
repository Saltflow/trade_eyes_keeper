"""Real public DAG/SQLite/files/SSE with failures injected only at IO boundaries."""

import copy
import json
import re
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

import pytest

from src.research_pipeline.common import file_sha
from src.research_pipeline.engine import get_status, plan_research, run_research
from src.research_pipeline.models import AnnualDocument, ResearchSpec
from src.research_pipeline.stages import market_fingerprint
from src.research_pipeline.store import Store

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(
        socket.socket, "connect", lambda *args: pytest.fail("unexpected real network")
    )


def pdf(path, text):
    """A valid one-page PDF exercises the real PDF reader, not a fake parser."""
    text = text.replace("(", "[").replace(")", "]")
    stream = f"BT /F1 9 Tf 40 700 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length "
        + str(len(stream)).encode()
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
    ]
    raw = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(raw))
        raw.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(raw)
    raw.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        raw.extend(f"{offset:010d} 00000 n \n".encode())
    raw.extend(f"trailer << /Root 1 0 R /Size 6 >>\nstartxref\n{xref}\n%%EOF".encode())
    path.write_bytes(raw)


class Source:
    def __init__(self, documents, *, discovery_error=False, quote=None, missing=()):
        self.documents = documents
        self.discovery_error, self.missing = discovery_error, missing
        self.quote_value = quote or {
            "price": 10,
            "date": "2026-10-30",
            "currency": "CNY",
            "quote_time": "2026-10-30T15:00:00+08:00",
        }

    def local_documents(self):
        return self.documents

    def discover(self, instrument, as_of):
        if self.discovery_error:
            raise TimeoutError("fixture official directory offline")
        return [d for d in self.documents if d.instrument == instrument.key]

    def acquire(self, document):
        if document.instrument in self.missing:
            raise FileNotFoundError("fixture annual PDF404")
        return document

    def quote(self, instrument, as_of):
        return {
            "instrument": instrument.key,
            **self.quote_value,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }


class Response:
    status_code = 200
    headers: ClassVar = {"Content-Type": "text/event-stream"}

    def __init__(self, content, *, partial=False):
        self.content, self.partial = content, partial

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_content(self, chunk_size=8192):
        value = {
            "id": "offline_" + str(id(self)),
            "model": "deepseek-flash",
            "choices": [{"delta": {"content": self.content}, "finish_reason": None}],
        }
        yield ("data: " + json.dumps(value, ensure_ascii=False) + "\n\n").encode()
        if self.partial:
            import requests

            raise requests.exceptions.ChunkedEncodingError("fixture stream interrupted")
        value = {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 30,
                "prompt_cache_hit_tokens": 0,
                "prompt_cache_miss_tokens": 100,
                "completion_tokens_details": {"reasoning_tokens": 5},
            },
        }
        yield ("data: " + json.dumps(value) + "\n\ndata: [DONE]\n\n").encode()


class Model:
    def __init__(self, *, invalid_block=None, partial_draft=False):
        config = json.loads(
            (ROOT / "config/fundamental_data_v2.json").read_text("utf-8")
        )
        self.examples = json.loads((ROOT / config["examples_path"]).read_text("utf-8"))[
            "blocks"
        ]
        self.calls = []
        self.invalid_block, self.partial_draft = invalid_block, partial_draft

    def __call__(self, url, **kwargs):
        request = json.loads(kwargs["data"])
        self.calls.append(request)
        body = json.dumps(request, ensure_ascii=False)
        match = re.search(r"\[block:([a-z0-9_]+)\]", body)
        if match:
            name = match.group(1)
            return Response(
                "{invalid original"
                if name == self.invalid_block
                else json.dumps(self.examples[name], ensure_ascii=False)
            )
        draft = "# 第一轮：" in body
        return Response(
            "# 离线边界响应\n\n本正文用于执行链测试，不能作为真实财务研究。\n\n## Q1\n资料保留。\n",
            partial=draft and self.partial_draft,
        )


def fixture_spec(
    tmp_path, *, companies=("a_share:600348",), name="run", budget="20", **kwargs
):
    return ResearchSpec(
        companies=companies,
        output=tmp_path / name,
        repo=ROOT,
        store_root=tmp_path / "shared",
        budget_limit_usd=Decimal(budget),
        paid=True,
        start_year=2025,
        end_year=2025,
        cache_roots=(tmp_path / "old",),
        **kwargs,
    )


def documents(tmp_path, identities=("a_share:600348",)):
    result = []
    for index, identity in enumerate(identities):
        path = tmp_path / f"annual{index}.pdf"
        pdf(path, "OFFLINE ANNUAL SOURCE FIXTURE " * 8 + str(index))
        result.append(
            AnnualDocument(
                identity,
                identity,
                2025,
                f"official_{index}",
                local_path=str(path),
                sha256=file_sha(path),
                profiles=["extractives"],
            )
        )
    return result


def test_discovery_applicability_requires_proof_and_never_hides_available_report(
    tmp_path,
):
    from src.research_pipeline.common import atomic_write, fingerprint
    from src.research_pipeline.engine import Pipeline, _new_run

    docs = documents(tmp_path)
    record = {"instrument": "a_share:600348", "listing_date": "2026-01-01"}
    rows = [
        {
            "instrument": "a_share:600348",
            "fiscal_year": year,
            "status": "prelisting",
            "listing_date": record["listing_date"],
            "evidence": {
                "source_url": "https://www.sse.com.cn/fixture-official-record",
                "record": record,
                "sha256": fingerprint(record),
            },
        }
        for year in (2019, 2020, 2025)
    ]
    # A declaration not bound to its evidence must remain a missing-year gap.
    rows[1]["listing_date"] = "2027-01-01"
    manifest = tmp_path / "annuals.json"
    atomic_write(manifest, {"documents": [], "applicability": rows})
    spec = fixture_spec(tmp_path, source_manifest=manifest)
    spec.start_year, spec.end_year = 2019, 2025
    store, run = _new_run(spec)
    pipeline = Pipeline(spec, store, run["id"], source=Source(docs))
    try:
        pipeline.discover()
        subject = pipeline.coverage()["companies"]["a_share:600348"]
        assert (
            subject["applicability"]["2019"]["a_share:600348"]["status"] == "prelisting"
        )
        assert "2025" in subject["versions"]
        assert "2025" not in subject["applicability"]
        missing = {
            gap["year"]
            for gap in pipeline.gaps
            if gap["kind"] == "annual_original_not_found"
        }
        assert missing == {2020, 2021, 2022, 2023, 2024}
        assert store.list_attempts(run["id"]) == []
    finally:
        pipeline.publisher.close()


def test_real_flow_cache_hit_and_quote_only_invalidates_research(tmp_path, monkeypatch):
    docs, model = documents(tmp_path), Model()
    first = run_research(fixture_spec(tmp_path), source=Source(docs), post=model)
    assert first["state"] == "complete", first
    # 8 common +1 extractives, 1 summary, single-company handoff free, 3 research.
    assert len(model.calls) == 13
    assert len([a for a in first["artifacts"] if a["kind"] == "research"]) == 1
    store = Store(tmp_path / "shared")
    count = len(model.calls)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    second = run_research(
        fixture_spec(tmp_path, name="cache", budget="0"),
        source=Source(docs),
    )
    assert second["state"] == "complete" and len(model.calls) == count
    assert second["budget"]["known_usage_usd"] == "0"
    changed = Source(docs, quote={"price": 11, "date": "2026-10-30", "currency": "CNY"})
    third = run_research(
        fixture_spec(tmp_path, name="new_price"), source=changed, post=model
    )
    assert len(model.calls) == count + 3
    assert third["budget"]["known_usage_usd"] != "0"
    assert len(list((store.root / "parsed").rglob("result.json"))) <= 1


def test_missing_company_and_invalid_block_are_isolated(tmp_path):
    identities = ("a_share:600348", "a_share:600546")
    docs = documents(tmp_path, identities)
    docs[1].local_path = ""
    model = Model(invalid_block="notes_assets")
    result = run_research(
        fixture_spec(tmp_path, companies=identities),
        source=Source(docs, missing=(identities[1],)),
        post=model,
    )
    assert result["state"] == "partial"
    assert any(
        a["kind"] == "summary" and a["company"] == identities[0]
        for a in result["artifacts"]
    )
    assert result["source_coverage"]["requested_instruments"] == list(identities)
    assert any(
        g["kind"] == "original_unavailable" and g["company"] == identities[1]
        for g in result["source_coverage"]["gaps"]
    )
    invalid = [
        request
        for request in model.calls
        if "[block:notes_assets]" in json.dumps(request, ensure_ascii=False)
    ]
    assert len(invalid) == 1
    status = get_status(result["id"], store_root=tmp_path / "shared")
    assert status["state"] == "partial" and status["events"]


def test_optional_directory_and_html_failure_do_not_stop_cached_evidence(
    tmp_path, monkeypatch
):
    docs, model = documents(tmp_path), Model()
    run_research(fixture_spec(tmp_path), source=Source(docs), post=model)
    from scripts import research_render

    monkeypatch.setattr(
        research_render,
        "html",
        lambda text: (_ for _ in ()).throw(PermissionError("fixture HTMLlocked")),
    )
    count = len(model.calls)
    result = run_research(
        fixture_spec(tmp_path, name="degraded", budget="0"),
        source=Source(docs, discovery_error=True),
        post=model,
    )
    assert result["state"] == "partial" and len(model.calls) == count
    assert result["artifacts"]
    assert any(
        g["kind"] == "freshness_unknown" for g in result["source_coverage"]["gaps"]
    )


def test_partial_draft_preserved_and_next_planned_round_runs_no_retry(tmp_path):
    docs, model = documents(tmp_path), Model(partial_draft=True)
    result = run_research(fixture_spec(tmp_path), source=Source(docs), post=model)
    assert result["state"] == "partial"
    assert len(model.calls) == 13
    research = next(a for a in result["artifacts"] if a["kind"] == "research")
    assert research["metadata"]["round_statuses"] == ["partial", "complete", "complete"]
    assert Decimal(result["budget"]["unknown_liability_reserved_usd_not_spend"]) > 0


def test_review_without_new_charts_keeps_prior_png_in_write_request(
    tmp_path, monkeypatch
):
    import base64

    from src.research_pipeline.stages import StageAdapters

    image = tmp_path / "actual_fixture.png"
    image.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jR2kAAAAASUVORK5CYII="
        )
    )
    views = []

    def render(self, text, folder, status):
        views.append(folder.name)
        return {"assets/draft_chart_1.png": image} if folder.name == "draft" else {}

    monkeypatch.setattr(StageAdapters, "render_charts", render)
    model = Model()
    result = run_research(
        fixture_spec(tmp_path), source=Source(documents(tmp_path)), post=model
    )
    assert views == ["draft", "review"]
    parts = model.calls[-1]["messages"][1]["content"]
    assert len([part for part in parts if part["type"] == "image_url"]) == 1
    assert "assets/draft_chart_1.png" in json.dumps(parts, ensure_ascii=False)
    assert result["state"] == "complete"
    assert len(model.calls) == 13


def test_offline_plan_never_contacts_sources_or_reads_model_key(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    spec = fixture_spec(tmp_path)
    spec.paid = False
    result = plan_research(spec)
    assert result["paid_requests_sent"] == 0 and result["discovery_pending"]


def test_quote_observation_clock_and_order_are_not_semantic_cache_changes():
    rows = [
        {
            "instrument": "us:XOM",
            "price": 100,
            "currency": "USD",
            "date": "2026-10-30",
            "observed_at": "one",
            "quote_time": "2026-10-30T16:00:00-04:00",
        }
    ]
    other = copy.deepcopy(rows)
    other[0].update(
        observed_at="two",
        quote_time="2026-10-30T16:01:00-04:00",
        source_url="different-provenance",
    )
    assert market_fingerprint(rows) == market_fingerprint(other)
    other[0]["price"] = 101
    assert market_fingerprint(rows) != market_fingerprint(other)


def test_resume_only_never_sent_extraction_and_preserves_sent_summary(
    tmp_path, monkeypatch
):
    from src.research_pipeline.engine import resume_research
    from src.research_pipeline.runtime import ModelExecutor

    original = ModelExecutor.call
    omitted = []

    def admit(self, key, request, *args, **kwargs):
        if not omitted and "[block:notes_assets]" in json.dumps(
            request, ensure_ascii=False
        ):
            omitted.append(key)
            return {
                "status": "not_sent",
                "reason": "fixture_temporary_admission",
                "capture_dir": "",
            }
        return original(self, key, request, *args, **kwargs)

    monkeypatch.setattr(ModelExecutor, "call", admit)
    model, docs = Model(), documents(tmp_path)
    spec = fixture_spec(tmp_path)
    result = run_research(spec, source=Source(docs), post=model)
    assert len(model.calls) == 12 and omitted
    store = Store(spec.store_root)
    before = {a["path"]: file_sha(Path(a["path"])) for a in store.list_artifacts()}
    resumed = resume_research(
        result["id"],
        store_root=spec.store_root,
        background=False,
        source=Source(docs),
        post=model,
    )
    assert len(model.calls) == 13
    assert all(file_sha(Path(path)) == sha for path, sha in before.items())
    assert any(
        g["kind"] == "summary_retains_prior_partial_input"
        for g in resumed["source_coverage"]["gaps"]
    )
    assert len(list((spec.output / "annuals").glob("*.json"))) == 2
    assert (
        sum(
            "[block:notes_assets]" in json.dumps(r, ensure_ascii=False)
            for r in model.calls
        )
        == 1
    )


def test_manifest_relative_originals_and_research_inputs_are_frozen(tmp_path):
    docs = documents(tmp_path)
    manifest = tmp_path / "original_manifest.json"
    record = docs[0].to_dict()
    record["local_path"] = Path(record["local_path"]).name
    manifest.write_text(json.dumps([record]), "utf-8")
    spec = fixture_spec(tmp_path, source_manifest=manifest)
    spec.paid = False
    result = plan_research(spec)
    known = result["known_documents"]
    assert len(known) == 1 and Path(known[0]["local_path"]).samefile(docs[0].local_path)
    original = (spec.output / "inputs/source_manifest.json").read_bytes()
    manifest.write_text("[]", "utf-8")
    assert (spec.output / "inputs/source_manifest.json").read_bytes() == original


def test_annual_selection_never_supersedes_another_issuer():
    from src.research_pipeline.engine import Pipeline

    first = AnnualDocument(
        "a_share:600348", "issuer_A", 2025, "A", published_at="2026-03-01"
    )
    updated = AnnualDocument(
        "a_share:600348", "issuer_A", 2025, "A2", published_at="2026-04-01"
    )
    second = AnnualDocument(
        "a_share:600546", "issuer_B", 2025, "B", published_at="2026-02-01"
    )
    selected = Pipeline._current_documents([first, updated, second])
    assert {d.document_id for d in selected} == {"A2", "B"}


def test_changed_response_content_is_not_treated_as_original_model_evidence(tmp_path):
    from src.research_pipeline.stages import LocalStageError, visible

    content = tmp_path / "content.md"
    content.write_text("original output", "utf-8")
    receipt = {"capture_dir": str(tmp_path), "content_sha256": file_sha(content)}
    content.write_text("changed financial output", "utf-8")
    with pytest.raises(LocalStageError, match="response_content_integrity_changed"):
        visible(receipt)


def test_tampered_frozen_inputs_pause_before_any_model_call(tmp_path):
    from src.research_pipeline.engine import _execute_existing, _new_run

    assumptions = tmp_path / "assumptions.md"
    assumptions.write_text("user approved basis", "utf-8")
    spec = fixture_spec(tmp_path, assumptions=assumptions)
    store, run = _new_run(spec, freeze=True)
    spec.assumptions.write_text("unapproved changed basis", "utf-8")
    model = Model()
    with pytest.raises(ValueError, match="input_integrity_changed"):
        _execute_existing(run["id"], spec.store_root, post=model)
    assert model.calls == [] and store.get_run(run["id"])["requires_resume"]
    assert store.get_run(run["id"])["pause_reason"] == "integrity_error"


def test_background_spawn_error_is_persisted_with_task_identity(tmp_path, monkeypatch):
    from src.research_pipeline.engine import _launch, _new_run

    spec = fixture_spec(tmp_path, budget="0")
    store, run = _new_run(spec, freeze=True)
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            FileNotFoundError("fixture Python unavailable")
        ),
    )
    with pytest.raises(ValueError, match=run["id"]):
        _launch(store, run)
    status = store.get_run(run["id"])
    assert status["state"] == "failed" and status["requires_resume"]
    assert status["pause_reason"] == "host_error"
    assert store.list_attempts(run["id"]) == []


def test_legacy_research_scope_unknown_is_delivered_without_duplicate_charge(tmp_path):
    from src.research_pipeline.engine import Pipeline

    spec = fixture_spec(tmp_path, budget="0")
    store = Store(spec.store_root)
    run = store.create_run(spec.to_dict())
    text = tmp_path / "prior_research.md"
    text.write_text("Original report with unproven old market/focus identity", "utf-8")
    versions = {"a_share:600348": {"2025": ["a" * 64]}}
    reference = store.put_artifact(
        "old-reference",
        "legacy_reference",
        text,
        metadata={"scope_unresolved": True, "versions": versions},
    )
    pipeline = Pipeline(spec, store, run["id"])
    pipeline.subjects = {"a_share:600348": {"versions": versions["a_share:600348"]}}
    assert pipeline.research(None, "new-key") is None
    assert pipeline.final_artifacts == [reference]
    assert (
        pipeline.gaps[0]["kind"]
        == "legacy_research_identity_unresolved_no_duplicate_charge"
    )
    assert store.list_attempts(run["id"]) == []


def test_locked_reading_page_cannot_destroy_body_delivery(tmp_path, monkeypatch):
    import shutil

    from src.research_pipeline.engine import Pipeline

    spec = fixture_spec(tmp_path, budget="0")
    store = Store(spec.store_root)
    run = store.create_run(spec.to_dict())
    body = tmp_path / "summary.md"
    page = body.with_suffix(".html")
    body.write_text("Preserved complete original model body", "utf-8")
    page.write_text("<html>reading page</html>", "utf-8")
    summary = store.put_artifact("summary", "summary", body)
    pipeline = Pipeline(spec, store, run["id"])
    pipeline.subjects = {"issuer": {"versions": {}}}
    pipeline.company_artifacts = {"issuer": {"summary": summary}}
    original = shutil.copyfile

    def copy(source, destination, **kwargs):
        if Path(source) == page:
            raise PermissionError("fixture reading HTML is exclusively locked")
        return original(source, destination, **kwargs)

    monkeypatch.setattr(shutil, "copyfile", copy)
    result = pipeline.deliver()
    assert result["artifacts"] and Path(result["artifacts"][0]["path"]).read_text(
        "utf-8"
    ) == body.read_text("utf-8")
    assert store.list_attempts(run["id"]) == []
    assert any(
        event["event"] == "presentation_failed" for event in store.events(run["id"])
    )


def test_soft_waterline_does_not_truncate_and_hard_limit_blocks(tmp_path, monkeypatch):
    from src.research_pipeline.engine import LazyModelExecutor
    from src.research_pipeline.stages import LocalStageError, StageAdapters

    spec = fixture_spec(tmp_path)
    store = Store(spec.store_root)
    run = store.create_run(spec.to_dict())
    adapter = StageAdapters(
        ROOT, store, run["id"], LazyModelExecutor(store, spec, run["id"]), spec.output
    )
    request = adapter.request("contract", "unchanged financial body")
    monkeypatch.setattr(adapter, "estimate", lambda value: 581940)
    assert adapter.check_context(request)["soft_waterline_exceeded"]
    assert request["messages"][1]["content"][0]["text"] == "unchanged financial body"
    monkeypatch.setattr(adapter, "estimate", lambda value: 800000)
    with pytest.raises(LocalStageError, match="hard_context_limit_no_truncation"):
        adapter.check_context(request)


def test_summary_partition_repeats_full_original_definitions_and_coverage(
    tmp_path, monkeypatch
):
    from src.research_pipeline.engine import LazyModelExecutor
    from src.research_pipeline.stages import StageAdapters

    spec = fixture_spec(tmp_path)
    store = Store(spec.store_root)
    run = store.create_run(spec.to_dict())
    adapter = StageAdapters(
        ROOT, store, run["id"], LazyModelExecutor(store, spec, run["id"]), spec.output
    )
    path = tmp_path / "context.md"
    original = "# 正文\n\n## 共用定义\n\n### 共同口径\nC1：集团、原报、原币种。\n\n## 历史数据\n甲〔C1〕\n乙〔C1〕\n"
    path.write_text(original, "utf-8")
    headers = []

    def partition(system, header, material):
        headers.append(header)
        return [material[: len(material) // 2], material[len(material) // 2 :]]

    monkeypatch.setattr(adapter, "partition", partition)
    plan = adapter.summary_plan(
        "issuer",
        {"2025": ["hash"]},
        {"path": str(path), "status": "complete"},
        {"years": [2024, 2025]},
    )
    assert "C1：集团、原报、原币种。" in headers[0]
    assert "2024" in headers[0] and "2025" in headers[0]
    assert "".join(plan["pieces"]) == original
    for piece in plan["pieces"]:
        request = adapter.request(plan["system"], plan["header"] + piece)
        assert (
            "C1：集团、原报、原币种。" in request["messages"][1]["content"][0]["text"]
        )


def test_public_background_cli_runs_frozen_cached_pipeline_without_model_key(
    tmp_path, monkeypatch
):
    docs, model = documents(tmp_path), Model()
    run_research(fixture_spec(tmp_path), source=Source(docs), post=model)
    manifest = tmp_path / "documents.json"
    manifest.write_text(json.dumps([d.to_dict() for d in docs]), "utf-8")
    market = tmp_path / "market.json"
    market.write_text(
        json.dumps([{"instrument": docs[0].instrument, **Source(docs).quote_value}]),
        "utf-8",
    )
    # External metadata boundaries fail through a local unreachable proxy. No
    # model is authorised, and the separate cold worker must still deliver cache.
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    output = tmp_path / "background"
    command = [
        sys.executable,
        "-B",
        "-X",
        "utf8",
        str(ROOT / "scripts/financial_pipeline.py"),
        "run",
        "--company",
        docs[0].instrument,
        "--output",
        str(output),
        "--store-root",
        str(tmp_path / "shared"),
        "--cache-root",
        str(tmp_path / "old"),
        "--source-manifest",
        str(manifest),
        "--market-snapshot",
        str(market),
        "--start-year",
        "2025",
        "--end-year",
        "2025",
        "--budget-limit-usd",
        "0",
        "--paid",
        "--background",
    ]
    launched = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=45,
        check=False,
    )
    assert launched.returncode == 0, launched.stderr
    accepted = json.loads(launched.stdout)
    assert accepted["id"] and accepted["host_launch"]["pid"]
    store = Store(tmp_path / "shared")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        run = store.get_run(accepted["id"])
        if run.get("worker_exit"):
            break
        time.sleep(0.2)
    assert run.get("worker_exit"), run
    assert run["state"] == "partial", run
    assert store.list_attempts(run["id"]) == []
    assert (output / "delivery/index.html").is_file()
    audit = json.loads((output / "delivery/accounting.json").read_text("utf-8"))
    assert audit["totals"]["sent_count"] == 0


def test_summary_partition_plan_is_shared_and_not_recomputed_on_prompt_upgrade(
    tmp_path, monkeypatch
):
    from src.research_pipeline.engine import LazyModelExecutor
    from src.research_pipeline.stages import StageAdapters

    spec = fixture_spec(tmp_path)
    store = Store(spec.store_root)
    run = store.create_run(spec.to_dict())
    adapter = StageAdapters(
        ROOT, store, run["id"], LazyModelExecutor(store, spec, run["id"]), spec.output
    )
    path = tmp_path / "context.md"
    path.write_text("# Annual-only original body", "utf-8")
    context = {"path": str(path), "status": "complete"}
    first = adapter.summary_plan(
        "issuer", {"2025": ["hash"]}, context, {"original_scope": True}
    )
    adapter.summary_config["questions"] = [
        "a changed prompt cannot silently replan paid work"
    ]
    monkeypatch.setattr(
        adapter,
        "_build_summary_plan",
        lambda *args: pytest.fail("same annual partition must be reused"),
    )
    second = adapter.summary_plan(
        "issuer", {"2025": ["hash"]}, context, {"another_run_gap": True}
    )
    assert second == first
    assert store.list_attempts(run["id"]) == []


@pytest.mark.parametrize(
    "status,body",
    [
        ("complete", "Original model summary"),
        ("partial", "Original partial model summary"),
        ("failed", ""),
    ],
)
def test_attempted_summary_cannot_create_new_partition_requests(
    tmp_path, monkeypatch, status, body
):
    from src.research_pipeline.common import artifact_key
    from src.research_pipeline.engine import Pipeline
    from src.research_pipeline.stages import StageAdapters

    spec = fixture_spec(tmp_path)
    store = Store(spec.store_root)
    run = store.create_run(spec.to_dict())
    context_file = tmp_path / "context.md"
    context_file.write_text("# Current available context", "utf-8")
    capture = tmp_path / "prior_capture"
    capture.mkdir()
    (capture / "content.md").write_text(body, "utf-8")
    versions = {"2025": ["a" * 64]}
    key = artifact_key("summary", "issuer", versions)
    store.import_attempt(
        key,
        {
            "capture_dir": str(capture),
            "receipt": {
                "status": status,
                "content_sha256": file_sha(capture / "content.md"),
                "usage": None,
            },
        },
    )
    context = store.put_artifact("context", "context", context_file)
    model = Model()
    pipeline = Pipeline(spec, store, run["id"], post=model)
    pipeline.adapters = StageAdapters(
        ROOT, store, run["id"], pipeline.executor, spec.output, pipeline.event
    )
    pipeline.subjects = {"issuer": {"versions": versions}}
    pipeline.company_artifacts = {"issuer": {"summary": None, "context": context}}
    monkeypatch.setattr(
        pipeline.adapters,
        "summary_plan",
        lambda *args: pytest.fail("do not replan an already sent summary"),
    )
    try:
        pipeline.summaries()
        assert model.calls == []
        assert bool(pipeline.company_artifacts["issuer"]["summary"]) == bool(body)
    finally:
        pipeline.executor.close()
