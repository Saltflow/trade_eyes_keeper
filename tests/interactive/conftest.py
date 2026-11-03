"""Isolated host services use the repository's real Pi subprocess bridge."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from src.interactive.assistant.harness import ConversationHarness


@pytest.fixture(autouse=True)
def isolated_shared_authority(tmp_path, monkeypatch):
    """HTTP fixtures must never poison another test's durable source/account state."""
    monkeypatch.setenv("TRADE_EYES_AUTHORITY_ROOT", str(tmp_path / "authority"))


@pytest.fixture
def pi_node():
    executable = os.environ.get("FEISHU_PI_TEST_NODE") or shutil.which("node")
    assert executable, "Pi tests require Node >=22.19; set FEISHU_PI_TEST_NODE"
    result = subprocess.run(
        [executable, "--version"], capture_output=True, text=True, check=True, timeout=5
    )
    version = tuple(int(part) for part in result.stdout.strip().lstrip("v").split("."))
    assert version >= (22, 19), "Set FEISHU_PI_TEST_NODE to Node >=22.19"
    return executable


@pytest.fixture
def real_pi_bridge(monkeypatch, pi_node):
    """Keep temporary business roots while loading the actual installed Pi bridge."""
    original_init = ConversationHarness.__init__
    bridge = Path(__file__).resolve().parents[2] / "tools/feishu-pi/bridge.mjs"

    def initialize(harness, *args, **kwargs):
        original_init(harness, *args, **kwargs)
        harness.bridge_path = bridge
        harness.node_executable = pi_node

    monkeypatch.setattr(ConversationHarness, "__init__", initialize)
