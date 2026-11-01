"""Frozen run inputs are evidence, never a source of silently adopted updates."""

import hashlib

import pytest

from src.research_pipeline.common import atomic_write, file_sha
from src.research_pipeline.integrity import verify_input_manifests


def binding(tmp_path, files):
    path = tmp_path / "manifest.json"
    atomic_write(path, {"files": files})
    return {"path": str(path), "sha256": file_sha(path)}


def test_exact_manifest_and_all_files_verify_without_mutation(tmp_path):
    first, second = tmp_path / "assumptions.md", tmp_path / "quotes.json"
    atomic_write(first, "条件不变\r\n原文")
    atomic_write(second, [{"price": "10.00", "date": "2026-10-30"}])
    declared = binding(tmp_path, {str(p): file_sha(p) for p in (first, second)})
    paths = [first, second, tmp_path / "manifest.json"]
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}
    assert verify_input_manifests({"integrity_manifests": [declared]}) is None
    assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}


@pytest.mark.parametrize(
    "operation", ["change_file", "remove_file", "change_manifest", "remove_manifest"]
)
def test_missing_or_changed_manifest_and_mandatory_files_fail(tmp_path, operation):
    source = tmp_path / "snapshot.json"
    atomic_write(source, {"price": 10})
    declared = binding(tmp_path, {str(source): file_sha(source)})
    if operation == "change_file":
        atomic_write(source, {"price": 11})
    elif operation == "remove_file":
        source.unlink()
    elif operation == "change_manifest":
        atomic_write(tmp_path / "manifest.json", {"files": {}})
    else:
        (tmp_path / "manifest.json").unlink()
    with pytest.raises(ValueError, match="^input_integrity_changed"):
        verify_input_manifests({"integrity_manifests": [declared]})


def test_relative_manifest_path_is_rejected(tmp_path):
    declared = binding(tmp_path, {})
    declared["path"] = "manifest.json"
    with pytest.raises(
        ValueError, match="input_integrity_changed: absolute_path_required"
    ):
        verify_input_manifests({"integrity_manifests": [declared]})


def test_relative_listed_file_is_rejected(tmp_path):
    declared = binding(tmp_path, {"relative.json": "0" * 64})
    with pytest.raises(
        ValueError, match="input_integrity_changed: absolute_path_required"
    ):
        verify_input_manifests({"integrity_manifests": [declared]})


@pytest.mark.parametrize(
    "value", [None, [], {"files": []}, {"files": {"ignored": None}}]
)
def test_malformed_manifest_is_not_treated_as_empty(tmp_path, value):
    path = tmp_path / "manifest.json"
    atomic_write(path, value)
    declared = {"path": str(path), "sha256": file_sha(path)}
    with pytest.raises(ValueError, match="^input_integrity_changed"):
        verify_input_manifests({"integrity_manifests": [declared]})


def test_duplicate_json_keys_are_rejected_even_with_matching_raw_hash(tmp_path):
    raw = b'{"files": {"one": "bad"}, "files": {}}'
    path = tmp_path / "manifest.json"
    path.write_bytes(raw)
    declared = {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}
    with pytest.raises(ValueError, match="duplicate_manifest_key"):
        verify_input_manifests({"integrity_manifests": [declared]})


def test_all_bound_manifests_are_mandatory(tmp_path):
    first = binding(tmp_path, {})
    missing = {"path": str(tmp_path / "missing.json"), "sha256": "0" * 64}
    with pytest.raises(ValueError, match="^input_integrity_changed"):
        verify_input_manifests({"integrity_manifests": [first, missing]})


@pytest.mark.parametrize(
    "bindings", [None, {}, "manifest", [{"path": "missing"}], [None]]
)
def test_malformed_binding_field_fails_closed(bindings):
    with pytest.raises(ValueError, match="^input_integrity_changed"):
        verify_input_manifests({"integrity_manifests": bindings})


def test_empty_input_set_and_unmigrated_run_are_compatible(tmp_path):
    verify_input_manifests({"integrity_manifests": [binding(tmp_path, {})]})
    verify_input_manifests({})
