"""Regression coverage for scoped point-in-time backfill CLI options."""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path

import yaml


def _module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "backfill_point_in_time.py"
    spec = importlib.util.spec_from_file_location("point_in_time_backfill_cli", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scoped_config_preserves_base_and_adds_explicit_market_inputs():
    module = _module()
    base = {
        "point_in_time_data": {
            "output_dir": "data/original",
            "history_years": 6,
            "fx_symbols": ["USDCNY=X"],
        }
    }

    scoped = module._scoped_config(
        base,
        output_dir="data/value_hk_us",
        history_years=5,
        fx_symbols=["CNYHKD=X", "USDCNY=X"],
        market_only_symbols=["02800", "VOO", "02800"],
        hkex_report_kinds=["year"],
    )

    assert base["point_in_time_data"]["output_dir"] == "data/original"
    assert base["point_in_time_data"]["fx_symbols"] == ["USDCNY=X"]
    settings = scoped["point_in_time_data"]
    assert settings["output_dir"] == "data/value_hk_us"
    assert settings["history_years"] == 5
    assert settings["fx_symbols"] == ["USDCNY=X", "CNYHKD=X"]
    assert settings["market_only_symbols"] == ["02800", "VOO"]
    assert settings["hkex_report_kinds"] == ["year"]


def test_cli_routes_scoped_codes_and_evaluation_date(tmp_path, monkeypatch):
    module = _module()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({"point_in_time_data": {"history_years": 6}}),
        encoding="utf-8",
    )
    captured = {}

    class Service:
        def __init__(self, config):
            captured["config"] = config

        def run(self, *, codes, evaluation_date):
            captured["codes"] = codes
            captured["evaluation_date"] = evaluation_date
            return {"market_history_failed": 0}

    monkeypatch.setattr(module, "PointInTimeBackfillService", Service)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "backfill_point_in_time.py",
            "--config",
            str(config_path),
            "--codes",
            "00700",
            "GOOG",
            "--output-dir",
            "data/scoped",
            "--history-years",
            "5",
            "--evaluation-date",
            "2026-08-24",
            "--fx-symbol",
            "CNYHKD=X",
            "--market-only-symbol",
            "02800",
            "--hkex-report-kind",
            "year",
        ],
    )

    assert module.main() == 0
    assert captured["codes"] == ["00700", "GOOG"]
    assert captured["evaluation_date"] == date(2026, 8, 24)
    assert captured["config"]["point_in_time_data"] == {
        "history_years": 5,
        "output_dir": "data/scoped",
        "fx_symbols": ["CNYHKD=X"],
        "market_only_symbols": ["02800"],
        "hkex_report_kinds": ["year"],
    }
