"""Read runtime settings and atomically update raw YAML configuration.

Environment overrides are applied only to copies returned for runtime use. All
writes reload the file while holding its process lock and mutate that raw copy;
there is deliberately no API for saving an arbitrary runtime configuration.
"""

from __future__ import annotations

import os
import stat
import tempfile
import time
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Callable, Iterator, Mapping

import yaml
from dotenv import load_dotenv

from .process_lock import exclusive_process_lock

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"

_ENV_OVERRIDES = {
    "EMAIL_SENDER": ("email", "sender_email"),
    "EMAIL_PASSWORD": ("email", "sender_password"),
    "EMAIL_RECEIVER": ("email", "receiver_email"),
    "DEEPSEEK_API_KEY": ("llm", "api_key"),
}


def runtime_config(raw: dict, environ: Mapping[str, str] | None = None) -> dict:
    """Return an independent runtime view with the established env overrides."""
    config = deepcopy(raw)
    environment = os.environ if environ is None else environ
    for name, (section, key) in _ENV_OVERRIDES.items():
        value = environment.get(name)
        if name == "DEEPSEEK_API_KEY" and value is not None:
            value = value.strip()
        if value:
            config.setdefault(section, {})[key] = value
    return config


class ConfigStore:
    """A field-update store shared by scheduler and Bot commands."""

    def __init__(self, path: Path | str = DEFAULT_CONFIG_PATH):
        self.path = Path(path).resolve()
        # Lock a separate, stable inode: replacing the YAML cannot drop the lock.
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")

    def load_raw(self) -> dict:
        """Read raw YAML; missing, empty, or malformed files fail closed."""
        with self.path.open("r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle)
        if not isinstance(config, dict):
            raise TypeError(f"configuration root must be a mapping: {self.path}")
        return config

    def load_runtime(self) -> dict:
        """Read YAML with environment values without changing its disk copy."""
        load_dotenv(self.path.parent / ".env", override=False)
        return runtime_config(self.load_raw())

    @contextmanager
    def _locked(self, timeout: float) -> Iterator[None]:
        deadline = time.monotonic() + timeout
        while True:
            with exclusive_process_lock(self.lock_path) as acquired:
                if acquired:
                    yield
                    return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"configuration is busy: {self.path}")
            time.sleep(min(0.05, remaining))

    def update(
        self,
        mutate: Callable[[dict], None],
        *,
        validate: Callable[[Path], None] | None = None,
        lock_timeout: float = 5.0,
    ) -> dict:
        """Mutate current raw YAML under lock, validate, then atomically save.

        ``mutate`` must change only fields owned by the operation. Exceptions
        from the callback, validation, or writing leave the prior YAML intact.
        Validation receives the temporary file so existing typed YAML loaders
        can validate exactly what will be persisted.
        """
        with self._locked(lock_timeout):
            config = self.load_raw()
            before = deepcopy(config)
            mutate(config)
            if config == before:
                return config
            mode = stat.S_IMODE(self.path.stat().st_mode)
            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    newline="\n",
                    prefix=f".{self.path.name}.",
                    suffix=".tmp",
                    dir=self.path.parent,
                    delete=False,
                ) as output:
                    temporary_path = Path(output.name)
                    yaml.safe_dump(
                        config,
                        output,
                        allow_unicode=True,
                        default_flow_style=False,
                        sort_keys=False,
                    )
                    output.flush()
                    os.fsync(output.fileno())
                os.chmod(temporary_path, mode)
                if validate is not None:
                    validate(temporary_path)
                os.replace(temporary_path, self.path)
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
            return config


def load_config(config_path: Path | str | None = None) -> dict:
    """Load the central runtime configuration for command-line entry points."""
    path = DEFAULT_CONFIG_PATH if config_path is None else config_path
    return ConfigStore(path).load_runtime()
