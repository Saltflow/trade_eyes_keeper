"""Management operations retain confirmation without replaying command text."""

# ruff: noqa: F811 - imported pytest fixtures
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.interactive.assistant.command_tools import configuration_hash
from src.interactive.commands import handlers
from tests.interactive.test_ref_date_confirmation_flow import (  # noqa: F401
    assistant,
    reset_project,
)


@pytest.fixture(autouse=True)
def no_external_identity(monkeypatch):
    monkeypatch.setattr(
        "src.instruments.resolver.InstrumentResolver.resolve",
        lambda self, code, market=None: {
            "code": code,
            "market": market or "a_share",
            "name": "fixture",
        },
    )


def test_old_reset_string_with_valid_plan_is_not_upgraded(assistant, reset_project):
    plan = handlers.prepare_ref_date("2026-11-09")
    item = assistant.store.create(
        "command",
        "chat",
        "alice",
        {
            "command": "/ref_date 2026-11-09",
            "reference_reset": plan,
            "config_sha256": configuration_hash(assistant.root),
            "preview": plan["preview"],
        },
    )
    assistant.store.activate(item["id"])
    assistant._control("chat", "alice", "确认", item["id"])
    result = assistant.store.get(item["id"], "chat", "alice")
    assert result["status"] == "failed"
    assert all(manager.load().holdings for manager in reset_project.managers)


@pytest.mark.parametrize(
    "command",
    [
        "/ref_date 2026-11-09",
        "/ref_position set a_share 600036 100 10",
        "/add 600036",
        "/remove 600036",
        "/save",
        "/schedule daily 20:00",
        "/report_frequency weekly",
    ],
)
def test_text_tool_rejects_retired_management_writes(assistant, command):
    with pytest.raises(ValueError):
        assistant.call_tool("chat", "alice", "bot_command", {"command": command})
    assert assistant.store.recent("chat", "alice") == []


def test_reference_reset_has_no_text_parse_or_command_executor(
    assistant, reset_project, monkeypatch
):
    parser = Mock(
        side_effect=AssertionError("structured operation parsed command text")
    )
    execute = Mock(
        side_effect=AssertionError("structured operation used command executor")
    )
    monkeypatch.setattr("src.interactive.command_parser.parse_command", parser)
    assistant.command_executor.execute = execute
    action = assistant.call_tool(
        "chat",
        "alice",
        "reference_portfolio",
        {
            "action": "reset",
            "date": "2026-11-09",
        },
    )["proposal_ids"][0]
    pending = assistant.store.get(action, "chat", "alice")
    assert pending["kind"] == "reference"
    assert "command" not in pending["payload"]
    assert all(manager.load().holdings for manager in reset_project.managers)
    assistant._control("chat", "alice", "确认", action)
    assert assistant.store.get(action, "chat", "alice")["status"] == "succeeded"
    assert all(not manager.load().holdings for manager in reset_project.managers)
    parser.assert_not_called()
    execute.assert_not_called()


def test_structured_and_slash_position_share_real_portfolio_business(
    assistant, reset_project, monkeypatch
):
    from src.core.ref_portfolio import apply_reference_reset, prepare_reference_reset

    path = reset_project.path
    apply_reference_reset(path, prepare_reference_reset(path, "2026-11-09"))
    monkeypatch.setattr(
        "src.search.config.get_market_optimizer_config",
        lambda *args, **kwargs: SimpleNamespace(
            execution=SimpleNamespace(
                commission_rate=0.0,
                lot_sizes={"a_share": 100},
                fx_rates={"a_share": 1.0},
            ),
        ),
    )
    action = assistant.call_tool(
        "chat",
        "alice",
        "reference_portfolio",
        {
            "action": "buy",
            "group": "a_share",
            "code": "600036",
            "shares": 100,
            "price": 10,
        },
    )["proposal_ids"][0]
    manager = reset_project.managers[0]
    assert not manager.load().holdings
    with pytest.raises(ValueError):
        assistant._control("chat", "mallory", "确认", action)
    assistant._control("chat", "alice", "确认", action)
    assert manager.load().holdings["600036"].shares == 100
    assert manager.load().cash == 9000
    assert handlers.handle_ref_position("set", "a_share", "600036", 200, 10).startswith(
        "✅"
    )
    assert manager.load().holdings["600036"].shares == 200
    assert manager.load().cash == 8000
    with pytest.raises(ValueError):
        assistant._control("chat", "alice", "确认", action)
    assert manager.load().holdings["600036"].shares == 200


def test_configuration_save_retains_confirmation_and_bypasses_command_text(
    assistant, monkeypatch
):
    save = Mock(return_value="✅ saved")
    monkeypatch.setattr("src.core.config_store.save_configuration", save)
    monkeypatch.setattr(
        "src.interactive.command_parser.parse_command",
        Mock(side_effect=AssertionError("unexpected parser")),
    )
    result = assistant.call_tool("chat", "alice", "configuration_save", {})
    action = result["proposal_ids"][0]
    save.assert_not_called()
    with pytest.raises(ValueError):
        assistant._control("other", "alice", "确认", action)
    assistant._control("chat", "alice", "确认", action)
    save.assert_called_once_with(assistant.root / "config/config.yaml")
    assert assistant.store.get(action, "chat", "alice")["status"] == "succeeded"
    with pytest.raises(ValueError):
        assistant._control("chat", "alice", "确认", action)
    save.assert_called_once()


