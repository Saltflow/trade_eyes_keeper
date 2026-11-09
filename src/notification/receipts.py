"""Small transport receipt contract; no retry or delivery inference lives here."""

from __future__ import annotations

import re
from collections.abc import Iterable

PIECE_STATUSES = frozenset({"sent", "failed", "partial", "unknown", "skipped"})


def public_deliveries(value) -> dict:
    """Keep typed delivery evidence, never raw provider text or recipients."""
    def receipt(item, depth=0):
        status = item.get("status")
        result = {
            "status": status
            if status in PIECE_STATUSES | {"not_requested"}
            else "unknown"
        }
        reason = item.get("reason")
        if isinstance(reason, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", reason):
            result["reason"] = reason
        for field in ("http_status", "smtp_code", "provider_code", "code"):
            if type(item.get(field)) is int:
                result[field] = item[field]
        if depth < 3 and isinstance(item.get("pieces"), list):
            result["pieces"] = [
                receipt(piece, depth + 1)
                for piece in item["pieces"][:100]
                if isinstance(piece, dict)
            ]
        return result

    if not isinstance(value, dict):
        return {}
    return {
        channel: receipt(value[channel])
        for channel in ("email", "feishu", "telegram")
        if isinstance(value.get(channel), dict)
    }


def normalize_receipt(value) -> dict:
    if isinstance(value, dict) and value.get("status") in PIECE_STATUSES:
        return dict(value)
    # Keep integrations that supply the established tuple acknowledgement.
    if isinstance(value, tuple) and value and isinstance(value[0], bool):
        return {
            "status": "sent" if value[0] else "unknown",
            "reason": str(value[1]) if len(value) > 1 else "legacy_receipt",
        }
    return {"status": "unknown", "reason": "transport_acknowledgement_missing"}


def combine_receipts(pieces: Iterable[dict]) -> dict:
    items = [normalize_receipt(piece) for piece in pieces]
    states = {piece["status"] for piece in items}
    if not states or states == {"skipped"}:
        status, reason = "skipped", "nothing_sent"
    elif "partial" in states or (
        "sent" in states and bool(states & {"failed", "unknown", "skipped"})
    ):
        status, reason = "partial", "some_pieces_not_confirmed"
    elif "unknown" in states:
        status, reason = "unknown", "delivery_result_unknown"
    elif "failed" in states:
        status, reason = "failed", "delivery_failed"
    else:
        status, reason = "sent", "delivery_acknowledged"
    detail = next(
        (
            piece["detail"]
            for piece in items
            if piece["status"] not in {"sent", "skipped"} and piece.get("detail")
        ),
        "ok" if status == "sent" else reason,
    )
    return {"status": status, "reason": reason, "detail": detail, "pieces": items}


def legacy_tuple(result: dict) -> tuple[bool, str]:
    return result.get("status") == "sent", str(
        result.get("detail") or result.get("reason") or "unknown"
    )
