#!/usr/bin/env python3
"""Fetch a dated Baostock industry snapshot for a reference universe.

Baostock supplies a current snapshot rather than a historical taxonomy tape.
The produced file is therefore valid only at and after each row's update date;
the industry feature builder enforces that restriction.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.fundamental_embedding.industry import (  # noqa: E402
    INDUSTRY_CLASSIFICATION_CONTRACT,
    IndustryClassification,
)


def _symbol(code: str) -> str | None:
    value = str(code).strip().lower()
    if "." not in value:
        return None
    result = value.split(".", 1)[1]
    return result if result.isdigit() and len(result) == 6 else None


def _requested_symbols(manifest: Path | None) -> set[str]:
    if manifest is None:
        return set()
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    if payload.get("contract") != "index-reference-universe-1":
        raise ValueError("unsupported reference universe manifest")
    return {str(item["code"]) for item in payload.get("companies", [])}


def fetch_classifications(
    requested_symbols: set[str] | None = None,
) -> list[IndustryClassification]:
    """Fetch labels once and retain the source-provided effective date."""

    try:
        import baostock as bs
    except ImportError as exc:  # pragma: no cover - deployment dependency
        raise RuntimeError("baostock is required to fetch industry labels") from exc

    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"baostock login failed: {login.error_msg}")
    try:
        query = bs.query_stock_industry()
        if query.error_code != "0":
            raise RuntimeError(f"baostock industry query failed: {query.error_msg}")
        fields = {name: index for index, name in enumerate(query.fields)}
        required = {"updateDate", "code", "industry", "industryClassification"}
        missing = sorted(required - fields.keys())
        if missing:
            raise RuntimeError(f"baostock industry response missing: {missing}")
        records: dict[str, IndustryClassification] = {}
        while query.next():
            row = query.get_row_data()
            symbol = _symbol(row[fields["code"]])
            if symbol is None or (requested_symbols and symbol not in requested_symbols):
                continue
            effective = row[fields["updateDate"]]
            try:
                label = IndustryClassification(
                    symbol=symbol,
                    industry_code=(row[fields["industry"]] or None),
                    industry_name=(row[fields["industry"]] or None),
                    taxonomy=(row[fields["industryClassification"]] or "unspecified"),
                    effective_from=datetime.strptime(
                        effective, "%Y-%m-%d"
                    ).date(),
                    source="baostock.query_stock_industry",
                )
            except (TypeError, ValueError):
                continue
            previous = records.get(symbol)
            if previous is None or previous.effective_from < label.effective_from:
                records[symbol] = label
        return sorted(records.values(), key=lambda item: item.symbol)
    finally:
        bs.logout()


def write_snapshot(
    output: Path,
    labels: list[IndustryClassification],
    requested_symbols: set[str],
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "contract": INDUSTRY_CLASSIFICATION_CONTRACT,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "source": "baostock.query_stock_industry",
        "semantics": (
            "current snapshot only; labels may be used only where "
            "effective_from <= evaluation_date"
        ),
        "requested_symbol_count": len(requested_symbols),
        "classified_symbol_count": len(labels),
        "missing_symbols": sorted(requested_symbols - {item.symbol for item in labels}),
        "classifications": [item.to_dict() for item in labels],
    }
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(output)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--universe-manifest")
    args = parser.parse_args()
    manifest = Path(args.universe_manifest) if args.universe_manifest else None
    requested = _requested_symbols(manifest)
    labels = fetch_classifications(requested)
    write_snapshot(Path(args.output), labels, requested)
    print(json.dumps({
        "output": str(Path(args.output).resolve()),
        "requested": len(requested),
        "classified": len(labels),
        "missing": len(requested - {item.symbol for item in labels}),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
