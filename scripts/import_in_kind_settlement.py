"""Import an explicitly supplied, hash-bound HK distribution settlement receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data.in_kind_distributions import (
    in_kind_distribution_issues,
    match_hkex_in_kind_notice,
    verify_settlement_evidence,
)
from src.data.market_history import CorporateAction, PointInTimeMarketStore


def _bounded_bytes(path: Path, limit: int) -> bytes:
    with path.open("rb") as source:
        content = source.read(limit + 1)
    if len(content) > limit:
        raise ValueError(f"input exceeds {limit} bytes: {path.name}")
    return content


def import_settlement(
    store_root: Path,
    code: str,
    ex_date: date,
    receipt_path: Path,
    expected_sha256: str,
    *,
    apply: bool = False,
    declaration_json: Path | None = None,
    declaration_source: Path | None = None,
    reconcile_estimated: bool = False,
) -> dict:
    """Update exactly one cached action, preserving prices and all other actions.

    Unknown declaration terms require an explicit previously reviewed action
    JSON and its actual source text. Those terms are re-parsed, not accepted
    from arbitrary JSON assignments. Private receipt discovery is never used.
    An estimated event requires explicit reconciliation. Partial actual facts
    disable its estimated execution fallback until confirmed terms are complete.
    """
    if not re.fullmatch(r"\d{5}", str(code)):
        raise ValueError("an explicit five-digit HK stock code is required")
    store = PointInTimeMarketStore(store_root)
    action_path = store.market_dir / f"{code}.actions.json"
    original = _bounded_bytes(action_path, 4 * 1024 * 1024)
    records = json.loads(original.decode("utf-8"))
    if not isinstance(records, list):
        raise TypeError("action cache must be a JSON array")
    actions = [CorporateAction(**row) for row in records]
    matches = [
        i
        for i, action in enumerate(actions)
        if action.code == code and action.ex_date == ex_date
    ]
    if len(matches) != 1:
        raise ValueError("cached distribution event is missing or ambiguous")
    index = matches[0]
    action = actions[index]
    if action.action_type != "in_kind_distribution":
        raise ValueError("selected cached action is not an in-kind distribution")
    if (declaration_json is None) != (declaration_source is None):
        raise ValueError("declaration JSON and source text must be supplied together")
    if declaration_json is not None:
        declared = CorporateAction(
            **json.loads(_bounded_bytes(declaration_json, 64 * 1024).decode("utf-8"))
        )
        terms = declared.in_kind_terms
        if declared.code != code or declared.ex_date != ex_date or terms is None:
            raise ValueError("reviewed declaration instrument/event mismatch")
        source_text = (
            _bounded_bytes(declaration_source, 2 * 1024 * 1024)
            .decode("utf-8-sig")
            .replace("\r\n", "\n")
        )
        if hashlib.sha256(source_text.encode()).hexdigest() != terms.source_sha256:
            raise ValueError("reviewed declaration source SHA256 mismatch")
        parsed = match_hkex_in_kind_notice(
            source_text,
            action,
            published_at=terms.evidence_published_at,
            source_url=terms.source_url,
            listed_code=code,
        )
        if parsed is None:
            raise ValueError("reviewed source does not match the distribution event")
        if action.in_kind_terms is None:
            action = parsed
        elif action.in_kind_terms.source_sha256 != parsed.in_kind_terms.source_sha256:
            raise ValueError("declaration differs from existing evidenced cache")
    content = _bounded_bytes(receipt_path, 64 * 1024)
    previous_basis = (
        action.in_kind_terms.settlement_basis if action.in_kind_terms else None
    )
    verified = verify_settlement_evidence(
        content, expected_sha256, action, reconcile_estimated=reconcile_estimated
    )
    issues = in_kind_distribution_issues(verified)
    result = {
        "code": code,
        "ex_date": str(ex_date),
        "applied": False,
        "receipt_sha256": expected_sha256,
        "action_executable": not issues,
        "previous_settlement_basis": previous_basis,
        "settlement_basis": verified.in_kind_terms.settlement_basis,
        "confirmed_reconciliation_requested": reconcile_estimated is True,
        "confirmed_reconciliation_complete": not issues,
        "remaining_action_issues": list(issues),
        "bundle_validated": False,
        "prices_changed": False,
        "optimizer_executed": False,
        "pointers_changed": False,
    }
    if not apply:
        return result
    # Recheck the exact cache before replacing it; never discard concurrent work.
    if _bounded_bytes(action_path, 4 * 1024 * 1024) != original:
        raise ValueError("action cache changed during receipt validation")
    records[index] = json.loads(verified.json())
    updated = json.dumps(records, ensure_ascii=False, indent=2).encode("utf-8")
    if updated == original:
        result["applied"] = True
        return result
    digest = hashlib.sha256(original).hexdigest()
    backup = action_path.with_name(f"{code}.actions.before-in-kind-{digest}.json")
    if backup.exists():
        if backup.read_bytes() != original:
            raise ValueError("in-kind import backup integrity mismatch")
    else:
        with backup.open("xb") as stream:
            stream.write(original)
    temporary = action_path.with_suffix(".json.in-kind.tmp")
    try:
        temporary.write_bytes(updated)
        temporary.replace(action_path)
    finally:
        temporary.unlink(missing_ok=True)
    result.update(applied=True, backup_path=str(backup))
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store-root", type=Path, default=Path("data/point_in_time"))
    parser.add_argument("--code", required=True)
    parser.add_argument("--ex-date", type=date.fromisoformat, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--declaration-json", type=Path)
    parser.add_argument("--declaration-source", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--reconcile-estimated",
        action="store_true",
        help="Replace estimated execution with receipt-backed confirmed facts; incomplete facts remain non-executable",
    )
    args = parser.parse_args(argv)
    try:
        result = import_settlement(
            args.store_root,
            args.code,
            args.ex_date,
            args.receipt,
            args.expected_sha256,
            apply=args.apply,
            declaration_json=args.declaration_json,
            declaration_source=args.declaration_source,
            reconcile_estimated=args.reconcile_estimated,
        )
    except (OSError, ValueError, TypeError) as exc:
        parser.exit(1, f"Receipt import failed: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
