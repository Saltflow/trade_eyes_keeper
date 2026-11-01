"""This project's composition defaults, kept outside the portable libraries."""

from __future__ import annotations

import os
from pathlib import Path

from .shared_contracts import AuthorityRef


def authority_for(
    project_root: Path | str,
    config: dict | None = None,
    *,
    explicit_root: Path | str | None = None,
    scope: str = "public",
    isolated: bool = False,
) -> AuthorityRef:
    project = Path(project_root).resolve()
    settings = (config or {}).get("storage", {}) or {}
    value = (
        explicit_root
        or settings.get("authority_root")
        or os.getenv("TRADE_EYES_AUTHORITY_ROOT")
        or project / "data/shared_platform"
    )
    root = Path(value).expanduser()
    if not root.is_absolute():
        root = project / root
    return AuthorityRef(root.resolve(), scope=scope, isolated=isolated)
