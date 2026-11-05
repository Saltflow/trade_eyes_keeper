"""Configuration previews exercise real YAML routing and the market parser."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from src.core.config_store import ConfigStore
from src.core.schedule_manager import ScheduleManager, set_schedule_manager
from src.interactive.assistant.config_tools import (
    ConfigProposal,
    ConfigurationConflict,
    ConfigurationTools,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def config_tools(tmp_path, monkeypatch):
    directory = tmp_path / "project" / "config"
    directory.mkdir(parents=True)
    app = yaml.safe_load(
        (PROJECT_ROOT / "config/config.yaml.example").read_text(encoding="utf-8")
    )
    app["stocks"] = ["600036", "VOO"]
    app["llm"]["api_key"] = "stored-deepseek-secret"
    app["email"]["sender_password"] = "stored-email-secret"
    app["interactive"]["feishu"]["allowed_chat_ids"] = ["*"]
    app_path = directory / "config.yaml"
    app_path.write_text(yaml.safe_dump(app, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "runtime-api-secret")
    monkeypatch.setenv("EMAIL_PASSWORD", "runtime-email-secret")
    monkeypatch.setattr(
        ScheduleManager, "_job_runner", staticmethod(lambda *_args: lambda: None)
    )
    set_schedule_manager(None)
    from unittest.mock import Mock

    from src.instruments.classifier import detect_market

    resolver = Mock()
    resolver.resolve.side_effect = lambda code, market=None: {
        "code": code,
        "name": "fixture identity",
        "market": detect_market(code),
        "source": "test_quote",
    }
    yield ConfigurationTools(directory.parent, symbol_resolver=resolver)
    set_schedule_manager(None)


def _change(key, value):
    return {"key": key, "value": value}


def _app_store(tools):
    return ConfigStore(tools.project_root / "config/config.yaml")


def test_describe_only_exposes_allowlisted_values(config_tools):
    description = config_tools.describe()
    payload = json.dumps(description)
    assert "secret" not in payload
    assert "allowed_chat_ids" not in payload
    assert "api_key" not in payload
    keys = {entry["key"] for entry in description["fields"]}
    assert "stocks" in keys
    assert "skip_signals" not in keys
    assert "notification.feishu.enabled" in keys
    assert not any(
        key.startswith(
            (
                "optimizer.",
                "search.",
                "gate_profiles.",
                "walk_forward_profiles.",
                "execution_profiles.",
            )
        )
        for key in keys
    )
    assert "execution_params.commission_rate" not in keys
    assert "walk_forward.window_range_penalty" not in keys
    assert "search.solvers.local_genetic.budget" not in keys
    trigger = next(
        item
        for item in description["fields"]
        if item["key"] == "scheduler.daily_report_triggers"
    )
    assert trigger["schema"]["mode"] == ["any", "all"]
    assert set(trigger["schema"]["allowed_roots"]) == {
        "dividend_events",
        "placements",
        "announcements",
    }


def _announcement_fields(tools):
    return {
        item["key"]: item
        for item in tools.describe()["fields"]
        if item["key"].startswith("announcements.")
    }


def test_report_announcement_display_months_preview_and_apply(config_tools):
    key = "report.events.announcement_lookback_months"
    description = next(
        item for item in config_tools.describe()["fields"] if item["key"] == key
    )
    assert description["value"] == 2 and "已采集" in description["description"]
    before = _app_store(config_tools).path.read_bytes()
    (proposal,) = config_tools.propose([_change(key, 3)])
    assert _app_store(config_tools).path.read_bytes() == before
    assert "下次任务" in proposal["effect"]
    config_tools.apply(proposal)
    assert (
        _app_store(config_tools).load_raw()["report"]["events"][
            "announcement_lookback_months"
        ]
        == 3
    )
    for value in (0, 25, True, "2", 1.5):
        with pytest.raises((ValueError, TypeError)):
            config_tools.propose([_change(key, value)])
    _app_store(config_tools).update(lambda raw: raw["report"].pop("events"))
    assert (
        next(item for item in config_tools.describe()["fields"] if item["key"] == key)[
            "value"
        ]
        == 2
    )
    for value in (1, 24):
        (proposal,) = config_tools.propose([_change(key, value)])
        config_tools.apply(proposal)
        assert (
            _app_store(config_tools).load_raw()["report"]["events"][
                "announcement_lookback_months"
            ]
            == value
        )


def test_announcement_writable_fields_refresh_from_live_config(config_tools):
    values = {
        "enable": True,
        "days": 11,
        "dividend_days": 430,
        "enable_content_fetching": True,
        "enable_llm_extraction": False,
        "max_llm_calls_per_run": 3,
        "max_pdf_size_mb": 2.5,
        "api_key": "announcement-private-secret",
        "provider": {"password": "nested-private-secret"},
    }
    _app_store(config_tools).update(lambda raw: raw.update(announcements=values))
    fields = _announcement_fields(config_tools)
    assert len(fields) == 7
    for name, value in list(values.items())[:7]:
        row = fields[f"announcements.{name}"]
        assert row["value"] == value
        assert row["configured"] is True
        assert row["valid"] is True
        assert row["writable"] is True
    assert "secret" not in json.dumps(fields)
    _app_store(config_tools).update(lambda raw: raw["announcements"].update(days=17))
    assert _announcement_fields(config_tools)["announcements.days"]["value"] == 17
    writable = [
        item
        for item in config_tools.describe()["fields"]
        if not item["key"].startswith("announcements.")
    ]
    assert all(item["writable"] is True for item in writable)


def test_announcement_missing_is_not_reported_as_configured_default(config_tools):
    _app_store(config_tools).update(lambda raw: raw.pop("announcements", None))
    fields = _announcement_fields(config_tools)
    expected = {
        "enable": False,
        "days": 7,
        "dividend_days": 420,
        "enable_content_fetching": False,
        "enable_llm_extraction": False,
        "max_llm_calls_per_run": 5,
        "max_pdf_size_mb": 10,
    }
    for name, default in expected.items():
        row = fields[f"announcements.{name}"]
        assert row["configured"] is False
        assert row["value"] is None
        assert row["default"] == default
        assert row["default_source"] in {"main.py", "src/data/announcement_fetcher.py"}


@pytest.mark.parametrize(
    "value", ["hidden-private-secret", {"api_key": "private"}, ["secret"], None, True]
)
def test_announcement_invalid_integer_values_never_expose_raw_content(
    config_tools, value
):
    _app_store(config_tools).update(
        lambda raw: raw.update(announcements={"days": value})
    )
    row = _announcement_fields(config_tools)["announcements.days"]
    assert row["configured"] is True
    assert row["valid"] is False
    assert row["value"] is None
    assert row["default"] == 7
    assert "secret" not in json.dumps(row)
    before = _app_store(config_tools).path.read_bytes()
    with pytest.raises(ValueError) as exc:
        config_tools.propose([_change("announcements.days", 7)])
    assert "private" not in str(exc.value)
    assert "secret" not in str(exc.value)
    assert _app_store(config_tools).path.read_bytes() == before


@pytest.mark.parametrize(
    "name,value",
    [
        ("enable", 1),
        ("max_pdf_size_mb", float("nan")),
        ("max_pdf_size_mb", float("inf")),
        ("max_pdf_size_mb", 10**400),
    ],
)
def test_announcement_boolean_and_finite_numeric_types_are_checked(
    config_tools, name, value
):
    _app_store(config_tools).update(lambda raw: raw.update(announcements={name: value}))
    row = _announcement_fields(config_tools)[f"announcements.{name}"]
    assert row["value"] is None
    assert row["valid"] is False
    assert row["configured"] is True
    before = _app_store(config_tools).path.read_bytes()
    with pytest.raises(ValueError):
        config_tools.propose(
            [_change(f"announcements.{name}", True if name == "enable" else 10)]
        )
    assert _app_store(config_tools).path.read_bytes() == before


def test_announcement_invalid_section_does_not_expose_original_value(config_tools):
    _app_store(config_tools).update(
        lambda raw: raw.update(announcements="private-secret")
    )
    fields = _announcement_fields(config_tools)
    assert all(row["valid"] is False for row in fields.values())
    assert "private-secret" not in json.dumps(fields)
    before = _app_store(config_tools).path.read_bytes()
    with pytest.raises(ValueError) as exc:
        config_tools.propose([_change("announcements.enable", True)])
    assert "private-secret" not in str(exc.value)
    assert _app_store(config_tools).path.read_bytes() == before


@pytest.mark.parametrize(
    "name,default",
    [
        ("enable", False),
        ("days", 7),
        ("dividend_days", 420),
        ("enable_content_fetching", False),
        ("enable_llm_extraction", False),
        ("max_llm_calls_per_run", 5),
        ("max_pdf_size_mb", 10),
    ],
)
def test_missing_announcement_can_be_explicitly_set_to_its_default(
    config_tools, name, default
):
    _app_store(config_tools).update(lambda raw: raw.pop("announcements", None))
    before = _app_store(config_tools).path.read_bytes()
    (proposal,) = config_tools.propose([_change(f"announcements.{name}", default)])
    assert _app_store(config_tools).path.read_bytes() == before
    assert proposal["diff"][0]["old"] is None
    assert proposal["diff"][0]["old_configured"] is False
    assert proposal["diff"][0]["default"] == default
    assert proposal["diff"][0]["new"] == default
    assert config_tools.apply(proposal)["status"] == "applied"
    assert _app_store(config_tools).load_raw()["announcements"][name] == default


@pytest.mark.parametrize(
    "name,value",
    [
        ("days", 1),
        ("days", 3650),
        ("dividend_days", 1),
        ("dividend_days", 3650),
        ("max_llm_calls_per_run", 0),
        ("max_llm_calls_per_run", 1000),
        ("max_pdf_size_mb", 0.1),
        ("max_pdf_size_mb", 100),
    ],
)
def test_announcement_boundary_values_preview_and_apply(config_tools, name, value):
    _app_store(config_tools).update(lambda raw: raw.update(announcements={}))
    before = _app_store(config_tools).path.read_bytes()
    (proposal,) = config_tools.propose([_change(f"announcements.{name}", value)])
    assert _app_store(config_tools).path.read_bytes() == before
    assert proposal["diff"][0]["new"] == value
    config_tools.apply(proposal)
    assert _app_store(config_tools).load_raw()["announcements"][name] == value


@pytest.mark.parametrize(
    "name,value",
    [
        ("enable", 1),
        ("enable", "true"),
        ("enable_content_fetching", "false"),
        ("enable_llm_extraction", 0),
        ("days", 0),
        ("days", 3651),
        ("days", True),
        ("dividend_days", 0),
        ("dividend_days", 3651),
        ("dividend_days", 1.5),
        ("max_llm_calls_per_run", -1),
        ("max_llm_calls_per_run", 1001),
        ("max_llm_calls_per_run", False),
        ("max_pdf_size_mb", 0.09),
        ("max_pdf_size_mb", 100.01),
        ("max_pdf_size_mb", True),
        ("max_pdf_size_mb", float("nan")),
        ("max_pdf_size_mb", float("inf")),
        ("max_pdf_size_mb", 10**400),
    ],
)
def test_announcement_bad_new_value_cannot_create_or_apply_proposal(
    config_tools, name, value
):
    _app_store(config_tools).update(lambda raw: raw.update(announcements={}))
    before = _app_store(config_tools).path.read_bytes()
    with pytest.raises(ValueError):
        config_tools.propose([_change(f"announcements.{name}", value)])
    assert _app_store(config_tools).path.read_bytes() == before


@pytest.mark.parametrize(
    "name,old,new",
    [
        ("days", 5000, 30),
        ("dividend_days", -10, 420),
        ("max_llm_calls_per_run", 1001, 5),
        ("max_pdf_size_mb", 200, 10),
    ],
)
def test_announcement_valid_numeric_old_value_outside_new_range_can_be_repaired(
    config_tools, name, old, new
):
    _app_store(config_tools).update(lambda raw: raw.update(announcements={name: old}))
    (proposal,) = config_tools.propose([_change(f"announcements.{name}", new)])
    assert proposal["diff"][0]["old"] == old
    assert proposal["diff"][0]["old_configured"] is True
    config_tools.apply(proposal)
    assert _app_store(config_tools).load_raw()["announcements"][name] == new


def test_announcement_multi_field_change_is_one_atomic_proposal_preserving_secrets(
    config_tools,
):
    _app_store(config_tools).update(
        lambda raw: raw.update(
            announcements={"private_api_key": "announcement-private-secret"}
        )
    )
    before = _app_store(config_tools).load_raw()
    changes = {
        "enable": True,
        "days": 14,
        "dividend_days": 500,
        "enable_content_fetching": True,
        "enable_llm_extraction": True,
        "max_llm_calls_per_run": 6,
        "max_pdf_size_mb": 12.5,
    }
    proposals = config_tools.propose(
        [_change(f"announcements.{key}", value) for key, value in changes.items()]
    )
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal["target"] == "config/config.yaml"
    assert len(proposal["diff"]) == 7
    assert "secret" not in json.dumps(proposal)
    assert _app_store(config_tools).load_raw() == before
    expected = deepcopy(before)
    expected["announcements"].update(changes)
    result = config_tools.apply(proposal)
    assert result["status"] == "applied"
    assert "下次" in result["effect"]
    assert _app_store(config_tools).load_raw() == expected


def test_announcement_proposal_rejects_stale_preview_after_independent_update(
    config_tools,
):
    _app_store(config_tools).update(lambda raw: raw.update(announcements={"days": 7}))
    (proposal,) = config_tools.propose([_change("announcements.days", 14)])
    _app_store(config_tools).update(lambda raw: raw.update(skip_search=["600036"]))
    before = _app_store(config_tools).path.read_bytes()
    with pytest.raises(ConfigurationConflict):
        config_tools.apply(proposal)
    assert _app_store(config_tools).path.read_bytes() == before


def test_announcement_credentials_stay_outside_writable_registry(config_tools):
    for key in (
        "announcements.api_key",
        "announcements.password",
        "announcements.provider",
    ):
        with pytest.raises(ValueError, match="不可修改"):
            config_tools.propose([_change(key, "override")])


@pytest.mark.parametrize(
    "key,value",
    [
        ("llm.api_key", "override"),
        ("interactive.feishu.allowed_chat_ids", []),
        ("interactive.feishu.enabled", False),
        ("assistant.docker.network", "host"),
        ("optimizer.output_dir", "../"),
        ("optimizer.markets.a_share.search.evaluation_backend", "scalar"),
        ("execution_params.commission_rate", 0.01),
        ("walk_forward.window_range_penalty", 0.8),
        ("search.solvers.local_genetic.budget", 1000),
        ("skip_signals", ["600036"]),
        ("skip_search", ["600036"]),
        ("optimizer.markets.a_share.strategy", "percentile"),
        ("optimizer.markets.a_share.solver_id", "random"),
        ("optimizer.markets.a_share.solver_config.budget", 1000),
        ("optimizer.markets.a_share.search.workers", 2),
        ("execution_profiles.a_share_cny.commission_rate", 0.001),
        ("walk_forward_profiles.a_share_84m.num_windows", 6),
        ("gate_profiles.standard.rules.maximum_drawdown.value", -20),
    ],
)
def test_rejects_credentials_permissions_paths_and_shadow_fields(
    config_tools, key, value
):
    before = _app_store(config_tools).path.read_bytes()
    with pytest.raises(ValueError, match="不可修改"):
        config_tools.propose([_change(key, value)])
    assert _app_store(config_tools).path.read_bytes() == before


@pytest.mark.parametrize(
    "key,value",
    [
        ("stocks", [600036]),
        ("stocks", ["600036", "600036"]),
        ("stocks", ["600036;python"]),
        ("notification.feishu.enabled", "false"),
        ("scheduler.run_time", "25:00"),
        ("scheduler.run_time", "9:30"),
        ("scheduler.daily_report_frequency", "monthly"),
        (
            "scheduler.daily_report_triggers",
            {
                "mode": "any",
                "conditions": [{"path": "__class__", "operator": "exists"}],
            },
        ),
        (
            "scheduler.brief_reports.morning_snapshot.triggers",
            {
                "mode": "xor",
                "conditions": [{"path": "placements", "operator": "non_empty"}],
            },
        ),
        ("scheduler.daily_report_weekday", True),
        ("scheduler.daily_report_weekday", 7),
        ("optimizer.markets.a_share.solver_config.budget", True),
        ("optimizer.markets.a_share.solver_config.budget", 10),
        ("optimizer.markets.a_share.solver_config.budget", 100.5),
        ("optimizer.markets.a_share.solver_id", []),
        ("optimizer.markets.a_share.solver_config.gene_mutation_rate", float("nan")),
        ("optimizer.markets.a_share.solver_config.random_immigrant_rate", 1.0),
        ("execution_profiles.a_share_cny.commission_rate", float("inf")),
        ("gate_profiles.standard.rules.maximum_drawdown.value", 3),
    ],
)
def test_rejects_bad_types_and_values(config_tools, key, value):
    with pytest.raises(ValueError):
        config_tools.propose([_change(key, value)])


def test_watchlist_preview_has_old_new_and_keeps_raw_credentials(config_tools):
    app_path = _app_store(config_tools).path
    before = app_path.read_bytes()
    (proposal,) = config_tools.propose([_change("stocks", ["600036", "VOO", "00883"])])
    assert app_path.read_bytes() == before
    assert proposal["target"] == "config/config.yaml"
    assert proposal["diff"][0]["old"] == ["600036", "VOO"]
    assert proposal["diff"][0]["new"] == ["600036", "VOO", "00883"]
    assert (
        ConfigProposal.from_dict(json.loads(json.dumps(proposal))).to_dict() == proposal
    )
    result = config_tools.apply(proposal)
    assert result["status"] == "applied"
    raw = _app_store(config_tools).load_raw()
    assert raw["stocks"] == ["600036", "VOO", "00883"]
    assert raw["llm"]["api_key"] == "stored-deepseek-secret"
    assert raw["email"]["sender_password"] == "stored-email-secret"
    assert raw["interactive"]["feishu"]["allowed_chat_ids"] == ["*"]
    assert "runtime-api-secret" not in app_path.read_text(encoding="utf-8")
    assert "runtime-email-secret" not in app_path.read_text(encoding="utf-8")
    with pytest.raises(ConfigurationConflict):
        config_tools.apply(proposal)


def test_concurrent_change_invalidates_preview_instead_of_overwriting(config_tools):
    (proposal,) = config_tools.propose([_change("stocks", ["00883"])])
    _app_store(config_tools).update(lambda raw: raw.update(skip_search=["VOO"]))
    with pytest.raises(ConfigurationConflict, match="配置已变化"):
        config_tools.apply(proposal)
    raw = _app_store(config_tools).load_raw()
    assert raw["stocks"] == ["600036", "VOO"]
    assert raw["skip_search"] == ["VOO"]


def test_revision_is_checked_after_store_acquires_lock(config_tools, monkeypatch):
    (proposal,) = config_tools.propose([_change("stocks", ["00883"])])
    original = ConfigStore.update
    injected = False

    def interleaved_update(store, mutate, **kwargs):
        nonlocal injected
        if store.path == _app_store(config_tools).path and not injected:
            injected = True
            original(store, lambda raw: raw.update(skip_search=["600036"]))
        return original(store, mutate, **kwargs)

    monkeypatch.setattr(ConfigStore, "update", interleaved_update)
    with pytest.raises(ConfigurationConflict):
        config_tools.apply(proposal)
    assert _app_store(config_tools).load_raw()["skip_search"] == ["600036"]


def test_concurrent_confirmation_writes_once(config_tools):
    (proposal,) = config_tools.propose([_change("stocks", ["VOO"])])

    def apply():
        try:
            return config_tools.apply(proposal)["status"]
        except ConfigurationConflict:
            return "stale"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: apply(), range(2)))
    assert sorted(results) == ["applied", "stale"]


def test_forged_proposal_path_or_diff_is_rejected(config_tools):
    (proposal,) = config_tools.propose([_change("stocks", ["00883"])])
    forged = deepcopy(proposal)
    forged["target"] = "../../other/config.yaml"
    with pytest.raises(ValueError, match="目标"):
        config_tools.apply(forged)
    forged["target"] = "config/optimizer_constraints.yaml"
    with pytest.raises(ValueError, match="目标"):
        config_tools.apply(forged)
    forged = deepcopy(proposal)
    forged["diff"][0]["old"] = ["different"]
    with pytest.raises(ConfigurationConflict, match="影响范围"):
        config_tools.apply(forged)


def test_schedule_change_reloads_live_jobs_without_startup_execution(config_tools):
    store = _app_store(config_tools)
    manager = ScheduleManager(store.load_raw(), config_path=store.path)
    manager.start()
    try:
        (proposal,) = config_tools.propose(
            [
                _change("scheduler.run_time", "20:30"),
                _change("scheduler.brief_reports.morning_snapshot.enabled", False),
            ]
        )
        result = config_tools.apply(proposal)
        assert result["status"] == "applied"
        assert "立即生效" in result["effect"]
        daily = manager.scheduler.get_job("daily")
        assert (daily.next_run_time.hour, daily.next_run_time.minute) == (20, 30)
        assert manager.scheduler.get_job("optimize") is None
        assert manager.scheduler.get_job("brief_morning_snapshot") is None
        assert manager.scheduler.get_job("startup_task") is None
    finally:
        manager.stop()


def test_retired_optimizer_schedule_is_neither_exposed_nor_writable(config_tools):
    values = {
        "optimize_enabled": True,
        "optimize_time": "02:00",
        "optimize_misfire_grace_seconds": 7200,
    }
    store = _app_store(config_tools)
    store.update(lambda raw: raw["scheduler"].update(values))
    store.update(
        lambda raw: raw["scheduler"]["brief_reports"].append(
            {"id": "optimize", "run_time": "02:00", "enabled": True}
        )
    )
    before = store.path.read_bytes()
    keys = {item["key"] for item in config_tools.describe()["fields"]}
    assert not any(key.startswith("scheduler.brief_reports.optimize.") for key in keys)
    for key, value in values.items():
        name = f"scheduler.{key}"
        assert name not in keys
        with pytest.raises(ValueError, match="不可修改"):
            config_tools.propose([_change(name, value)])
    assert store.path.read_bytes() == before


def test_report_triggers_are_previewed_and_saved_under_confirmation(config_tools):
    rules = {
        "mode": "any",
        "conditions": [
            {"path": "dividend_events", "operator": "non_empty"},
            {"path": "placements", "operator": "non_empty"},
        ],
    }
    changes = [
        _change("scheduler.daily_report_triggers", rules),
        _change(
            "scheduler.brief_reports.morning_snapshot.triggers",
            {
                "mode": "all",
                "conditions": [
                    {
                        "path": "announcements.*.*.title",
                        "operator": "contains",
                        "value": "年报",
                    }
                ],
            },
        ),
    ]

    (proposal,) = config_tools.propose(changes)

    assert {item["key"]: item["old"] for item in proposal["diff"]} == {
        "scheduler.daily_report_triggers": {},
        "scheduler.brief_reports.morning_snapshot.triggers": {},
    }
    assert {item["key"]: item["new"] for item in proposal["diff"]} == {
        "scheduler.daily_report_triggers": rules,
        "scheduler.brief_reports.morning_snapshot.triggers": changes[1]["value"],
    }

    result = config_tools.apply(proposal)

    saved = _app_store(config_tools).load_raw()["scheduler"]
    assert result["status"] == "applied"
    assert saved["daily_report_triggers"] == rules
    assert saved["brief_reports"][0]["triggers"] == changes[1]["value"]


def test_schedule_reload_failure_reports_saved_configuration(config_tools, monkeypatch):
    store = _app_store(config_tools)
    manager = ScheduleManager(store.load_raw(), config_path=store.path)
    manager.start()
    try:

        def fail():
            raise RuntimeError("provider-secret-in-error")

        monkeypatch.setattr(manager, "reload_config", fail)
        (proposal,) = config_tools.propose([_change("scheduler.run_time", "20:30")])
        result = config_tools.apply(proposal)
        assert result["status"] == "applied_reload_failed"
        assert "重启服务" in result["effect"]
        assert "provider-secret-in-error" not in json.dumps(result)
        assert store.load_raw()["scheduler"]["run_time"] == "20:30"
        assert manager.scheduler.get_job("daily").next_run_time.hour == 19
    finally:
        manager.stop()


def test_schedule_reload_rolls_back_jobs_on_install_failure(config_tools, monkeypatch):
    store = _app_store(config_tools)
    manager = ScheduleManager(store.load_raw(), config_path=store.path)
    manager.start()
    try:
        install = manager._install_scheduled_specs
        attempts = 0

        def fail_once(specs, owned_ids):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                manager.scheduler.remove_job("daily")
                raise RuntimeError("failed scheduling")
            install(specs, owned_ids)

        monkeypatch.setattr(manager, "_install_scheduled_specs", fail_once)
        (proposal,) = config_tools.propose([_change("scheduler.run_time", "20:30")])
        result = config_tools.apply(proposal)
        assert result["status"] == "applied_reload_failed"
        assert attempts == 2
        assert manager.scheduler.get_job("daily").next_run_time.hour == 19
        assert store.load_raw()["scheduler"]["run_time"] == "20:30"
    finally:
        manager.stop()


def test_no_live_manager_reports_startup_requirement(config_tools):
    (proposal,) = config_tools.propose([_change("scheduler.run_time", "20:30")])
    result = config_tools.apply(proposal)
    assert "服务启动后生效" in result["effect"]


def test_off_report_frequency_keeps_daily_alert_job(config_tools):
    store = _app_store(config_tools)
    manager = ScheduleManager(store.load_raw(), config_path=store.path)
    manager.start()
    try:
        (proposal,) = config_tools.propose(
            [_change("scheduler.daily_report_frequency", "off")]
        )
        config_tools.apply(proposal)
        assert manager.scheduler.get_job("daily") is not None
        assert manager.config["scheduler"]["daily_report_frequency"] == "off"
    finally:
        manager.stop()


def test_schedule_collision_rejected_before_config_write(config_tools):
    store = _app_store(config_tools)
    store.update(lambda raw: raw["scheduler"]["brief_reports"][0].update(id="daily"))
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match="id 重复"):
        config_tools.propose([_change("scheduler.run_time", "20:30")])
    assert store.path.read_bytes() == before


def test_malformed_yaml_error_never_echoes_secret_line(config_tools):
    (proposal,) = config_tools.propose([_change("stocks", ["00883"])])
    store = _app_store(config_tools)
    store.path.write_text("llm: [stored-secret: ", encoding="utf-8")
    for action in (config_tools.describe, lambda: config_tools.apply(proposal)):
        with pytest.raises(ValueError, match="无法解析") as caught:
            action()
        assert "stored-secret" not in str(caught.value)
