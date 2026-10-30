#!/usr/bin/env python3
"""Overlay official H1 parent equity onto existing monitoring-list PIT data."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.instruments.models import FinancialStatementSnapshot
from src.instruments.point_in_time import (
    CninfoInterimReportProvider,
    PointInTimeFundamentalStore,
    merge_statement_sources,
)

OVERLAY_CONTRACT = "monitoring-h1-fundamental-overlay-1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_finite(value: object) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _normalise_codes(codes: Iterable[object]) -> list[str]:
    normalized = [str(code).strip() for code in codes if str(code).strip()]
    selected = list(dict.fromkeys(normalized))
    if not selected:
        raise ValueError("at least one --code is required")
    if any(not code.isdigit() or len(code) != 6 for code in selected):
        raise ValueError("all --codes must be six-digit A-share codes")
    return selected


def _scoped_config(config: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    """Return an in-memory config that isolates only CNINFO parse cache writes."""

    scoped = copy.deepcopy(config)
    settings = scoped.setdefault("point_in_time_data", {})
    if not isinstance(settings, dict):
        raise TypeError("point_in_time_data configuration must be a mapping")
    settings["output_dir"] = str(output_dir)
    settings["cninfo_pdf_cache_dir"] = str(
        output_dir / "provider_cache" / "cninfo"
    )
    return scoped


def _latest_h1(
    statements: Iterable[FinancialStatementSnapshot],
    *,
    year: int,
    as_of: date,
) -> FinancialStatementSnapshot | None:
    candidates = [
        item
        for item in statements
        if item.period_end == date(year, 6, 30)
        and item.period_type == "quarter"
        and item.is_cumulative
        and item.published_at is not None
        and item.published_at <= as_of
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (item.published_at or date.min, item.source),
    )


def _validate_base_h1(
    code: str,
    statements: Iterable[FinancialStatementSnapshot],
    *,
    as_of: date,
) -> FinancialStatementSnapshot:
    snapshot = _latest_h1(statements, year=as_of.year, as_of=as_of)
    if snapshot is None:
        raise ValueError(f"{code}: existing PIT data has no disclosed current H1")
    missing = [
        name
        for name in ("net_income_parent", "total_shares")
        if not _is_finite(getattr(snapshot, name))
    ]
    if missing:
        raise ValueError(
            f"{code}: existing H1 is incomplete for overlay: {', '.join(missing)}"
        )
    return snapshot


def _validate_supplement(
    code: str,
    statements: Iterable[FinancialStatementSnapshot],
    *,
    as_of: date,
) -> FinancialStatementSnapshot:
    candidates = [
        item
        for item in statements
        if item.period_end == date(as_of.year, 6, 30)
        and item.period_type == "quarter"
        and item.is_cumulative
        and item.published_at is not None
        and item.published_at <= as_of
        and "cninfo_interim_report" in item.source.split("+")
        and _is_finite(item.parent_equity)
        and bool(item.source_url)
    ]
    if not candidates:
        raise ValueError(
            f"{code}: official H1 parent-equity supplement is missing or invalid"
        )
    return max(
        candidates,
        key=lambda item: (item.published_at or date.min, item.source),
    )


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def refresh_monitoring_h1_fundamentals(
    config: dict[str, Any],
    *,
    codes: Iterable[object],
    base_data_root: str | Path,
    output_dir: str | Path,
    as_of: date,
    provider: Any | None = None,
) -> dict[str, Any]:
    """Create a fail-closed, fundamental-only H1 overlay in a new directory.

    This function deliberately does not import or invoke a market-history
    provider.  It only reads existing statement files and retrieves official
    interim reports for the explicitly supplied company codes.
    """

    selected_codes = _normalise_codes(codes)
    base_root = Path(base_data_root).resolve()
    target_root = Path(output_dir).resolve()
    if target_root.exists():
        raise FileExistsError(
            f"refusing to overwrite existing isolated output: {target_root}"
        )
    if not base_root.exists():
        raise FileNotFoundError(f"base PIT root does not exist: {base_root}")

    scoped = _scoped_config(config, target_root)
    interim_provider = provider or CninfoInterimReportProvider(scoped)
    fetch_start = date(as_of.year, 1, 1)

    supplements: dict[str, FinancialStatementSnapshot] = {}
    attempts_by_code: dict[str, list[dict[str, Any]]] = {}
    for code in selected_codes:
        result = interim_provider.fetch(code, fetch_start, as_of)
        supplements[code] = _validate_supplement(
            code,
            result.statements,
            as_of=as_of,
        )
        attempts_by_code[code] = list(result.attempts)

    base_store = PointInTimeFundamentalStore(base_root)
    planned: list[dict[str, Any]] = []
    for code in selected_codes:
        base_path = base_store._path(code)
        if not base_path.exists():
            raise FileNotFoundError(f"{code}: base statement file is missing")
        base_statements = base_store.read_all(code)
        base_h1 = _validate_base_h1(code, base_statements, as_of=as_of)
        supplement = supplements[code]
        merged = merge_statement_sources(base_statements, [supplement])
        merged_h1 = _latest_h1(merged, year=as_of.year, as_of=as_of)
        if (
            merged_h1 is None
            or "cninfo_interim_report" not in merged_h1.source.split("+")
            or not _is_finite(merged_h1.parent_equity)
            or not _is_finite(merged_h1.net_income_parent)
            or not _is_finite(merged_h1.total_shares)
        ):
            raise ValueError(f"{code}: H1 overlay did not preserve required fields")
        planned.append(
            {
                "code": code,
                "base_path": base_path,
                "base_sha256": _sha256(base_path),
                "base_h1": base_h1,
                "supplement": supplement,
                "merged": merged,
            }
        )

    output_store = PointInTimeFundamentalStore(target_root)
    rows: list[dict[str, Any]] = []
    for plan in planned:
        code = str(plan["code"])
        output_path = output_store.upsert(code, plan["merged"])
        rows.append(
            {
                "code": code,
                "base_statement_file": str(plan["base_path"]),
                "base_statement_sha256": plan["base_sha256"],
                "base_h1_published_at": plan["base_h1"].published_at.isoformat(),
                "official_source_url": plan["supplement"].source_url,
                "official_h1_published_at": (
                    plan["supplement"].published_at.isoformat()
                ),
                "period_end": plan["supplement"].period_end.isoformat(),
                "parent_equity": plan["supplement"].parent_equity,
                "output_statement_file": str(output_path),
                "output_statement_sha256": _sha256(output_path),
                "source_attempts": attempts_by_code[code],
            }
        )

    for row in rows:
        verified = _latest_h1(
            output_store.as_of(str(row["code"]), as_of),
            year=as_of.year,
            as_of=as_of,
        )
        if (
            verified is None
            or "cninfo_interim_report" not in verified.source.split("+")
            or not _is_finite(verified.parent_equity)
            or verified.published_at is None
            or verified.published_at.isoformat()
            != row["official_h1_published_at"]
        ):
            raise ValueError(
                f"{row['code']}: output PIT verification did not pass"
            )
        row["verified_h1_source"] = verified.source
        row["verified_h1_published_at"] = verified.published_at.isoformat()
        row["verified_parent_equity"] = verified.parent_equity

    report = {
        "contract": OVERLAY_CONTRACT,
        "status": "success",
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
        "as_of": as_of.isoformat(),
        "codes": selected_codes,
        "base_data_root": str(base_root),
        "output_dir": str(target_root),
        "market_history_touched": False,
        "market_history_downloaded": False,
        "historical_backfill_run": False,
        "active_config_mutated": False,
        "portfolio_optimization_run": False,
        "allocation_conclusion": "not_defined",
        "rows": rows,
    }
    _atomic_json(target_root / "run_summary.json", report)
    report["run_summary"] = str(target_root / "run_summary.json")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Overlay official H1 parent equity on existing monitoring-list PIT "
            "statement data without market-history downloads or backfills"
        )
    )
    parser.add_argument(
        "--config",
        default=str(ROOT / "config" / "config.yaml"),
        help="configuration file read into memory only",
    )
    parser.add_argument("--codes", nargs="+", required=True)
    parser.add_argument(
        "--base-data-root",
        default=str(ROOT / "data" / "point_in_time"),
        help="existing PIT fundamental root to read only",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="new isolated output root; existing directories are refused",
    )
    parser.add_argument("--as-of", type=date.fromisoformat, required=True)
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    with config_path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    if not isinstance(config, dict):
        raise TypeError("configuration root must be a mapping")
    try:
        report = refresh_monitoring_h1_fundamentals(
            config,
            codes=args.codes,
            base_data_root=args.base_data_root,
            output_dir=args.output_dir,
            as_of=args.as_of,
        )
    except Exception as exc:  # noqa: BLE001 - CLI emits a machine-readable failure
        print(
            json.dumps(
                {
                    "contract": OVERLAY_CONTRACT,
                    "status": "failed",
                    "error": str(exc),
                    "market_history_touched": False,
                    "portfolio_optimization_run": False,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
