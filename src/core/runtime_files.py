"""Atomic JSON commits shared by runtime status and report receipts."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Callable


def atomic_write_json(
    path: Path | str,
    payload: object,
    *,
    ensure_ascii: bool = False,
    indent: int | None = None,
    allow_nan: bool = True,
    replace_attempts: int = 8,
    sleep: Callable[[float], None] | None = None,
) -> None:
    """Replace one JSON file using a private temporary in the same directory.

    Only Windows access/sharing/lock errors (5/32/33) are retried. Replace and
    cleanup share at most seven sleeps, totaling 0.45 seconds with eight attempts.
    Permanent errors propagate, and cleanup touches only this call's temporary.
    """
    if type(replace_attempts) is not int or not 1 <= replace_attempts <= 8:
        raise ValueError("replace_attempts must be an integer from 1 to 8")
    contents = json.dumps(
        payload, ensure_ascii=ensure_ascii, indent=indent, allow_nan=allow_nan
    )
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    pause = time.sleep if sleep is None else sleep
    delays = iter(
        min(0.01 * 2**attempt, 0.1) for attempt in range(replace_attempts - 1)
    )

    def retry(operation: Callable[[], object]) -> None:
        while True:
            try:
                operation()
                return
            except OSError as exc:
                if getattr(exc, "winerror", None) not in {5, 32, 33}:
                    raise
                delay = next(delays, None)
                if delay is None:
                    raise
                pause(delay)

    descriptor, name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(contents)
        retry(lambda: temporary.replace(target))
    finally:
        if descriptor != -1:
            os.close(descriptor)
        retry(lambda: temporary.unlink(missing_ok=True))
