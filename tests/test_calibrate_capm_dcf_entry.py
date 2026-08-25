"""Market-isolation tests for the CAPM-DCF policy calibration command."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "calibrate_capm_dcf_entry.py"
    )
    spec = importlib.util.spec_from_file_location("capm_dcf_calibration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_frozen_policy_cannot_cross_market_boundaries(tmp_path):
    module = _module()
    report = tmp_path / "a_share_policy.json"
    report.write_text(
        json.dumps(
            {
                "contract": module.DCF_ENTRY_CALIBRATION_CONTRACT,
                "dataset": {"market": "a_share"},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="does not match calibration market"):
        module._load_frozen_policy(report, expected_market="hk")
