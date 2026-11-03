"""Offline checks of batch orchestration; no financial results are fabricated."""

import hashlib
import importlib.util
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location(
    "financial_subagent_batch", ROOT / "scripts/run_financial_subagent_batch.py"
)
batch_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(batch_module)


def record(tmp_path, name):
    pdf = tmp_path / f"{name}.pdf"
    pdf.write_bytes(b"offline orchestration fixture")
    return {
        "id": name,
        "code": name,
        "pdf": str(pdf),
        "pdf_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
        "profiles": ["fixture"],
        "name": name,
        "year": 2025,
    }


def test_failed_report_does_not_stop_other_reports(tmp_path, monkeypatch):
    records = [record(tmp_path, "bad"), record(tmp_path, "good")]
    output = tmp_path / "output"
    output.mkdir()
    batch = batch_module.Batch(output, records, "manifest", 2)

    def extract(repo, args, config, pdfs, summary):
        if args.company == "bad":
            raise ValueError("fixture failure")
        summary["status"] = "completed"
        return {"tables": [{"role": "BS"}], "issues": [], "unstructured_blocks": []}

    monkeypatch.setattr(batch_module.pipeline, "extract", extract)
    monkeypatch.setattr(batch_module.pipeline, "publish", lambda *args: None)
    for item in records:
        batch.run_record(item, {"labels": {}})

    assert batch.status["records"]["bad"]["status"] == "failed"
    assert batch.status["records"]["good"]["status"] == "processed"
    assert (
        json.loads((output / "bad" / "summary.json").read_text("utf8"))["error"]
        == "fixture failure"
    )


def test_existing_report_directory_is_preserved_on_resume(tmp_path, monkeypatch):
    item = record(tmp_path, "report")
    output = tmp_path / "output"
    folder = output / item["id"]
    folder.mkdir(parents=True)
    raw = folder / "native-raw.txt"
    raw.write_bytes(b"previous raw bytes\x00")
    batch = batch_module.Batch(output, [item], "manifest", 1)

    def forbidden(*args, **kwargs):
        pytest.fail("existing report must not call the pipeline")

    monkeypatch.setattr(batch_module.pipeline, "extract", forbidden)
    batch.run_record(item, {})
    assert raw.read_bytes() == b"previous raw bytes\x00"
    assert batch.status["records"]["report"]["status"] == "incomplete_or_interrupted"
    with pytest.raises(ValueError, match="output_manifest_mismatch"):
        batch_module.Batch(output, [item], "different", 1)


def test_native_calls_never_exceed_worker_limit(tmp_path, monkeypatch):
    output = tmp_path / "output"
    output.mkdir()
    records = [{"id": str(i)} for i in range(6)]
    batch = batch_module.Batch(output, records, "manifest", 5)
    guard = threading.Lock()
    full = threading.Event()
    release = threading.Event()
    active = 0
    peak = 0

    def call(repo, folder, request, parse, **kwargs):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
            if active == 5:
                full.set()
        assert release.wait(5)
        (folder / "receipt.json").write_text('{"status":"received"}', encoding="utf8")
        with guard:
            active -= 1

    monkeypatch.setattr(batch_module.pipeline, "call_subagent", call)
    folders = [tmp_path / str(i) for i in range(6)]
    for folder in folders:
        folder.mkdir()

    def invoke(i, folder):
        batch.local.record_id = str(i)
        return batch.native(ROOT, folder, {}, 1, "high", "statements")

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [
            executor.submit(invoke, i, folder) for i, folder in enumerate(folders)
        ]
        try:
            assert full.wait(5)
        finally:
            release.set()
        for future in futures:
            future.result()
    assert peak == 5


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (None, "failed"),
        (
            {"tables": [{"role": "BS", "issues": ["unreadable fixture cell"]}]},
            "processed_with_issues",
        ),
    ],
)
def test_missing_output_and_nested_issues_are_not_clean(
    tmp_path, monkeypatch, data, expected
):
    item = record(tmp_path, "report")
    output = tmp_path / "output"
    output.mkdir()
    batch = batch_module.Batch(output, [item], "manifest", 1)

    def extract(repo, args, config, pdfs, summary):
        summary["status"] = "completed"
        return data

    monkeypatch.setattr(batch_module.pipeline, "extract", extract)
    monkeypatch.setattr(batch_module.pipeline, "publish", lambda *args: None)
    batch.run_record(item, {"labels": {}})
    assert batch.status["records"]["report"]["status"] == expected


@pytest.mark.parametrize(
    ("event", "paused"),
    [
        ({"type": "turn.failed", "error": {"message": "usage limit reached"}}, True),
        (
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": "The report discusses rate limit policies.",
                },
            },
            False,
        ),
    ],
)
def test_quota_detection_only_uses_failure_evidence(
    tmp_path, monkeypatch, event, paused
):
    output = tmp_path / "output"
    output.mkdir()
    batch = batch_module.Batch(output, [{"id": "r"}], "manifest", 1)
    batch.local.record_id = "r"
    folder = tmp_path / "call"
    folder.mkdir()

    def call(*args, **kwargs):
        (folder / "receipt.json").write_text('{"status":"failed"}', encoding="utf8")
        (folder / "native-events.jsonl").write_text(json.dumps(event), encoding="utf8")

    monkeypatch.setattr(batch_module.pipeline, "call_subagent", call)
    if paused:
        with pytest.raises(batch_module.Paused):
            batch.native(ROOT, folder, {}, 1, "high", "statements")
    else:
        batch.native(ROOT, folder, {}, 1, "high", "statements")
    assert batch.stop.is_set() is paused


def test_statements_assembly_preserves_distinct_raw_sources(tmp_path, monkeypatch):
    output = tmp_path / "output"
    output.mkdir()
    batch = batch_module.Batch(output, [{"id": "r"}], "manifest", 1)
    batch.local.record_id = "r"
    batch.local.pdf = tmp_path / "source.pdf"
    folder = tmp_path / "statements"
    folder.mkdir()
    request = {"messages": [{"content": []}]}

    def prepare(repo, child, scoped, pdfs):
        child.mkdir(parents=True)

    def native(repo, child, scoped, timeout, effort, block, role):
        (child / "receipt.json").write_text('{"status":"received"}', encoding="utf8")
        (child / "raw.txt").write_bytes(("raw-" + role).encode())
        return {
            "schema_version": "fixture",
            "company": "fixture",
            "report_period": "fixture",
            "tables": [{"role": role}],
            "issues": [],
        }

    monkeypatch.setattr(batch_module.pipeline, "prepare_request", prepare)
    monkeypatch.setattr(batch, "native", native)
    merged, notes = batch.receive(ROOT, folder, "statements", request, {})
    assert notes == []
    assert [table["role"] for table in merged["tables"]] == list(batch_module.ROLES)
    for role in batch_module.ROLES:
        assert (folder / "sources" / role / "raw.txt").read_bytes() == (
            "raw-" + role
        ).encode()
    assert not (folder / "raw.txt").exists()
    assert (
        json.loads((folder / "receipt.json").read_text("utf8"))["mode"]
        == "mechanical_assembly"
    )
