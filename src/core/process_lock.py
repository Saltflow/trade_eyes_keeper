"""Cross-platform non-blocking process locks for long-running jobs."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import os
from pathlib import Path
from typing import Iterator, TextIO


def _try_lock(handle: TextIO) -> bool:
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(" ")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        return False
    return True


def _unlock(handle: TextIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def exclusive_process_lock(path: Path | str) -> Iterator[bool]:
    """Yield whether this process acquired the named lock without waiting."""

    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    acquired = _try_lock(handle)
    try:
        if acquired:
            handle.seek(0)
            handle.truncate()
            handle.write(
                f"pid={os.getpid()} started_at={datetime.now().isoformat()}\n"
            )
            handle.flush()
        yield acquired
    finally:
        if acquired:
            _unlock(handle)
        handle.close()
