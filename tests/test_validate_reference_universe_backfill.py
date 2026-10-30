"""Acceptance checks for the completed current-reference data validator."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from scripts.validate_reference_universe_backfill import (
    validate_reference_universe_backfill,
)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _prepare_complete_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    manifest_path = tmp_path / "manifest.json"
    data_root = tmp_path / "dataset"
    snapshot_path = tmp_path / "industry.json"
    codes = ["000001", "600519"]
    _write_json(
        manifest_path,
        {
            "contract": "index-reference-universe-1",
            "as_of": "2026-09-18",
            "companies": [
                {"code": code, "memberships": ["csi_300"]} for code in codes
            ],
        },
    )
    instruments = []
    for code in codes:
        _write_json(
            data_root / "market" / f"{code}.meta.json",
            {
                "contract": "raw-and-qfq-1",
                "end": "2026-09-18",
                "diagnostics": [],
            },
        )
        (data_root / "market").mkdir(parents=True, exist_ok=True)
        (data_root / "market" / f"{code}.csv").write_text(
            "date,raw_close,qfq_close\n2026-09-18,10,10\n",
            encoding="utf-8",
        )
        _write_json(
            data_root / "fundamentals" / f"{code}.statements.json",
            {
                "contract": "point-in-time-statements-1",
                "statements": [
                    {
                        "period_end": "2026-06-30",
                        "published_at": "2026-08-30",
                    }
                ],
            },
        )
        instruments.append(
            {
                "code": code,
                "market_history": {"status": "success"},
                "statements": {
                    "status": "success",
                    "all_have_publication_date": True,
                },
            }
        )
    _write_json(
        data_root / "latest_reference_backfill.json",
        {
            "state": "complete",
            "requested_company_count": len(codes),
            "checkpoint_count": len(codes),
            "failed_codes": [],
            "instruments": instruments,
        },
    )
    _write_json(
        snapshot_path,
        {
            "contract": "industry-classification-snapshot-1",
            "classifications": [
                {
                    "symbol": code,
                    "industry_code": "C39",
                    "effective_from": "2026-09-14",
                }
                for code in codes
            ],
        },
    )
    return manifest_path, data_root, snapshot_path


def test_complete_fixture_passes_and_reports_action_gate_separately(tmp_path: Path):
    manifest_path, data_root, snapshot_path = _prepare_complete_fixture(tmp_path)

    result = validate_reference_universe_backfill(
        manifest_path=manifest_path,
        data_root=data_root,
        evaluation_date=date(2026, 9, 18),
        industry_snapshot=snapshot_path,
    )

    assert result["passes"] is True
    assert result["industry"]["usable_label_count"] == 2
    assert result["execution_action_gate"]["strict_ready"] is True
    assert result["fundamental_freshness"]["current_count"] == 2

    metadata_path = data_root / "market" / "000001.meta.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["diagnostics"] = ["corporate_action_fallback_unresolved:example"]
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    with_action_issue = validate_reference_universe_backfill(
        manifest_path=manifest_path,
        data_root=data_root,
        evaluation_date=date(2026, 9, 18),
        industry_snapshot=snapshot_path,
    )

    assert with_action_issue["passes"] is True
    assert with_action_issue["execution_action_gate"]["strict_ready"] is False


def test_fundamental_freshness_is_reported_separately_from_storage_acceptance(
    tmp_path: Path,
):
    manifest_path, data_root, snapshot_path = _prepare_complete_fixture(tmp_path)
    statement_path = data_root / "fundamentals" / "000001.statements.json"
    statement = json.loads(statement_path.read_text(encoding="utf-8"))
    statement["statements"][0]["period_end"] = "2024-12-31"
    statement["statements"][0]["published_at"] = "2025-03-21"
    statement_path.write_text(json.dumps(statement), encoding="utf-8")

    result = validate_reference_universe_backfill(
        manifest_path=manifest_path,
        data_root=data_root,
        evaluation_date=date(2026, 9, 18),
        industry_snapshot=snapshot_path,
        max_fundamental_age_days=400,
    )

    assert result["passes"] is True
    assert result["fundamental_freshness"]["current_codes"] == ["600519"]
    assert result["fundamental_freshness"]["stale_or_unavailable_codes"] == [
        "000001"
    ]
