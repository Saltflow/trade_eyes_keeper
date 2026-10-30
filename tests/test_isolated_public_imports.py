"""Import entrypoints independently so earlier imports cannot hide cycles."""

from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "module",
    [
        "src.notification.email_notifier",
        "src.markets",
        "src.search",
        "src.backtest.engine",
    ],
)
def test_public_module_import_in_fresh_process(module):
    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "-B",
            "-c",
            "import importlib, sys; importlib.import_module(sys.argv[1])",
            module,
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
