from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest
import yaml

from src.search.artifacts import (
    OptimizerGroupSummary,
    activate_run,
    load_latest_strategy_run,
    prune_optimizer_runs,
)


def _write_run(
    root: Path,
    run_id: str,
    timestamp: str,
    *,
    group: str = "a_share",
    activated: bool = False,
    artifact_run_id: str | None = None,
) -> Path:
    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True)
    artifact_owner = artifact_run_id or run_id
    artifact_dir = root / "runs" / artifact_owner
    artifact_dir.mkdir(parents=True, exist_ok=True)
    strategy, solver = {
        "a_share": ("technical_ensemble", "local_genetic"),
        "hk": ("regime_pullback", "simulated_annealing"),
        "us": ("percentile", "random"),
    }[group]
    artifact = artifact_dir / f"{group}_best_params.yaml"
    config_hash = f"hash-{artifact_owner}"
    artifact.write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "group": group,
                "strategy_id": strategy,
                "solver_id": solver,
                "gate_profile": "standard",
                "market_config_hash": config_hash,
                "params": {},
                "execution": {"model": "cash_cap"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 4,
        "run_id": run_id,
        "market_group": group,
        "timestamp": timestamp,
        "activated": activated,
        "candidate": not activated,
        "groups": {
            group: {
                "group": group,
                "run_id": artifact_owner,
                "artifact": f"runs/{artifact_owner}/{artifact.name}",
                "strategy": strategy,
                "solver_id": solver,
                "gate_profile": "standard",
                "config_hash": config_hash,
            }
        },
    }
    (run_dir / "manifest.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return run_dir


def _write_terminal_run(
    root: Path,
    run_id: str,
    timestamp: str,
    *,
    group: str = "hk",
    status: str = "completed",
    group_status: str = "no_candidates",
    receipt_name: str = "run_summary.yaml",
) -> Path:
    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    summary = asdict(
        OptimizerGroupSummary(
            group=group, run_id=run_id, status=group_status, evaluated_count=60000
        )
    )
    receipt = {
        "schema_version": 1,
        "run_id": run_id,
        "timestamp": timestamp,
        "status": status,
        "groups": {group: summary},
    }
    if receipt_name == "run_summary.yaml":
        receipt.update(activated=False, candidate=False)
    else:
        # Guard receipts have a smaller summary and need not repeat flags.
        receipt["groups"][group] = {
            "status": group_status,
            "evaluated_count": 60000,
            "artifact": None,
        }
    (run_dir / receipt_name).write_text(yaml.safe_dump(receipt), encoding="utf-8")
    (run_dir / f"{group}_search_archive.jsonl").write_text(
        '{"gate_passed": false, "failure_reasons": ["minimum trades"]}\n',
        encoding="utf-8",
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


@pytest.mark.parametrize(
    ("receipt_name", "status", "group_status"),
    [
        ("run_summary.yaml", "completed", "no_candidates"),
        ("run_summary.yaml", "completed", "completed"),
        ("run_summary.yaml", "completed", "no_data"),
        ("run_summary.yaml", "no_symbols", "no_symbols"),
        ("run_summary.yaml", "failed", "failed"),
        ("run_summary.yaml", "failed", "interrupted"),
        ("optimizer_failure.yaml", "failed", "interrupted"),
        ("optimizer_failure.yaml", "failed", "completed"),
        ("optimizer_failure.yaml", "failed", "failed"),
    ],
)
def test_prune_preserves_terminal_audit_without_any_complete_candidate(
    tmp_path: Path, receipt_name, status, group_status
):
    run_dir = _write_terminal_run(
        tmp_path,
        "hk-terminal",
        "2026-09-07T20:25:00+08:00",
        receipt_name=receipt_name,
        status=status,
        group_status=group_status,
    )
    (run_dir / "hk_search_checkpoint.yaml").write_text("evaluated: 60000\n")
    (run_dir / "data_readiness.json").write_text('{"ready": true}\n')
    before = {path.name: path.read_bytes() for path in run_dir.iterdir()}

    result = prune_optimizer_runs(root=tmp_path, keep_completed=3)

    assert result.kept_complete == ()
    assert result.kept_diagnostics == (run_dir.name,)
    assert result.removed == ()
    assert {path.name: path.read_bytes() for path in run_dir.iterdir()} == before
    assert not (run_dir / "manifest.yaml").exists()


@pytest.mark.parametrize("candidate_market", ["a_share", "us"])
def test_three_newer_candidates_cannot_erase_hk_gate_audit(tmp_path, candidate_market):
    hk = _write_terminal_run(tmp_path, "hk-no-candidates", "2026-09-07T20:25:00")
    for index in range(3):
        _write_run(
            tmp_path,
            f"candidate-{index}",
            f"2026-09-07T20:{30 + index}:00",
            group=candidate_market,
        )
        result = prune_optimizer_runs(root=tmp_path, keep_completed=3)
        assert result.kept_diagnostics == (hk.name,)
        assert (hk / "hk_search_archive.jsonl").is_file()

    assert result.kept_complete == ("candidate-2", "candidate-1", "candidate-0")
    assert len(list((tmp_path / "runs").iterdir())) == 4


def test_candidates_and_terminal_audits_share_a_separate_limit_per_market(tmp_path):
    for group in ("a_share", "hk", "us"):
        for index in range(4):
            writer = _write_terminal_run if index % 2 == 0 else _write_run
            writer(
                tmp_path,
                f"{group}-{index}",
                f"2026-09-0{index + 1}T02:00:00",
                group=group,
            )

    result = prune_optimizer_runs(root=tmp_path, keep_completed=2)

    assert set(result.kept_complete) == {
        f"{group}-3" for group in ("a_share", "hk", "us")
    }
    assert set(result.kept_diagnostics) == {
        f"{group}-2" for group in ("a_share", "hk", "us")
    }
    assert set(result.removed) == {
        f"{group}-{index}" for group in ("a_share", "hk", "us") for index in (0, 1)
    }
    assert len(list((tmp_path / "runs").iterdir())) == 6


def test_candidate_with_receipt_uses_one_slot_and_remains_manifest_backed(tmp_path):
    _write_run(tmp_path, "old-hk", "2026-09-01T02:00:00", group="hk")
    diagnostic = _write_terminal_run(tmp_path, "hk-audit", "2026-09-02T02:00:00")
    candidate = _write_run(tmp_path, "hk-candidate", "2026-09-03T02:00:00", group="hk")
    _write_terminal_run(
        tmp_path, candidate.name, "2026-09-03T02:00:00", group_status="completed"
    )

    result = prune_optimizer_runs(root=tmp_path, keep_completed=2)

    assert result.kept_complete == (candidate.name,)
    assert result.kept_diagnostics == (diagnostic.name,)
    assert result.removed == ("old-hk",)


@pytest.mark.parametrize(
    ("fields", "value"),
    [
        (("schema_version",), None),
        (("schema_version",), True),
        (("schema_version",), "1"),
        (("schema_version",), 1.0),
        (("schema_version",), 2),
        (("run_id",), "foreign-run"),
        (("run_id",), "../outside"),
        (("groups",), {}),
        (("groups",), {"mixed": {"status": "completed"}}),
        (
            ("groups",),
            {"hk": {"status": "no_candidates"}, "us": {"status": "completed"}},
        ),
        (("groups", "hk"), []),
        (("groups", "hk", "group"), "us"),
        (("groups", "hk", "run_id"), "foreign-run"),
        (("groups", "hk", "status"), "running"),
        (("groups", "hk", "status"), "not_run"),
        (("groups", "hk", "status"), None),
        (("groups", "hk", "status"), []),
        (("groups", "hk", "status"), "failed"),
        (("status",), "running"),
        (("status",), "no_symbols"),
        (("market_group",), "us"),
        (("activated",), True),
        (("candidate",), True),
        (("candidate",), "false"),
        (("candidate",), 0),
        (("timestamp",), "not-an-ISO-timestamp"),
        (("timestamp",), None),
        (("timestamp",), 12345),
    ],
)
def test_invalid_receipt_does_not_displace_valid_market_audit(tmp_path, fields, value):
    valid = _write_terminal_run(tmp_path, "valid-hk", "2026-09-01T02:00:00")
    invalid = _write_terminal_run(tmp_path, "invalid-hk", "2026-09-02T02:00:00")
    path = invalid / "run_summary.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    parent = payload
    for field in fields[:-1]:
        parent = parent[field]
    parent[fields[-1]] = value
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    result = prune_optimizer_runs(root=tmp_path, keep_completed=1)

    assert result.kept_diagnostics == (valid.name,)
    assert result.kept_complete == ()
    assert result.removed == (invalid.name,)


@pytest.mark.parametrize("raw", [b"[invalid", b"- not-a-mapping\n", b"", b"\xff"])
def test_malformed_receipt_is_pruned_without_aborting_cleanup(tmp_path, raw):
    run_dir = _write_terminal_run(tmp_path, "bad-receipt", "2026-09-01T02:00:00")
    (run_dir / "run_summary.yaml").write_bytes(raw)

    result = prune_optimizer_runs(root=tmp_path)

    assert result.kept_diagnostics == ()
    assert result.removed == (run_dir.name,)


@pytest.mark.parametrize(
    "field", ["schema_version", "run_id", "candidate", "activated"]
)
def test_summary_receipt_requires_explicit_identity_and_audit_flags(tmp_path, field):
    run_dir = _write_terminal_run(tmp_path, "incomplete", "2026-09-01T02:00:00")
    path = run_dir / "run_summary.yaml"
    receipt = yaml.safe_load(path.read_text(encoding="utf-8"))
    receipt.pop(field)
    path.write_text(yaml.safe_dump(receipt), encoding="utf-8")

    result = prune_optimizer_runs(root=tmp_path)

    assert result.removed == (run_dir.name,)
    assert result.kept_diagnostics == ()


@pytest.mark.parametrize("case", ["legacy", "foreign_id", "not_failed", "mixed_groups"])
def test_invalid_guard_receipt_cannot_qualify_for_retention(tmp_path, case):
    run_dir = _write_terminal_run(
        tmp_path,
        "bad-guard",
        "2026-09-01T02:00:00",
        status="failed",
        group_status="interrupted",
        receipt_name="optimizer_failure.yaml",
    )
    path = run_dir / "optimizer_failure.yaml"
    receipt = yaml.safe_load(path.read_text(encoding="utf-8"))
    if case == "legacy":
        receipt.pop("schema_version")
        receipt.pop("run_id")
    elif case == "foreign_id":
        receipt["run_id"] = "some-other-run"
    elif case == "not_failed":
        receipt["status"] = "completed"
    else:
        receipt["groups"]["us"] = {"status": "failed"}
    path.write_text(yaml.safe_dump(receipt), encoding="utf-8")

    result = prune_optimizer_runs(root=tmp_path)

    assert result.removed == (run_dir.name,)
    assert result.kept_diagnostics == ()


@pytest.mark.parametrize("conflict", ["different_market", "foreign_id"])
def test_conflicting_receipts_cannot_preserve_a_run(tmp_path, conflict):
    run_dir = _write_terminal_run(tmp_path, "conflict", "2026-09-01T02:00:00")
    _write_terminal_run(
        tmp_path,
        run_dir.name,
        "2026-09-01T03:00:00",
        status="failed",
        group_status="interrupted",
        receipt_name="optimizer_failure.yaml",
        group="us" if conflict == "different_market" else "hk",
    )
    if conflict == "foreign_id":
        path = run_dir / "optimizer_failure.yaml"
        receipt = yaml.safe_load(path.read_text(encoding="utf-8"))
        receipt["run_id"] = "foreign"
        path.write_text(yaml.safe_dump(receipt), encoding="utf-8")

    result = prune_optimizer_runs(root=tmp_path)

    assert result.removed == (run_dir.name,)
    assert result.kept_diagnostics == ()


def test_same_market_receipts_use_one_slot_and_the_latest_terminal_time(tmp_path):
    _write_terminal_run(tmp_path, "older", "2026-09-01T20:00:00+08:00")
    newer = _write_terminal_run(tmp_path, "newer", "2026-09-01T19:00:00+08:00")
    _write_terminal_run(
        tmp_path,
        newer.name,
        "2026-09-01T13:00:00Z",
        status="failed",
        group_status="interrupted",
        receipt_name="optimizer_failure.yaml",
    )

    result = prune_optimizer_runs(root=tmp_path, keep_completed=1)

    assert result.kept_diagnostics == (newer.name,)
    assert result.kept_complete == ()
    assert result.removed == ("older",)


def test_receipt_only_run_is_never_an_activatable_candidate(tmp_path):
    run_dir = _write_terminal_run(
        tmp_path, "audit-only", "2026-09-01T02:00:00", group_status="completed"
    )
    result = prune_optimizer_runs(root=tmp_path, keep_completed=1)

    assert result.kept_diagnostics == (run_dir.name,)
    assert result.kept_complete == ()
    assert not activate_run(run_dir.name, group="hk", root=tmp_path)
    assert load_latest_strategy_run(groups=("hk",), root=tmp_path) is None
    assert not (run_dir / "manifest.yaml").exists()
    assert not (tmp_path / "latest_strategy.yaml").exists()


@pytest.mark.parametrize("with_receipt", [False, True])
def test_legacy_manifest_is_not_migrated_or_treated_as_a_candidate(
    tmp_path, with_receipt
):
    run_dir = _write_run(tmp_path, "legacy", "2026-09-01T02:00:00", group="hk")
    path = run_dir / "manifest.yaml"
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    manifest["schema_version"] = 3
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    original = path.read_bytes()
    if with_receipt:
        _write_terminal_run(tmp_path, run_dir.name, "2026-09-01T02:00:00")

    result = prune_optimizer_runs(root=tmp_path)

    assert result.kept_complete == ()
    if with_receipt:
        assert result.kept_diagnostics == (run_dir.name,)
        assert path.read_bytes() == original
        assert not activate_run(run_dir.name, group="hk", root=tmp_path)
    else:
        assert result.removed == (run_dir.name,)


def test_diagnostics_do_not_displace_protected_active_artifacts(tmp_path):
    old = _write_run(tmp_path, "old-active", "2026-07-01T02:00:00", activated=True)
    (tmp_path / "latest_strategy.yaml").write_bytes(
        (old / "manifest.yaml").read_bytes()
    )
    for index in range(3):
        _write_terminal_run(
            tmp_path,
            f"audit-{index}",
            f"2026-09-0{index + 1}T02:00:00",
            group="a_share",
        )

    result = prune_optimizer_runs(root=tmp_path, keep_completed=3)

    assert result.kept_complete == ()
    assert result.kept_diagnostics == ("audit-2", "audit-1", "audit-0")
    assert result.protected == (old.name,)
    assert (old / "a_share_best_params.yaml").is_file()


def test_malformed_candidate_artifact_does_not_hide_valid_terminal_receipt(tmp_path):
    run_dir = _write_run(
        tmp_path, "broken-candidate", "2026-09-01T02:00:00", group="hk"
    )
    artifact_path = run_dir / "hk_best_params.yaml"
    artifact = yaml.safe_load(artifact_path.read_text(encoding="utf-8"))
    artifact["schema_version"] = "invalid"
    artifact_path.write_text(yaml.safe_dump(artifact), encoding="utf-8")
    _write_terminal_run(tmp_path, run_dir.name, "2026-09-01T02:00:00")

    result = prune_optimizer_runs(root=tmp_path, keep_completed=1)

    assert result.kept_complete == ()
    assert result.kept_diagnostics == (run_dir.name,)
    assert artifact_path.is_file()


def test_prune_never_removes_children_identified_as_symlinks(tmp_path, monkeypatch):
    linked = tmp_path / "runs" / "linked-run"
    linked.mkdir(parents=True)
    sentinel = linked / "do-not-delete.txt"
    sentinel.write_text("external diagnostics", encoding="utf-8")
    partial = tmp_path / "runs" / "partial"
    partial.mkdir()
    original_is_symlink = Path.is_symlink
    # Model link metadata without requiring Windows symlink privileges.
    monkeypatch.setattr(
        Path, "is_symlink", lambda path: path == linked or original_is_symlink(path)
    )

    result = prune_optimizer_runs(root=tmp_path)

    assert result.removed == (partial.name,)
    assert sentinel.read_text(encoding="utf-8") == "external diagnostics"


def test_receipt_symlink_does_not_qualify_a_partial_run(tmp_path, monkeypatch):
    run_dir = _write_terminal_run(tmp_path, "linked-receipt", "2026-09-01T02:00:00")
    receipt = run_dir / "run_summary.yaml"
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path, "is_symlink", lambda path: path == receipt or original_is_symlink(path)
    )

    result = prune_optimizer_runs(root=tmp_path)

    assert result.kept_diagnostics == ()
    assert result.removed == (run_dir.name,)


def test_prune_rejects_runs_root_resolving_outside_optimizer_root(
    tmp_path, monkeypatch
):
    root = tmp_path / "optimizer"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "do-not-delete.txt"
    sentinel.write_text("external diagnostics", encoding="utf-8")
    original_resolve = Path.resolve
    monkeypatch.setattr(
        Path,
        "resolve",
        lambda path: outside if path == root / "runs" else original_resolve(path),
    )

    with pytest.raises(ValueError, match="must stay under its root"):
        prune_optimizer_runs(root=root)

    assert sentinel.read_text(encoding="utf-8") == "external diagnostics"
