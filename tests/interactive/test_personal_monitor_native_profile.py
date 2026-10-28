"""Retained generic lease and explicit final-profile ownership contracts."""

from unittest.mock import Mock

import pytest

from scripts.personal_monitor_process import FileLease
from scripts.personal_monitor_shared_test import SharedPersonalTestBridge


def test_file_lease_excludes_second_holder_and_releases(tmp_path):
    path = tmp_path / "worker.lock"
    with FileLease(path), pytest.raises(RuntimeError, match="已运行"), FileLease(path):
        pytest.fail("concurrent holder entered")
    with FileLease(path):
        pass


def test_missing_host_runtime_rejects_before_transport_or_workspace(
    tmp_path, monkeypatch
):
    factory = Mock(side_effect=AssertionError("no fallback transport"))
    monkeypatch.setattr("scripts.personal_monitor_transport.make_transport", factory)
    settings = {"workspace": tmp_path / "never-created"}
    with pytest.raises(ValueError, match="公共宿主注入"):
        SharedPersonalTestBridge(settings, execution_runtime=None)
    factory.assert_not_called()
    assert not settings["workspace"].exists()


def test_retired_runtime_symbols_are_absent():
    from scripts import run_personal_monitor_feishu_group_test as group
    from scripts import run_personal_monitor_feishu_test as discovery

    assert not hasattr(discovery, "PersonalTestBridge")
    assert not hasattr(group, "run") and not hasattr(group, "_legacy_bridge")
    assert not hasattr(SharedPersonalTestBridge, "_observe_health")
    assert not hasattr(SharedPersonalTestBridge, "_send_health_notice")
