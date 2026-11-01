"""Offline exact-byte recovery must never manufacture replacement evidence."""

import json
from pathlib import Path

import pytest

from src.research_pipeline.common import atomic_write, file_sha
from src.research_pipeline.recovery import recover_artifact
from src.research_pipeline.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "shared")


def artifact(store, tmp_path, text="原始正文\n", stage="summary", metadata=None):
    path = tmp_path / "historical" / "body.md"
    atomic_write(path, text, exclusive=True)
    return store.put_artifact("artifact-key", stage, path, metadata=metadata)


def successful_capture(store, tmp_path, **files):
    folder = tmp_path / "capture"
    for name, value in files.items():
        atomic_write(folder / name, value)
    store.import_attempt(
        "producer-other-key",
        {
            "status": "complete",
            "capture_dir": str(folder),
            "receipt": {"status": "complete"},
        },
    )
    return folder


def test_valid_artifact_is_untouched(store, tmp_path):
    original = artifact(store, tmp_path)
    assert recover_artifact(store, original["key"]) == original
    assert not list((store.root / "recovered").glob("*/*"))


def test_exact_copy_reindexed_without_touching_historical_corruption(store, tmp_path):
    copy = tmp_path / "copy.md"
    copy.write_bytes("原始正文\n".encode())
    original = artifact(store, tmp_path, metadata={"copies": [str(copy)]})
    Path(original["path"]).write_bytes(b"corrupted")
    restored = recover_artifact(store, original["key"])
    assert restored["sha256"] == original["sha256"]
    assert Path(restored["path"]).is_relative_to(store.root / "recovered")
    assert Path(original["path"]).read_bytes() == b"corrupted"
    assert copy.read_bytes() == "原始正文\n".encode()
    assert store.get_artifact(original["key"]) == restored
    assert restored["metadata"]["recovery"]["api_calls"] == 0


def test_successful_capture_content_restores_missing_final(store, tmp_path):
    original = artifact(store, tmp_path)
    successful_capture(store, tmp_path, **{"content.md": "原始正文\n"})
    Path(original["path"]).unlink()
    restored = recover_artifact(store, original["key"])
    assert restored["sha256"] == original["sha256"]
    assert Path(restored["path"]).read_text("utf-8") == "原始正文\n"


def test_raw_sse_recovery_omits_reasoning_and_preserves_exact_whitespace(
    store, tmp_path
):
    original = artifact(store, tmp_path, "  结论\n原文  ")
    chunks = [
        {"choices": [{"delta": {"reasoning_content": "隐藏推理"}}]},
        {"choices": [{"delta": {"content": "  结论\n"}}]},
        {"choices": [{"delta": {"content": "原文  "}}]},
    ]
    raw = "\n\n".join("data: " + json.dumps(c, ensure_ascii=False) for c in chunks)
    successful_capture(store, tmp_path, **{"response.raw": raw + "\n\ndata: [DONE]\n"})
    Path(original["path"]).unlink()
    restored = recover_artifact(store, original["key"])
    assert restored["sha256"] == original["sha256"]
    assert restored["metadata"]["recovery"]["method"] == "exact_response_reconstruction"


def test_extract_json_reconstruction_requires_original_serialization_hash(
    store, tmp_path
):
    value = {"v": [1, None], "中文": "值"}
    original = artifact(
        store, tmp_path, json.dumps(value, ensure_ascii=False, indent=2), "extract"
    )
    raw = {"choices": [{"message": {"content": json.dumps(value, ensure_ascii=False)}}]}
    successful_capture(
        store, tmp_path, **{"response.body": json.dumps(raw, ensure_ascii=False)}
    )
    Path(original["path"]).write_bytes(b"corrupt")
    restored = recover_artifact(store, original["key"])
    assert restored["sha256"] == original["sha256"]
    assert json.loads(Path(restored["path"]).read_text("utf-8")) == value


@pytest.mark.parametrize(
    "raw",
    [
        'data: {"choices":[{"delta":{"content":"原始正文\\n"}}]}\n',
        '{"choices":[{"message":{"content":"不同正文"}}]}',
        "not a valid response",
    ],
)
def test_missing_terminal_or_different_content_is_not_recovered(store, tmp_path, raw):
    original = artifact(store, tmp_path)
    successful_capture(store, tmp_path, **{"response.raw": raw})
    Path(original["path"]).unlink()
    assert recover_artifact(store, original["key"]) is None
    assert not list((store.root / "recovered").glob("*/*"))


def test_failed_receipt_cannot_supply_reconstructed_body(store, tmp_path):
    original = artifact(store, tmp_path)
    folder = tmp_path / "failed"
    atomic_write(folder / "content.md", "原始正文\n")
    store.import_attempt(
        "failed",
        {
            "status": "failed",
            "capture_dir": str(folder),
            "receipt": {"status": "failed"},
        },
    )
    Path(original["path"]).unlink()
    assert recover_artifact(store, original["key"]) is None


def test_context_missing_required_auxiliary_cannot_be_recovered(store, tmp_path):
    copy = tmp_path / "copy.md"
    atomic_write(copy, "原始正文\n")
    original = artifact(
        store,
        tmp_path,
        stage="context",
        metadata={
            "copies": [str(copy)],
            "metadata_path": str(tmp_path / "missing.json"),
        },
    )
    Path(original["path"]).unlink()
    assert recover_artifact(store, original["key"]) is None


def test_context_hashed_auxiliary_restored_to_new_sibling(store, tmp_path):
    auxiliary = tmp_path / "metadata.json"
    backup = tmp_path / "metadata.backup.json"
    atomic_write(auxiliary, [{"year": 2024}])
    backup.write_bytes(auxiliary.read_bytes())
    copy = tmp_path / "copy.md"
    atomic_write(copy, "原始正文\n")
    store.put_artifact("auxiliary-backup", "metadata", backup)
    original = artifact(
        store,
        tmp_path,
        stage="context",
        metadata={
            "copies": [str(copy)],
            "metadata_path": str(auxiliary),
            "metadata_sha256": file_sha(auxiliary),
        },
    )
    auxiliary.write_bytes(b"bad json")
    Path(original["path"]).unlink()
    restored = recover_artifact(store, original["key"])
    restored_aux = Path(restored["metadata"]["metadata_path"])
    assert restored_aux.parent == Path(restored["path"]).parent
    assert restored_aux.read_bytes() == backup.read_bytes()
    assert auxiliary.read_bytes() == b"bad json"


def test_context_unhashed_but_invalid_auxiliary_fails_closed(store, tmp_path):
    auxiliary = tmp_path / "metadata.json"
    atomic_write(auxiliary, b"corrupt")
    original = artifact(
        store, tmp_path, stage="context", metadata={"metadata_path": str(auxiliary)}
    )
    successful_capture(store, tmp_path, **{"content.md": "原始正文\n"})
    Path(original["path"]).unlink()
    assert recover_artifact(store, original["key"]) is None


def test_semantically_identical_json_without_exact_original_bytes_is_rejected(
    store, tmp_path
):
    original = artifact(store, tmp_path, '{ "v" : 1 }', "extract")
    successful_capture(store, tmp_path, **{"content.md": '{"v":1}'})
    Path(original["path"]).unlink()
    assert recover_artifact(store, original["key"]) is None


def test_directory_and_missing_registry_are_explicitly_not_rebuilt(store, tmp_path):
    folder = tmp_path / "bundle"
    atomic_write(folder / "body.md", "正文")
    row = store.put_artifact("directory", "summary", folder)
    (folder / "body.md").unlink()
    assert recover_artifact(store, row["key"]) is None
    assert recover_artifact(store, "never-indexed") is None
