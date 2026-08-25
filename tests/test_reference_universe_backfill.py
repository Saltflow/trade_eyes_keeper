from __future__ import annotations

import json
from datetime import date

import pytest

from src.data.reference_universe_backfill import (
    REFERENCE_MANIFEST_CONTRACT,
    ReferenceUniverseBackfillService,
    load_reference_manifest,
)


def _manifest() -> dict:
    return {
        "contract": REFERENCE_MANIFEST_CONTRACT,
        "as_of": "2026-08-18",
        "weight_as_of": "2026-07-31",
        "companies": [
            {"code": "000001", "memberships": ["csi_300"]},
            {"code": "600519", "memberships": ["csi_300"]},
        ],
    }


def _config() -> dict:
    return {
        "point_in_time_data": {
            "history_years": 6,
            "official_statement_crawlers": True,
            "official_pdf_recent_years": 3,
            "market_history": {"coverage_tolerance_days": 31},
        }
    }


def _success(code: str) -> dict:
    return {
        "code": code,
        "market": "a_share",
        "instrument_type": "equity",
        "market_history": {
            "status": "success",
            "requested_window_coverage": "full",
        },
        "statements": {
            "status": "success",
            "field_availability": {},
        },
    }


def test_manifest_loader_normalizes_order_and_rejects_duplicates(tmp_path):
    path = tmp_path / "manifest.json"
    payload = _manifest()
    payload["companies"].reverse()
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = load_reference_manifest(path)

    assert [item["code"] for item in loaded["companies"]] == [
        "000001",
        "600519",
    ]
    assert loaded["company_count"] == 2

    payload["companies"] = [
        {"code": "000001"},
        {"code": "000001"},
    ]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate company"):
        load_reference_manifest(path)


def test_batch_overrides_all_output_roots_and_caps_workers(tmp_path):
    service = ReferenceUniverseBackfillService(
        _config(),
        _manifest(),
        output_dir=tmp_path / "dataset",
        workers=1,
        worker=lambda payload: _success(payload["code"]),
    )

    settings = service.config["point_in_time_data"]
    assert settings["output_dir"] == str((tmp_path / "dataset").resolve())
    assert settings["cninfo_pdf_cache_dir"] == str(
        (tmp_path / "dataset" / "provider_cache" / "cninfo").resolve()
    )
    with pytest.raises(ValueError, match="between 1 and 4"):
        ReferenceUniverseBackfillService(
            _config(),
            _manifest(),
            output_dir=tmp_path,
            workers=5,
        )


def test_successful_checkpoints_are_reused_without_calling_worker(tmp_path):
    calls = []

    def fetch(payload):
        calls.append(payload["code"])
        return _success(payload["code"])

    service = ReferenceUniverseBackfillService(
        _config(),
        _manifest(),
        output_dir=tmp_path,
        workers=1,
        worker=fetch,
    )
    first = service.run(evaluation_date=date(2026, 8, 18))
    second = service.run(evaluation_date=date(2026, 8, 18))

    assert first["state"] == "complete"
    assert first["reused_checkpoint_count"] == 0
    assert second["state"] == "complete"
    assert second["reused_checkpoint_count"] == 2
    assert calls == ["000001", "600519"]
    progress = json.loads(
        (
            tmp_path
            / "reference_batches"
            / first["batch_id"]
            / "progress.json"
        ).read_text(encoding="utf-8")
    )
    assert progress == {
        **progress,
        "state": "complete",
        "total": 2,
        "completed": 2,
        "success": 2,
        "failed": 0,
        "pending": 0,
    }


def test_failed_checkpoint_is_retried_but_success_is_reused(tmp_path):
    attempts = {"000001": 0, "600519": 0}

    def flaky(payload):
        code = payload["code"]
        attempts[code] += 1
        if code == "600519" and attempts[code] == 1:
            row = _success(code)
            row["statements"] = {"status": "failed", "reason": "temporary"}
            return row
        return _success(code)

    service = ReferenceUniverseBackfillService(
        _config(),
        _manifest(),
        output_dir=tmp_path,
        workers=1,
        worker=flaky,
    )
    first = service.run(evaluation_date=date(2026, 8, 18))
    second = service.run(evaluation_date=date(2026, 8, 18))

    assert first["state"] == "partial"
    assert first["failed_codes"] == ["600519"]
    assert second["state"] == "complete"
    assert second["failed_codes"] == []
    assert second["reused_checkpoint_count"] == 1
    assert attempts == {"000001": 1, "600519": 2}


def test_subset_must_be_present_in_manifest(tmp_path):
    service = ReferenceUniverseBackfillService(
        _config(),
        _manifest(),
        output_dir=tmp_path,
        workers=1,
        worker=lambda payload: _success(payload["code"]),
    )
    with pytest.raises(ValueError, match="not present"):
        service.run(
            evaluation_date=date(2026, 8, 18),
            codes=["999999"],
        )
