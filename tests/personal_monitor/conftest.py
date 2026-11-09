"""Holdings tests use the installed Pi bridge with isolated business state."""

import os
from pathlib import Path

import pytest

from src.interactive.assistant.harness import ConversationHarness


@pytest.fixture(autouse=True)
def installed_bridge(monkeypatch):
    original = ConversationHarness.__init__
    bridge = Path(__file__).resolve().parents[2] / "tools/feishu-pi/bridge.mjs"

    def initialize(harness, *args, **kwargs):
        original(harness, *args, **kwargs)
        harness.bridge_path = bridge
        harness.node_executable = os.environ.get("FEISHU_PI_TEST_NODE")

    monkeypatch.setattr(ConversationHarness, "__init__", initialize)
