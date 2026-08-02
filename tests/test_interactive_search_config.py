from __future__ import annotations

from pathlib import Path
import shutil

import yaml

from src.interactive.commands import handlers


def _isolated_constraints(tmp_path: Path, monkeypatch) -> Path:
    source = Path("config/optimizer_constraints.yaml")
    target = tmp_path / "optimizer_constraints.yaml"
    shutil.copy2(source, target)
    monkeypatch.setattr(handlers, "OPT_CONSTRAINTS_PATH", target)
    return target


def test_config_updates_effective_solver_gate_and_runtime(tmp_path, monkeypatch):
    path = _isolated_constraints(tmp_path, monkeypatch)

    assert "✅" in handlers.handle_config("set", "solver", "random")
    assert "✅" in handlers.handle_config("set", "budget", "12000")
    assert "✅" in handlers.handle_config(
        "set",
        "positive_windows",
        "5",
    )
    assert "✅" in handlers.handle_config(
        "set",
        "majority_windows",
        "6",
    )
    assert "✅" in handlers.handle_config("set", "workers", "auto")
    assert "✅" in handlers.handle_config("set", "batch_size", "128")

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["search"]["solver_id"] == "random"
    assert raw["search"]["solvers"]["random"]["budget"] == 12000
    assert raw["search"]["workers"] is None
    assert raw["search"]["batch_size"] == 128
    standard = {
        rule["id"]: rule["value"]
        for rule in raw["gate_profiles"]["standard"]["rules"]
    }
    assert standard["positive_return_windows"] == 5
    assert standard["majority_benchmark_win_windows"] == 6


def test_invalid_config_is_rejected_without_replacing_file(tmp_path, monkeypatch):
    path = _isolated_constraints(tmp_path, monkeypatch)
    before = path.read_bytes()

    response = handlers.handle_config("set", "batch_size", "12")

    assert response.startswith("❌")
    assert path.read_bytes() == before
    assert not path.with_suffix(".yaml.tmp").exists()


def test_obsolete_window_mutation_is_not_exposed(tmp_path, monkeypatch):
    _isolated_constraints(tmp_path, monkeypatch)

    response = handlers.handle_config("set", "data_years", "3")

    assert response.startswith("❌")
    assert "data_years" not in handlers._CONFIG_HELP
