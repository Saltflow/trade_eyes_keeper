from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.search.artifacts import prune_optimizer_runs


def _write_run(
    root: Path,
    run_id: str,
    timestamp: str,
    *,
    activated: bool = False,
    artifact_run_id: str | None = None,
) -> Path:
    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True)
    artifact_owner = artifact_run_id or run_id
    artifact_dir = root / "runs" / artifact_owner
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact = artifact_dir / "a_share_best_params.yaml"
    artifact.write_text(
        "strategy_id: technical_ensemble\nparams: {}\n", encoding="utf-8"
    )
    manifest = {
        "run_id": run_id,
        "strategy": "technical_ensemble",
        "timestamp": timestamp,
        "activated": activated,
        "groups": {
            "a_share": {
                "artifact": f"runs/{artifact_owner}/{artifact.name}",
            }
        },
    }
    (run_dir / "manifest.yaml").write_text(
        yaml.safe_dump(manifest), encoding="utf-8"
    )
    return run_dir


def test_prune_keeps_three_newest_complete_runs_and_removes_partial(tmp_path: Path):
    for index in range(5):
        _write_run(tmp_path, f"run-{index}", f"2026-08-{index + 1:02d}T02:00:00")
    partial = tmp_path / "runs" / "partial"
    partial.mkdir(parents=True)
    (partial / "a_share_search_archive.jsonl").write_text(
        "candidate\n", encoding="utf-8"
    )

    result = prune_optimizer_runs(root=tmp_path, keep_completed=3)

    assert result.kept_complete == ("run-4", "run-3", "run-2")
    assert set(result.removed) == {"run-0", "run-1", "partial"}
    assert {path.name for path in (tmp_path / "runs").iterdir()} == {
        "run-2",
        "run-3",
        "run-4",
    }
    assert result.reclaimed_bytes > 0


def test_prune_protects_old_active_run_and_every_referenced_artifact(tmp_path: Path):
    old = _write_run(tmp_path, "old-active", "2026-07-01T02:00:00")
    for index in range(4):
        _write_run(tmp_path, f"recent-{index}", f"2026-08-{index + 1:02d}T02:00:00")
    merged = _write_run(
        tmp_path,
        "merged-active",
        "2026-08-10T02:00:00",
        activated=True,
        artifact_run_id="old-active",
    )
    (tmp_path / "latest_strategy.yaml").write_text(
        (merged / "manifest.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    result = prune_optimizer_runs(root=tmp_path, keep_completed=3)

    assert old.exists()
    assert merged.exists()
    assert {"old-active", "merged-active"}.issubset(result.protected)
    assert set(result.kept_complete) == {
        "merged-active",
        "recent-3",
        "recent-2",
    }


def test_prune_keeps_explicit_inflight_run(tmp_path: Path):
    inflight = tmp_path / "runs" / "inflight"
    inflight.mkdir(parents=True)
    (inflight / "archive.jsonl").write_text("candidate\n", encoding="utf-8")
    _write_run(tmp_path, "complete", "2026-08-01T02:00:00")

    result = prune_optimizer_runs(
        root=tmp_path,
        keep_completed=1,
        protected_run_ids=("inflight", "../outside"),
    )

    assert inflight.exists()
    assert result.protected == ("inflight",)


def test_prune_rejects_invalid_retention_count(tmp_path: Path):
    with pytest.raises(ValueError, match="at least 1"):
        prune_optimizer_runs(root=tmp_path, keep_completed=0)


def test_prune_fails_closed_when_active_pointer_is_invalid(tmp_path: Path):
    oldest = _write_run(tmp_path, "old", "2026-07-01T02:00:00")
    newest = _write_run(tmp_path, "new", "2026-08-01T02:00:00")
    (tmp_path / "latest_strategy.yaml").write_text("[invalid", encoding="utf-8")

    result = prune_optimizer_runs(root=tmp_path, keep_completed=1)

    assert result.removed == ()
    assert oldest.exists()
    assert newest.exists()


def test_prune_reports_one_failed_directory_without_blocking_others(
    tmp_path: Path, monkeypatch
):
    blocked = _write_run(tmp_path, "blocked", "2026-07-01T02:00:00")
    removable = _write_run(tmp_path, "removable", "2026-07-02T02:00:00")
    newest = _write_run(tmp_path, "newest", "2026-08-01T02:00:00")
    from src.search import artifacts

    real_rmtree = artifacts.shutil.rmtree

    def selective_rmtree(path):
        if Path(path).name == "blocked":
            raise PermissionError("simulated ACL")
        real_rmtree(path)

    monkeypatch.setattr(artifacts.shutil, "rmtree", selective_rmtree)

    result = prune_optimizer_runs(root=tmp_path, keep_completed=1)

    assert result.failed == ("blocked",)
    assert result.removed == ("removable",)
    assert blocked.exists()
    assert not removable.exists()
    assert newest.exists()
