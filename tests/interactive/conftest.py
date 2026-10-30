"""Unit services mock the Node transport; dedicated Pi tests use the real bridge."""

import pytest

from src.interactive.assistant.harness import ConversationHarness


@pytest.fixture
def stub_pi_transport(monkeypatch):
    def local_loop(harness, messages, tools, run):
        return harness._native(messages, run)

    monkeypatch.setattr(ConversationHarness, "_pi", local_loop)
