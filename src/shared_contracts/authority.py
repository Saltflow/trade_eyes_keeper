"""Explicit deployment identity shared by otherwise independent applications."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AuthorityRef:
    root: Path
    scope: str = "public"
    isolated: bool = False

    def __post_init__(self) -> None:
        root = Path(self.root).expanduser()
        if not root.is_absolute():
            raise ValueError("authority_root_must_be_absolute")
        if (
            not self.scope
            or len(self.scope) > 128
            or any(
                character
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.:"
                for character in self.scope
            )
        ):
            raise ValueError("invalid_authority_scope")
        object.__setattr__(self, "root", root.resolve())

    @property
    def authority_id(self) -> str:
        import os

        identity = os.path.normcase(str(self.root))
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    @property
    def scope_id(self) -> str:
        return hashlib.sha256(self.scope.encode("utf-8")).hexdigest()

    @property
    def materials_root(self) -> Path:
        return self.root / "materials" / self.scope_id

    @property
    def execution_db(self) -> Path:
        return self.root / "execution.sqlite3"

    @property
    def requests_db(self) -> Path:
        return self.root / "requests.sqlite3"

    @property
    def models_db(self) -> Path:
        return self.root / "models.sqlite3"

    def to_dict(self) -> dict:
        return {
            "authority_id": self.authority_id,
            "scope": self.scope,
            "isolated": self.isolated,
            "root": str(self.root),
            "materials_root": str(self.materials_root),
            "execution_db": str(self.execution_db),
            "requests_db": str(self.requests_db),
            "models_db": str(self.models_db),
        }
