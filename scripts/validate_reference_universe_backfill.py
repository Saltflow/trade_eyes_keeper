#!/usr/bin/env python3
"""Audit a completed current-reference PIT backfill without mutating it."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.data.reference_universe_backfill import load_reference_manifest

ACCEPTANCE_CONTRACT = "reference-universe-backfill-acceptance-1"


def _safe_code(code: str) -> str:
    return str(code).replace(".", "-").replace("/", "-")


def _read_json(path: Path) -> dict[str, Any] | list[Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None


def _report_items(report: object) -> dict[str, dict[str, Any]]:
    if not isinstance(report, dict):
        return {}
    rows = report.get("instruments", [])
    if not isinstance(rows, list):
        return {}
    return {
        str(item.get("code")): item for item in rows if isinstance(item, dict)
    }


def _report_is_complete(report: object, expected_count: int) -> bool:
    if not isinstance(report, dict) or report.get("state") != "complete":
        return False
    try:
        return (
            int(report.get("requested_company_count", -1)) == expected_count
            and int(report.get("checkpoint_count", -1)) == expected_count
            and not report.get("failed_codes")
        )
    except (TypeError, ValueError):
        return False


def _latest_statement_as_of(
    statements: object, evaluation_date: date
) -> tuple[date, date] | None:
    """Return the latest dated financial period known at ``evaluation_date``."""

    if not isinstance(statements, dict):
        return None
    rows = statements.get("statements", [])
    if not isinstance(rows, list):
        return None
    candidates: list[tuple[date, date]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        try:
            period_end = date.fromisoformat(str(item["period_end"]))
            published_at = date.fromisoformat(str(item["published_at"]))
        except (KeyError, TypeError, ValueError):
            continue
        if period_end <= evaluation_date and published_at <= evaluation_date:
            candidates.append((period_end, published_at))
    return max(candidates, default=None)


def validate_reference_universe_backfill(
    *,
    manifest_path: Path,
    data_root: Path,
    evaluation_date: date,
    industry_snapshot: Path | None = None,
    max_fundamental_age_days: int = 400,
) -> dict[str, object]:
    """Return a conservative completeness receipt for a fixed current universe."""

    if max_fundamental_age_days < 0:
        raise ValueError("max_fundamental_age_days must be non-negative")

    manifest = load_reference_manifest(manifest_path)
    codes = sorted(str(item["code"]) for item in manifest["companies"])
    report_path = data_root / "latest_reference_backfill.json"
    report = _read_json(report_path)
    report_items = _report_items(report)
    missing_market: list[str] = []
    invalid_market: list[str] = []
    stale_market: list[str] = []
    missing_statements: list[str] = []
    undated_statements: list[str] = []
    report_failures: list[str] = []
    unresolved_action_diagnostics: list[str] = []
    stale_fundamental_codes: list[str] = []
    current_fundamental_codes: list[str] = []

    for code in codes:
        stem = _safe_code(code)
        meta_path = data_root / "market" / f"{stem}.meta.json"
        price_path = data_root / "market" / f"{stem}.csv"
        statement_path = data_root / "fundamentals" / f"{stem}.statements.json"
        metadata = _read_json(meta_path)
        if not isinstance(metadata, dict) or not price_path.exists():
            missing_market.append(code)
        else:
            if metadata.get("contract") != "raw-and-qfq-1":
                invalid_market.append(code)
            try:
                market_end = date.fromisoformat(str(metadata["end"]))
            except (KeyError, TypeError, ValueError):
                invalid_market.append(code)
            else:
                if market_end < evaluation_date:
                    stale_market.append(code)
            diagnostics = metadata.get("diagnostics") or []
            if any(
                "corporate_action_fallback_unresolved" in str(item)
                for item in diagnostics
            ):
                unresolved_action_diagnostics.append(code)

        statements = _read_json(statement_path)
        if not isinstance(statements, dict) or not statements.get("statements"):
            missing_statements.append(code)
        elif (
            statements.get("contract") != "point-in-time-statements-1"
            or any(
                not isinstance(item, dict)
                or not item.get("period_end")
                or not item.get("published_at")
                for item in statements.get("statements", [])
            )
        ):
            undated_statements.append(code)
        else:
            latest_statement = _latest_statement_as_of(statements, evaluation_date)
            if (
                latest_statement is None
                or (evaluation_date - latest_statement[0]).days
                > max_fundamental_age_days
            ):
                stale_fundamental_codes.append(code)
            else:
                current_fundamental_codes.append(code)

        item = report_items.get(code)
        if (
            not isinstance(item, dict)
            or item.get("market_history", {}).get("status") != "success"
            or item.get("statements", {}).get("status") != "success"
            or not item.get("statements", {}).get("all_have_publication_date")
        ):
            report_failures.append(code)

    report_complete = _report_is_complete(report, len(codes))
    industry_result: dict[str, object] = {"provided": industry_snapshot is not None}
    if industry_snapshot is not None:
        snapshot = _read_json(industry_snapshot)
        labels = (
            snapshot.get("classifications", [])
            if isinstance(snapshot, dict)
            and snapshot.get("contract") == "industry-classification-snapshot-1"
            else []
        )
        usable = {
            str(item.get("symbol"))
            for item in labels
            if isinstance(item, dict)
            and item.get("industry_code")
            and item.get("effective_from")
            and str(item.get("effective_from")) <= evaluation_date.isoformat()
        }
        industry_result.update(
            {
                "contract_valid": bool(labels),
                "usable_label_count": len(usable & set(codes)),
                "missing_codes": sorted(set(codes) - usable),
            }
        )

    passes = not any(
        (
            not report_complete,
            missing_market,
            invalid_market,
            stale_market,
            missing_statements,
            undated_statements,
            report_failures,
            industry_result.get("missing_codes", []),
        )
    )
    return {
        "contract": ACCEPTANCE_CONTRACT,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "passes": passes,
        "manifest": {
            "path": str(manifest_path),
            "as_of": manifest.get("as_of"),
            "company_count": len(codes),
        },
        "data_root": str(data_root),
        "evaluation_date": evaluation_date.isoformat(),
        "reference_batch_report": {
            "path": str(report_path),
            "complete": report_complete,
            "state": report.get("state") if isinstance(report, dict) else None,
        },
        "market": {
            "missing_codes": missing_market,
            "invalid_contract_codes": invalid_market,
            "stale_codes": stale_market,
        },
        "statements": {
            "missing_codes": missing_statements,
            "invalid_or_undated_codes": undated_statements,
            "report_failure_codes": report_failures,
        },
        "fundamental_freshness": {
            "maximum_period_age_days": max_fundamental_age_days,
            "current_codes": current_fundamental_codes,
            "current_count": len(current_fundamental_codes),
            "stale_or_unavailable_codes": stale_fundamental_codes,
            "stale_or_unavailable_count": len(stale_fundamental_codes),
            "interpretation": (
                "informational coverage only; the Ke evaluator separately "
                "requires fresh ROE and PB evidence for every selected name"
            ),
        },
        "industry": industry_result,
        "execution_action_gate": {
            "strict_ready": not unresolved_action_diagnostics,
            "unresolved_diagnostic_codes": unresolved_action_diagnostics,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--evaluation-date", type=date.fromisoformat, required=True)
    parser.add_argument("--industry-snapshot", type=Path)
    parser.add_argument(
        "--max-fundamental-age-days",
        type=int,
        default=400,
        help="report statement-period freshness coverage at this age limit",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    result = validate_reference_universe_backfill(
        manifest_path=args.manifest,
        data_root=args.data_root,
        evaluation_date=args.evaluation_date,
        industry_snapshot=args.industry_snapshot,
        max_fundamental_age_days=args.max_fundamental_age_days,
    )
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text)
    return 0 if result["passes"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
