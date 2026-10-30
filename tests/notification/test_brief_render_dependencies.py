"""A cold brief must work without initializing daily-only rendering backends."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_cold_brief_does_not_load_chart_or_pdf_dependencies(tmp_path):
    # Use a new interpreter: the rest of the suite legitimately imports plotting.
    code = """
import importlib.abc
import socket
import sys
from types import SimpleNamespace


class DailyRenderingForbidden(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {
            "src.notification.chart_generator", "src.notification.daily_pdf"
        } or fullname == "matplotlib" or fullname.startswith("matplotlib."):
            raise AssertionError("brief initialized daily dependency: " + fullname)


def no_network(*args, **kwargs):
    raise AssertionError("real transport forbidden")


sys.meta_path.insert(0, DailyRenderingForbidden())
socket.socket.connect = socket.create_connection = no_network
from src.notification.manager import NotifierManager
import pandas as pd

config = {
    "email": {
        "archive_dir": sys.argv[1],
        "sender_email": "sender@example.invalid",
        "receiver_email": "receiver@example.invalid",
        "sender_password": "test-placeholder",
    },
    "notification": {
        "email": {"enabled": True},
        "feishu": {"enabled": False},
        "telegram": {"enabled": False},
    },
}
manager = NotifierManager(config)
captured = []


def acknowledge(subject, body):
    captured.append((subject, body))
    return {"status": "sent", "reason": "local-test-acknowledgement"}


manager.email._send_email = acknowledge
session = SimpleNamespace(get_all_dataframe=lambda: pd.DataFrame())
result = manager.send_brief_report(
    session, {"id": "afternoon_snapshot", "label": "controlled brief"}
)
assert result["status"] == "succeeded", result
assert len(captured) == 1
assert captured[0][0].startswith("controlled brief - ")
assert "<table" in captured[0][1]
assert not any(name == "matplotlib" or name.startswith("matplotlib.")
               for name in sys.modules)
assert "src.notification.chart_generator" not in sys.modules
assert "src.notification.daily_pdf" not in sys.modules
print("cold brief rendered and acknowledged without daily backends")
"""
    env = {
        **os.environ,
        "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": "",
        "SKIP_NOTIFICATIONS": "0",
        "SKIP_EMAIL": "0",
    }
    completed = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
