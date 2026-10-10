"""Stable evidence before mutable task state, without removing any data."""

from __future__ import annotations

import json


def evidence_first(packet: dict) -> dict:
    """Keep supplied source/fact order; move changing coverage behind evidence.

    This preserves a reusable prefix only while that supplied evidence is
    unchanged. It does not claim provider cache hits or mix reviewer contexts.
    """
    keys = ["sources", "facts", "company"]
    keys += [key for key in packet if key not in keys]
    return {key: packet[key] for key in keys if key in packet}


def stable_envelope(envelope: dict) -> str:
    """Canonicalize each value, preserving deliberate top-level field order."""
    parts = []
    for key, value in envelope.items():
        if key == "packet":
            value = evidence_first(value)
            encoded = (
                "{"
                + ", ".join(
                    json.dumps(k)
                    + ": "
                    + json.dumps(v, ensure_ascii=False, sort_keys=True, allow_nan=False)
                    for k, v in value.items()
                )
                + "}"
            )
        else:
            encoded = json.dumps(
                value, ensure_ascii=False, sort_keys=True, allow_nan=False
            )
        parts.append(json.dumps(key) + ": " + encoded)
    return "{" + ", ".join(parts) + "}"
