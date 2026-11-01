"""Unit services mock the Node transport; dedicated Pi tests use the real bridge."""

import pytest

from src.interactive.assistant.harness import ConversationHarness


@pytest.fixture(autouse=True)
def isolated_shared_authority(tmp_path, monkeypatch):
    """HTTP fixtures must never poison another test's durable source/account state."""
    monkeypatch.setenv("TRADE_EYES_AUTHORITY_ROOT", str(tmp_path / "authority"))


@pytest.fixture
def stub_pi_transport(monkeypatch):
    def local_loop(harness, messages, tools, run):
        return harness._native(messages, run)

    monkeypatch.setattr(ConversationHarness, "_pi", local_loop)
