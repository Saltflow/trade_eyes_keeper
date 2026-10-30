"""Small transport receipt contract; no retry or delivery inference lives here."""

from __future__ import annotations

from collections.abc import Iterable

PIECE_STATUSES = frozenset({"sent", "failed", "partial", "unknown", "skipped"})


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
