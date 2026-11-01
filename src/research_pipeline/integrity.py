"""Read-only verification of run-bound input and discovery snapshots."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .common import file_sha


def _sha(value) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _absolute(value) -> Path:
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise ValueError("absolute_path_required")
    return Path(value)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_manifest_key")
        result[key] = value
    return result


def verify_input_manifests(run: dict) -> None:
    """Verify every bound manifest and every declared file, without mutations.

    Runs without this newly introduced field remain readable for migration.
    Once a binding exists, a missing, relative, malformed, or changed entry is
    an integrity failure; no listed file is optional and no new hash is adopted.
    """
    try:
        if not isinstance(run, dict):
            raise TypeError("run_record_required")
        bindings = run.get("integrity_manifests", [])
        if not isinstance(bindings, list):
            raise TypeError("manifest_bindings_must_be_list")
        for binding in bindings:
            if not isinstance(binding, dict) or not _sha(binding.get("sha256")):
                raise ValueError("invalid_manifest_binding")
            manifest = _absolute(binding.get("path"))
            raw = manifest.read_bytes()
            if hashlib.sha256(raw).hexdigest() != binding["sha256"]:
                raise ValueError("manifest_hash_mismatch")
            value = json.loads(raw, object_pairs_hook=_unique)
            if not isinstance(value, dict) or not isinstance(value.get("files"), dict):
                raise TypeError("manifest_files_mapping_required")
            for name, expected in value["files"].items():
                path = _absolute(name)
                if not _sha(expected):
                    raise ValueError("invalid_input_digest")
                if not path.is_file() or file_sha(path) != expected:
                    raise ValueError("mandatory_input_missing_or_changed")
    except (OSError, ValueError, TypeError, UnicodeError) as error:
        raise ValueError("input_integrity_changed: " + str(error)) from None