@pytest.mark.parametrize(
    "command",
    [
        "/save",
        "/ref_position buy a_share 600036 100 10",
        "/schedule daily 20:00",
        "/report_frequency weekly",
    ],
)
def test_legacy_string_proposals_never_execute_management(assistant, command):
    assistant.command_executor.execute = Mock()
    item = assistant.store.create(
        "command",
        "chat",
        "alice",
        {
            "command": command,
            "config_sha256": configuration_hash(assistant.root),
            "preview": command,
        },
    )
    assistant.store.activate(item["id"])
    assistant._control("chat", "alice", "确认", item["id"])
    assert assistant.store.get(item["id"], "chat", "alice")["status"] == "failed"
    assistant.command_executor.execute.assert_not_called()


def test_fixed_schedule_writes_are_rejected_before_manager(monkeypatch):
    manager = Mock(side_effect=AssertionError("retired write touched schedule manager"))
    monkeypatch.setattr("src.core.schedule_manager.get_schedule_manager", manager)
    assert "trigger_manage" in handlers.handle_schedule("set", "daily", "20:00")
    assert "trigger_manage" in handlers.handle_daily_report_frequency("weekly")
    manager.assert_not_called()


def test_reference_root_is_explicit_not_current_directory(
    reset_project, monkeypatch, tmp_path
):
    from src.core.ref_portfolio import prepare_reference_reset

    load = Mock(return_value=reset_project.active)
    monkeypatch.setattr("src.search.artifacts.load_latest_strategy_run", load)
    away = tmp_path / "unrelated"
    away.mkdir()
    monkeypatch.chdir(away)
    plan = prepare_reference_reset(reset_project.path, "2026-11-09")
    load.assert_called_once_with(
        root=reset_project.path.parent.parent / "data/optimizer"
    )
    assert all(
        str(reset_project.path.parent.parent / "data") in target["path"]
        for target in plan["targets"]
    )


def test_structured_reset_keeps_g02_date_plan_equality(assistant, reset_project):
    from src.interactive.assistant.command_tools import prepare_reference

    prepared = prepare_reference(
        assistant.root, {"action": "reset", "date": "2026-11-09"}
    )
    prepared["payload"]["date"] = "2026-11-08"
    item = assistant.store.create("reference", "chat", "alice", prepared["payload"])
    assistant.store.activate(item["id"])
    assistant._control("chat", "alice", "确认", item["id"])
    result = assistant.store.get(item["id"], "chat", "alice")
    assert result["status"] == "failed"
    assert "重置日期与预览不一致" in result["note"]
    assert all(manager.load().holdings for manager in reset_project.managers)


def test_changed_configuration_blocks_git_save(assistant, monkeypatch):
    from src.core.config_store import ConfigStore

    save = Mock(return_value="✅ saved")
    monkeypatch.setattr("src.core.config_store.save_configuration", save)
    action = assistant.call_tool("chat", "alice", "configuration_save", {})[
        "proposal_ids"
    ][0]
    ConfigStore(assistant.root / "config/config.yaml").update(
        lambda config: config.update(stocks=["600036"])
    )
    assistant._control("chat", "alice", "确认", action)
    assert assistant.store.get(action, "chat", "alice")["status"] == "failed"
    save.assert_not_called()


def test_new_management_tools_preserve_existing_profile_permissions(assistant):
    from src.interactive.assistant.project_tools import tool_allowed, tool_group

    names = {item["function"]["name"] for item in assistant.tool_schema()}
    for name in ("watchlist_manage", "reference_portfolio", "configuration_save"):
        assert name in names
        assert tool_allowed(name, {"research"})
        assert not tool_allowed(name, {"repository"})
        assert not tool_allowed(name, {"personal"})
    assert tool_group("reference_portfolio") == "持仓与账户"
    assert tool_group("configuration_save") == "项目管理"


def test_slash_save_uses_same_configuration_business(reset_project, monkeypatch):
    from src.core.config_store import save_configuration

    calls = Mock()
    monkeypatch.setattr("subprocess.run", calls)
    assert save_configuration(reset_project.path).startswith("✅")
    calls.assert_called_once()
    assert calls.call_args.args[0][:3] == ["git", "commit", "--only"]
    assert calls.call_args.args[0][-2:] == ["--", "config/config.yaml"]
    assert calls.call_args.kwargs["cwd"] == reset_project.path.parent.parent
    shared = Mock(return_value="✅ shared")
    monkeypatch.setattr("src.core.config_store.save_configuration", shared)
    assert handlers.handle_save() == "✅ shared"
    shared.assert_called_once_with(reset_project.path)
