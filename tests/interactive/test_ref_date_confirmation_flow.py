"""Reference resets reuse the real proposal lifecycle; all files are isolated."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from src.core.ref_portfolio import Holding, RefPortfolio, RefPortfolioManager
from src.interactive.assistant.service import FeishuAssistant
from src.interactive.command_dispatcher import CommandExecutor, dispatch_command
from src.interactive.command_parser import parse_command
from src.interactive.commands import handlers


@pytest.fixture
def reset_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "config/config.yaml"
    path.parent.mkdir()
    path.write_text(
        yaml.safe_dump(
            {
                "storage": {"data_dir": str(tmp_path / "data")},
                "optimizer": {"_ref_date_pending": "2026-11-01"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(handlers, "CONFIG_PATH", path)
    params = SimpleNamespace(values={"threshold": 1}, execution_snapshot={})
    active = SimpleNamespace(
        timestamp="2026-11-01T19:00:00",
        params_by_group={g: params for g in ("a_share", "hk", "us")},
        strategy_for=lambda group: SimpleNamespace(name="fixture"),
        run_id_for=lambda group: f"run-{group}",
    )
    monkeypatch.setattr("src.search.artifacts.load_latest_strategy_run", lambda: active)
    monkeypatch.setattr(
        "src.search.config.get_market_optimizer_configs",
        lambda cfg: {
            g: SimpleNamespace(execution=SimpleNamespace(initial_capital=10000))
            for g in ("a_share", "hk", "us")
        },
    )
    monkeypatch.setattr(
        "src.core.ref_portfolio.reference_execution_contract",
        lambda snapshot, execution, group: {"market": group},
    )
    managers = [
        RefPortfolioManager(f"data/ref_portfolio_{g}.yaml") for g in ("a", "hk", "us")
    ]
    for manager in managers:
        portfolio = RefPortfolio(inception_date="2026-01-01", cash=5000)
        portfolio.holdings["TEST"] = Holding("TEST", 100, 10)
        manager.save(portfolio)
    return SimpleNamespace(path=path, managers=managers, active=active)


@pytest.fixture
def assistant(reset_project):
    service = FeishuAssistant(
        handlers._load_config(),
        Mock(return_value=(True, "fixture")),
        project_root=reset_project.path.parent.parent,
        research=Mock(),
        client=Mock(),
        command_executor=CommandExecutor(Mock()),
        start_workers=False,
    )
    yield service
    service.stop()


def propose(assistant, date="2026-11-09"):
    return assistant.call_tool(
        "chat",
        "alice",
        "bot_command",
        {
            "command": f"/ref_date {date}",
        },
    )["proposal_ids"][0]


def confirm(assistant, action):
    assistant._control("chat", "alice", "确认", action)
    return assistant.store.get(action, "chat", "alice")


@pytest.mark.parametrize("argument", ["2026-11-09", "confirm", "confirm old-id"])
def test_old_direct_entry_never_resets_even_with_global_pending(
    reset_project, argument
):
    assert dispatch_command(parse_command(f"/ref_date {argument}")).startswith("❌")
    assert all(m.load().holdings for m in reset_project.managers)


def test_query_does_not_create_a_proposal(assistant):
    result = assistant.call_tool(
        "chat", "alice", "bot_command", {"command": "/ref_date"}
    )
    assert "参考持仓基期" in result["response"]
    assert assistant.store.recent("chat", "alice") == []


def test_new_date_is_only_a_proposal_and_exact_confirmation_runs_once(
    assistant, reset_project
):
    a = propose(assistant, "2026-11-08")
    b = propose(assistant, "2026-11-09")
    assert a != b
    assert all(m.load().holdings for m in reset_project.managers)
    assert confirm(assistant, b)["status"] == "succeeded"
    for manager in reset_project.managers:
        pf = manager.load()
        assert not pf.holdings and pf.inception_date == "2026-11-09"
        assert pf.strategy_run_id == f"run-{pf.market_group}"
        assert pf.params_hash and pf.execution_hash
    with pytest.raises(ValueError):
        confirm(assistant, b)
    assert confirm(assistant, a)["status"] == "failed"


@pytest.mark.parametrize("chat,sender", [("other", "alice"), ("chat", "bob")])
def test_shared_proposal_owner_checks_cover_reset(
    assistant, reset_project, chat, sender
):
    action = propose(assistant)
    with pytest.raises(ValueError):
        assistant._control(chat, sender, "确认", action)
    assert all(m.load().holdings for m in reset_project.managers)
    assert confirm(assistant, action)["status"] == "succeeded"


@pytest.mark.parametrize("failure", ["expired", "preview_failed", "cancelled"])
def test_existing_proposal_lifecycle_blocks_reset(assistant, reset_project, failure):
    if failure == "preview_failed":
        assistant.send_message.return_value = (False, "offline")
    action = propose(assistant)
    if failure == "expired":
        with assistant.store.connection() as db:
            db.execute("UPDATE actions SET expires=0 WHERE id=?", (action,))
    if failure == "cancelled":
        assistant._control("chat", "alice", "取消", action)
    with pytest.raises(ValueError):
        confirm(assistant, action)
    assert all(m.load().holdings for m in reset_project.managers)


@pytest.mark.parametrize("changed", ["config", "strategy", "holdings"])
def test_changed_business_preview_is_not_applied(assistant, reset_project, changed):
    action = propose(assistant)
    if changed == "config":
        handlers._update_config(lambda config: config.update(stocks=["NEW"]))
    elif changed == "strategy":
        reset_project.active.params_by_group["a_share"].values["threshold"] = 2
    else:
        manager = reset_project.managers[0]
        pf = manager.load()
        pf.cash += 100
        manager.save(pf)
    assert confirm(assistant, action)["status"] == "failed"
    assert all(m.load().holdings for m in reset_project.managers)


def test_old_generic_proposal_without_business_preview_is_rejected(
    assistant, reset_project
):
    from src.interactive.assistant.command_tools import configuration_hash

    item = assistant.store.create(
        "command",
        "chat",
        "alice",
        {
            "command": "/ref_date 2026-11-09",
            "config_sha256": configuration_hash(assistant.root),
            "preview": {"command": "/ref_date 2026-11-09"},
        },
    )
    assistant.store.activate(item["id"])
    assert confirm(assistant, item["id"])["status"] == "failed"
    assert all(m.load().holdings for m in reset_project.managers)


def test_payload_cannot_choose_an_arbitrary_output_path(reset_project):
    plan = deepcopy(handlers.prepare_ref_date("2026-11-09"))
    plan["targets"][0]["path"] = str(reset_project.path)
    with pytest.raises(ValueError, match="重新预览"):
        handlers.apply_ref_date(plan)
    assert all(m.load().holdings for m in reset_project.managers)


def test_save_failure_is_failed_and_cannot_replay(
    assistant, reset_project, monkeypatch
):
    action = propose(assistant)
    monkeypatch.setattr(RefPortfolioManager, "save", lambda *_: None)
    item = confirm(assistant, action)
    assert item["status"] == "failed"
    assert "可能部分完成" in item["result"]["response"]
    with pytest.raises(ValueError):
        confirm(assistant, action)
    assert all(m.load().holdings for m in reset_project.managers)


@pytest.mark.parametrize("after_write", [False, True])
def test_claimed_reset_crash_uses_existing_recovery_without_replay(
    assistant, reset_project, monkeypatch, after_write
):
    class Crash(BaseException):
        pass

    action = propose(assistant)
    reset = RefPortfolioManager.reset
    calls = []

    def crash(manager, **kwargs):
        calls.append(kwargs["market_group"])
        if after_write:
            reset(manager, **kwargs)
        raise Crash()

    monkeypatch.setattr(RefPortfolioManager, "reset", crash)
    with pytest.raises(Crash):
        confirm(assistant, action)
    assert assistant.store.get(action, "chat", "alice")["status"] == "applying"
    assistant.store.recover()
    assert assistant.store.get(action, "chat", "alice")["status"] == "interrupted"
    with pytest.raises(ValueError):
        confirm(assistant, action)
    assert calls == ["a_share"]
