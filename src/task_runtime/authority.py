"""Bind explicit local authority without importing a host application."""

from __future__ import annotations

import json

from ..shared_contracts import AuthorityRef


def bind_authority(authority: AuthorityRef, *, expected_id: str | None = None) -> dict:
    if expected_id is not None and expected_id != authority.authority_id:
        raise ValueError("authority_identity_mismatch")
    root = authority.root
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise ValueError("authority_root_not_directory")
    path = root / "authority.json"
    identity = {
        "version": 1,
        "authority_id": authority.authority_id,
        "root": str(root),
        "isolated": authority.isolated,
    }
    try:
        with path.open("x", encoding="utf-8") as stream:
            json.dump(identity, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            import os

            os.fsync(stream.fileno())
    except FileExistsError:
        # A simultaneous creator can still be flushing. Retry only reading;
        # no existing identity is overwritten or silently repaired.
        import time

        for attempt in range(20):
            try:
                previous = json.loads(path.read_text(encoding="utf-8"))
                break
            except (ValueError, OSError):
                if attempt == 19:
                    raise ValueError("authority_manifest_unreadable") from None
                time.sleep(0.01)
        if previous != identity:
            raise ValueError("authority_manifest_mismatch")
    return authority.to_dict()
