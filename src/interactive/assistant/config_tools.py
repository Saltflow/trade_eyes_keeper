"""Allowlisted, versioned configuration proposals for the Feishu assistant.

The model may inspect these fields and prepare proposals. The conversation
service owns user confirmation; this module never infers authorization from a
model response. Every write reloads raw YAML and checks its revision under the
same ConfigStore lock that protects the atomic replacement.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import tempfile
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from ...core.config_store import ConfigStore
from ...core.schedule_manager import ScheduleManager, get_schedule_manager
from ...notification.settings import env_flag

_APP = "config/config.yaml"
_CONSTRAINTS = "config/optimizer_constraints.yaml"
_MARKETS = ("a_share", "hk", "us")
_PROFILES = {
    "gate_profile": "gate_profiles",
    "walk_forward_profile": "walk_forward_profiles",
    "execution_profile": "execution_profiles",
    "benchmark_profile": "benchmark_profiles",
}
_NEXT_TASK = "下次任务生效；已开始的任务继续使用原配置快照"
_SCHEDULE_EFFECT = "确认保存后刷新运行中的调度器；未运行时在服务启动后生效"
_SYMBOL = re.compile(r"[A-Za-z0-9^][A-Za-z0-9.^=_-]{0,31}\Z")
_TIME = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d\Z")


class ConfigurationConflict(ValueError):
    """The preview no longer describes the configuration being written."""


@dataclass(frozen=True)
class ConfigProposal:
    """Serializable preview; all paths are relative to the project root."""

    target: str
    revision: str
    changes: list[dict[str, Any]]
    diff: list[dict[str, Any]]
    effect: str
    affected_markets: list[str]
    dependencies: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ConfigProposal:
        if not isinstance(value, dict):
            raise TypeError("配置提案必须是对象")
        required = {
            "target",
            "revision",
            "changes",
            "diff",
            "effect",
            "affected_markets",
        }
        if not required <= value.keys() or set(value) - required - {"dependencies"}:
            raise ValueError("配置提案字段不完整或包含未知字段")
        if value["target"] not in {_APP, _CONSTRAINTS}:
            raise ValueError("配置提案目标不在可写范围")
        if not isinstance(value["revision"], str) or not re.fullmatch(
            r"[a-f0-9]{64}", value["revision"]
        ):
            raise ValueError("配置提案版本无效")
        if not isinstance(value["diff"], list) or not isinstance(value["effect"], str):
            raise TypeError("配置提案预览无效")
        markets = value["affected_markets"]
        if not isinstance(markets, list) or any(m not in _MARKETS for m in markets):
            raise ValueError("配置提案市场范围无效")
        dependencies = value.get("dependencies", {})
        if not isinstance(dependencies, dict) or any(
            key not in {_APP, _CONSTRAINTS}
            or not isinstance(revision, str)
            or not re.fullmatch(r"[a-f0-9]{64}", revision)
            for key, revision in dependencies.items()
        ):
            raise ValueError("配置提案依赖版本无效")
        return cls(**deepcopy(value))


@dataclass(frozen=True)
class _Rule:
    kind: str
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()

    def validate(self, value: Any) -> None:
        if self.kind == "boolean":
            valid = type(value) is bool
        elif self.kind == "integer":
            valid = type(value) is int
        elif self.kind == "nullable_integer":
            valid = value is None or type(value) is int
        elif self.kind == "number":
            valid = type(value) in {int, float} and math.isfinite(value)
        elif self.kind == "time":
            valid = isinstance(value, str) and bool(_TIME.fullmatch(value))
        elif self.kind == "enum":
            valid = isinstance(value, str) and value in self.choices
        elif self.kind == "symbols":
            valid = (
                isinstance(value, list)
                and len(value) <= 5000
                and all(
                    isinstance(item, str) and _SYMBOL.fullmatch(item) for item in value
                )
                and len(set(value)) == len(value)
            )
        elif self.kind == "report_triggers":
            from ...notification.report_triggers import validate_report_trigger

            validate_report_trigger(value)
            valid = True
        elif self.kind == "positive_numbers":
            valid = (
                isinstance(value, list)
                and 0 < len(value) <= 500
                and all(
                    type(item) in {int, float} and math.isfinite(item) and item > 0
                    for item in value
                )
            )
        else:
            valid = False
        if not valid:
            suffix = f"，可选值：{', '.join(self.choices)}" if self.choices else ""
            raise ValueError(f"要求 {self.kind}{suffix}")
        if value is not None and self.kind in {"number", "integer", "nullable_integer"}:
            if self.minimum is not None and value < self.minimum:
                raise ValueError(f"不得小于 {self.minimum}")
            if self.maximum is not None and value > self.maximum:
                raise ValueError(f"不得大于 {self.maximum}")

    def describe(self) -> dict[str, Any]:
        result: dict[str, Any] = {"type": self.kind}
        if self.minimum is not None:
            result["minimum"] = self.minimum
        if self.maximum is not None:
            result["maximum"] = self.maximum
        if self.choices:
            result["choices"] = list(self.choices)
        if self.kind == "report_triggers":
            result["schema"] = {
                "mode": ["any", "all"],
                "condition_keys": ["path", "operator", "value"],
                "path_syntax": "dot-separated fields with optional * wildcard segments",
                "operators": [
                    "exists", "non_empty", "contains", "equals",
                    "gt", "gte", "lt", "lte",
                ],
                "max_conditions": 20,
                "allowed_roots": [
                    "dividend_events", "placements", "announcements",
                    "signal_scan", "alerts",
                ],
            }
        return result


_ANNOUNCEMENT_FIELDS = (
    ("enable", _Rule("boolean"), False, "日报是否启用公告获取", "main.py"),
    ("days", _Rule("integer", 1, 3650), 7, "一般重要公告回看天数", "main.py"),
    (
        "dividend_days",
        _Rule("integer", 1, 3650),
        420,
        "分红公告扩展回看天数；窗口不保证单页数据源覆盖全部日期",
        "main.py",
    ),
    (
        "enable_content_fetching",
        _Rule("boolean"),
        False,
        "是否抓取公告正文",
        "src/data/announcement_fetcher.py",
    ),
    (
        "enable_llm_extraction",
        _Rule("boolean"),
        False,
        "是否启用公告LLM结构化提取",
        "src/data/announcement_fetcher.py",
    ),
    (
        "max_llm_calls_per_run",
        _Rule("integer", 0, 1000),
        5,
        "每次公告丰富调用的成功LLM提取数上限，非全报告请求总上限",
        "src/data/announcement_fetcher.py",
    ),
    (
        "max_pdf_size_mb",
        _Rule("number", 0.1, 100),
        10,
        "公告PDF体积限制配置（MiB）；实际下载限制还取决于内容抓取器",
        "src/data/announcement_fetcher.py",
    ),
)


def _announcement_values(app: dict) -> dict[str, dict]:
    section = app.get("announcements", {})
    section_valid = isinstance(section, dict)
    if not section_valid:
        section = {}
    result = {}
    for name, rule, default, _description, source in _ANNOUNCEMENT_FIELDS:
        value = section.get(name)
        configured, valid = name in section, section_valid
        if configured:
            try:
                # Old numeric values outside new bounds may be repaired.
                _Rule(rule.kind).validate(value)
            except (ValueError, OverflowError):
                value, valid = None, False
        result[f"announcements.{name}"] = {
            "value": value,
            "configured": configured,
            "valid": valid,
            "default": default,
            "default_source": source,
            **({"warning": "配置类型异常；原值未展示。"} if not valid else {}),
        }
    return result


@dataclass(frozen=True)
class _Field:
    key: str
    target: str
    path: tuple[str | int, ...]
    value: Any
    rule: _Rule
    description: str
    effect: str
    markets: tuple[str, ...]


_SOLVER_RULES = {
    "budget": _Rule("integer", 100, 1_000_000),
    "random_seed": _Rule("nullable_integer", 0, 2**32 - 1),
    "phase1_random_samples": _Rule("integer", 1, 1_000_000),
    "phase1_top_keep": _Rule("integer", 1, 1_000_000),
    "num_generations": _Rule("integer", 0, 10_000),
    "population_size": _Rule("integer", 2, 1_000_000),
    "offspring_size": _Rule("integer", 1, 1_000_000),
    "crossover_rate": _Rule("number", 0, 1),
    "mutation_rate": _Rule("number", 0, 1),
    "gene_mutation_rate": _Rule("number", 0, 1),
    "max_local_step": _Rule("integer", 1, 1000),
    "step_schedule": _Rule("enum", choices=("linear_to_one",)),
    "random_immigrant_rate": _Rule("number", 0, 0.999999),
    "duplicate_retry_limit": _Rule("integer", 1, 100_000),
    "initialization_samples": _Rule("integer", 1, 1_000_000),
}
_COMMON_SOLVER = {"budget", "random_seed"}
_GENETIC_SOLVER = _COMMON_SOLVER | {
    "phase1_random_samples",
    "phase1_top_keep",
    "num_generations",
    "population_size",
    "offspring_size",
    "crossover_rate",
    "gene_mutation_rate",
}
_SOLVER_FIELDS = {
    "random": _COMMON_SOLVER,
    "simulated_annealing": _COMMON_SOLVER | {"initialization_samples"},
    "genetic": _GENETIC_SOLVER | {"mutation_rate"},
    "local_genetic": _GENETIC_SOLVER
    | {
        "max_local_step",
        "step_schedule",
        "random_immigrant_rate",
        "duplicate_retry_limit",
    },
}
_SEARCH_RULES = {
    "workers": _Rule("nullable_integer", 1, 128),
    "batch_size": _Rule("integer", 128, 512),
    "candidate_retention_ratio": _Rule("number", 0.001, 1),
    "run_retention_count": _Rule("integer", 1, 100),
}
_EXECUTION_RULES = {
    "initial_capital": _Rule("number", 10_000, 10_000_000),
    "commission_rate": _Rule("number", 0, 0.02),
    "min_holding_days": _Rule("integer", 0, 3650),
}
_WALK_FORWARD_RULES = {
    "state_lookback_months": _Rule("integer", 1, 120),
    "test_months": _Rule("integer", 24, 120),
    "step_months": _Rule("integer", 1, 120),
    "num_windows": _Rule("integer", 2, 100),
    "data_years": _Rule("number", 1, 100),
    "window_weights": _Rule("positive_numbers"),
    "window_range_penalty": _Rule("number", 0, 10),
}


def _revision(raw: dict) -> str:
    # Hash raw values, including uneditable fields, but never disclose them.
    serialized = yaml.safe_dump(raw, allow_unicode=True, sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _get(raw: dict, path: tuple[str | int, ...], default: Any = None) -> Any:
    value: Any = raw
    for part in path:
        if (isinstance(value, dict) and part in value) or (
            isinstance(value, list) and isinstance(part, int) and 0 <= part < len(value)
        ):
            value = value[part]
        else:
            return default
    return value


def _set(raw: dict, path: tuple[str | int, ...], value: Any) -> None:
    current: Any = raw
    for part in path[:-1]:
        if isinstance(current, dict):
            current = current.setdefault(part, {})
        else:
            current = current[part]
        if not isinstance(current, (dict, list)):
            raise TypeError("配置路径的父级不是对象")
    current[path[-1]] = deepcopy(value)


def _check_changes(changes: Any) -> list[dict[str, Any]]:
    if not isinstance(changes, list) or not 1 <= len(changes) <= 100:
        raise ValueError("changes 必须包含 1 到 100 项配置变更")
    keys: set[str] = set()
    for change in changes:
        if not isinstance(change, dict) or set(change) != {"key", "value"}:
            raise ValueError("每项配置变更只接受 key 和 value")
        key = change["key"]
        if not isinstance(key, str) or len(key) > 240 or key in keys:
            raise ValueError("配置 key 无效或重复")
        keys.add(key)
    return deepcopy(changes)


def _minimum_evaluation_months(raw: dict) -> int:
    """Preserve the research policy even when the installed core predates it."""
    defaults = {
        "contract": "minimum-two-year-evaluation/1",
        "minimum_evaluation_months": 24,
        "daily_portfolio_history_months": 36,
        "default_backtest_months": 36,
        "default_research_horizon_profile": "existing_data_66m_24m",
    }
    if "evaluation_horizon_policy" not in raw:
        return 24
    declared = raw["evaluation_horizon_policy"]
    if not isinstance(declared, dict) or set(declared) - set(defaults):
        raise ValueError("invalid evaluation_horizon_policy declaration")
    policy = {**defaults, **declared}
    for name in ("contract", "default_research_horizon_profile"):
        if not isinstance(policy[name], str) or not policy[name].strip():
            raise ValueError(f"evaluation_horizon_policy.{name} is invalid")
    minimum = policy["minimum_evaluation_months"]
    if type(minimum) is not int or minimum < 24:
        raise ValueError(
            "evaluation_horizon_policy.minimum_evaluation_months must be "
            "an integer >= 24"
        )
    for name in ("daily_portfolio_history_months", "default_backtest_months"):
        if type(policy[name]) is not int or policy[name] < minimum:
            raise ValueError(f"evaluation_horizon_policy.{name} must be >= {minimum}")
    return minimum


def _validate_market_horizons(
    app: dict, raw: dict, before_app: dict, before_raw: dict
) -> None:
    """Check changed selections without silently migrating deployed profiles.

    The core parser remains authoritative for strategy, Solver and execution
    settings. Older versions did not reject short tests or a purge that leaves
    no independent ranking window, so the assistant enforces those boundaries
    when a selected Walk-Forward profile changes. Unchanged legacy profiles
    remain usable for independent budget/Solver updates. The same comparison
    runs inside the confirmed ConfigStore transaction.
    """
    markets = _get(app, ("optimizer", "markets"))
    profiles = raw.get("walk_forward_profiles")
    if not isinstance(markets, dict) or not markets or not isinstance(profiles, dict):
        raise ValueError("optimizer markets and Walk-Forward profiles are required")
    changed = []
    for group, market in markets.items():
        profile_id = (
            market.get("walk_forward_profile") if isinstance(market, dict) else None
        )
        profile = profiles.get(profile_id) if isinstance(profile_id, str) else None
        previous_id = _get(
            before_app, ("optimizer", "markets", group, "walk_forward_profile")
        )
        previous = _get(before_raw, ("walk_forward_profiles", previous_id))
        if profile_id != previous_id or profile != previous:
            changed.append((group, profile_id, profile))
    if not changed:
        return
    minimum = _minimum_evaluation_months(raw)
    for group, profile_id, profile in changed:
        prefix = f"{group}: invalid Walk-Forward profile {profile_id}"
        if not isinstance(profile, dict):
            raise ValueError(prefix)  # noqa: TRY004 - invalid profile reference
        for name in (
            "state_lookback_months",
            "test_months",
            "step_months",
            "num_windows",
            "validation_windows",
        ):
            value = profile.get(name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{prefix}: {name} must be a positive integer")
        data_years = profile.get("data_years")
        if (
            type(data_years) not in {int, float}
            or not math.isfinite(data_years)
            or data_years <= 0
        ):
            raise ValueError(f"{prefix}: data_years must be positive")
        test_months, step_months = profile["test_months"], profile["step_months"]
        if test_months < minimum:
            raise ValueError(
                f"{prefix}: test_months must cover at least {minimum} months"
            )
        if profile["validation_windows"] != 1:
            raise ValueError(f"{prefix}: exactly one continuous holdout is required")
        purge = profile.get("purge_overlapping_windows", True)
        if type(purge) is not bool or (step_months < test_months and not purge):
            raise ValueError(f"{prefix}: overlapping windows require a purge")
        overlapping = max(0, (test_months + step_months - 1) // step_months - 1)
        independent = profile["num_windows"] - 1 - (overlapping if purge else 0)
        if independent < 1:
            raise ValueError(
                f"{prefix}: at least one independent ranking window is required"
            )


class ConfigurationTools:
    """Prepare safe previews and apply only a confirmed, unchanged preview."""

    def __init__(self, project_root: Path):
        self.project_root = Path(project_root).resolve()
        self._stores = {
            target: ConfigStore(self.project_root / target)
            for target in (_APP, _CONSTRAINTS)
        }
        if any(
            not store.path.is_relative_to(self.project_root)
            for store in self._stores.values()
        ):
            raise ValueError("配置文件不能通过链接指向项目目录之外")
        # Serializes assistant multi-file lock acquisition across processes.
        # Legacy writers acquire one ConfigStore lock and cannot deadlock it.
        self._transaction = ConfigStore(
            self.project_root / "config" / "assistant_transactions"
        )

    def _read(self) -> dict[str, dict]:
        try:
            return {target: store.load_raw() for target, store in self._stores.items()}
        except yaml.YAMLError:
            # YAML parser messages can quote the failing line, including secrets.
            raise ValueError("配置文件无法解析，请先修复 YAML 格式") from None

    def _registry(
        self, snapshots: dict[str, dict], changes: list[dict] | None = None
    ) -> dict[str, _Field]:
        from ...search.registry import list_solvers
        from ...strategy import get_strategy, list_strategy_ids

        app, constraints = snapshots[_APP], snapshots[_CONSTRAINTS]
        fields: dict[str, _Field] = {}

        def add(
            key: str,
            rule: _Rule,
            description: str,
            *,
            target: str = _APP,
            path: tuple[str | int, ...] | None = None,
            default: Any = None,
            effect: str = _NEXT_TASK,
            markets: tuple[str, ...] = _MARKETS,
        ) -> None:
            field_path = path if path is not None else tuple(key.split("."))
            if key.endswith("search.workers") and "SEARCH_WORKERS" in os.environ:
                effect = (
                    "保存后仍由 SEARCH_WORKERS 环境变量决定进程数；"
                    "移除该覆盖后下次任务读取"
                )
            if key.startswith("notification.") and key.endswith(".enabled"):
                channel = key.split(".")[1]
                if env_flag("SKIP_NOTIFICATIONS") or env_flag(
                    f"SKIP_{channel.upper()}"
                ):
                    effect = (
                        "保存后当前进程的 SKIP_* 环境开关仍会阻止通知；"
                        "移除该覆盖后下次任务读取"
                    )
            fields[key] = _Field(
                key,
                target,
                field_path,
                deepcopy(_get(snapshots[target], field_path, default)),
                rule,
                description,
                effect,
                markets,
            )

        for key, label in (
            ("stocks", "监控标的"),
            ("skip_search", "跳过策略搜索的标的"),
            ("skip_signals", "跳过信号扫描的标的"),
        ):
            add(key, _Rule("symbols"), label, default=[])
        announcement_values = _announcement_values(app)
        for name, rule, _default, description, _source in _ANNOUNCEMENT_FIELDS:
            # Preserve absent values as None in the diff; defaults are metadata.
            key = f"announcements.{name}"
            add(key, rule, description)
            fields[key] = replace(fields[key], value=announcement_values[key]["value"])
        for key, default in (("run_time", "19:00"), ("optimize_time", "02:00")):
            add(
                f"scheduler.{key}",
                _Rule("time"),
                "调度时间 HH:MM",
                default=default,
                effect=_SCHEDULE_EFFECT,
            )
        for key, default in (("daily_enabled", True), ("optimize_enabled", False)):
            add(
                f"scheduler.{key}",
                _Rule("boolean"),
                "启用定时任务",
                default=default,
                effect=_SCHEDULE_EFFECT,
            )
        add(
            "scheduler.daily_report_frequency",
            _Rule("enum", choices=("daily", "weekly", "off")),
            "普通日报频次；weekly 按配置的星期发送，告警仍即时发送",
            default="daily",
        )
        add(
            "scheduler.daily_report_triggers",
            _Rule("report_triggers"),
            "定时日报声明式发送条件；使用 path/operator/value，mode=any或all。空对象保持原行为",
            default={},
            effect=_SCHEDULE_EFFECT,
        )
        add(
            "scheduler.daily_report_weekday",
            _Rule("integer", 0, 6),
            "每周日报的星期，0=周一，6=周日",
            default=4,
        )
        briefs = _get(app, ("scheduler", "brief_reports"), [])
        if not isinstance(briefs, list):
            raise TypeError("scheduler.brief_reports 必须是列表")
        seen_briefs: set[str] = set()
        for index, brief in enumerate(briefs):
            if not isinstance(brief, dict):
                raise TypeError("简报配置必须是对象")
            brief_id = brief.get("id")
            if not isinstance(brief_id, str) or not re.fullmatch(
                r"[a-zA-Z0-9_-]+", brief_id
            ):
                raise ValueError("简报 id 无效")
            if brief_id in seen_briefs:
                raise ValueError("简报 id 重复")
            seen_briefs.add(brief_id)
            for suffix, rule, default in (
                ("run_time", _Rule("time"), "09:50"),
                ("enabled", _Rule("boolean"), True),
                ("skip_weekends", _Rule("boolean"), True),
                ("stocks", _Rule("symbols"), []),
                ("triggers", _Rule("report_triggers"), {}),
            ):
                add(
                    f"scheduler.brief_reports.{brief_id}.{suffix}",
                    rule,
                    f"简报 {brief_id} 的 {suffix}",
                    path=("scheduler", "brief_reports", index, suffix),
                    default=default,
                    effect=_SCHEDULE_EFFECT
                    if suffix in {"run_time", "enabled", "triggers"}
                    else _NEXT_TASK,
                )
        for channel in ("email", "feishu", "telegram"):
            section = _get(app, ("notification", channel), {}) or {}
            default = bool(app.get("email")) if channel == "email" else bool(section)
            add(
                f"notification.{channel}.enabled",
                _Rule("boolean"),
                f"{channel} 通知开关；进程 SKIP_* 环境开关仍优先",
                default=default,
            )

        markets = _get(app, ("optimizer", "markets"), {}) or {}
        selected = deepcopy(markets)
        for change in changes or []:
            parts = change["key"].split(".")
            if (
                len(parts) == 4
                and parts[:2] == ["optimizer", "markets"]
                and parts[2] in selected
                and parts[3] == "solver_id"
            ):
                if not isinstance(change["value"], str):
                    raise ValueError("solver_id 必须是已注册的 Solver 名称")
                selected[parts[2]]["solver_id"] = change["value"]
        for group in _MARKETS:
            spec = markets.get(group)
            if not isinstance(spec, dict):
                continue
            prefix = f"optimizer.markets.{group}"
            strategies = tuple(
                key
                for key in list_strategy_ids()
                if get_strategy(key).supports_market(group)
            )
            add(
                f"{prefix}.strategy",
                _Rule("enum", choices=strategies),
                "此市场下次搜索使用的策略；不修改已激活策略产物",
                markets=(group,),
            )
            solver_choices = tuple(
                solver
                for solver in list_solvers()
                if solver in _get(constraints, ("search", "solvers"), {})
            )
            add(
                f"{prefix}.solver_id",
                _Rule("enum", choices=solver_choices),
                "此市场的 Solver",
                markets=(group,),
            )
            for selector, registry in _PROFILES.items():
                add(
                    f"{prefix}.{selector}",
                    _Rule("enum", choices=tuple(sorted(constraints.get(registry, {})))),
                    f"此市场使用的 {registry} 配置方案",
                    markets=(group,),
                )
            old_solver = spec.get("solver_id")
            inherited = deepcopy(
                _get(constraints, ("search", "solvers", old_solver), {}) or {}
            )
            if old_solver == "genetic":
                inherited = {**constraints.get("genetic_search", {}), **inherited}
            solver_id = selected[group].get("solver_id")
            for name in sorted(_SOLVER_FIELDS.get(solver_id, set())):
                add(
                    f"{prefix}.solver_config.{name}",
                    _SOLVER_RULES[name],
                    f"此市场 {solver_id} 参数，覆盖同名共享默认值",
                    default=inherited.get(name),
                    markets=(group,),
                )
            for name, rule in _SEARCH_RULES.items():
                add(
                    f"{prefix}.search.{name}",
                    rule,
                    "此市场搜索资源配置",
                    default=_get(constraints, ("search", name)),
                    markets=(group,),
                )

        def affected(selector: str, profile: str) -> tuple[str, ...]:
            return tuple(
                group
                for group in _MARKETS
                if markets.get(group, {}).get(selector) == profile
            )

        for name, rule in _SEARCH_RULES.items():
            users = tuple(
                group
                for group in _MARKETS
                if name not in (markets.get(group, {}).get("search") or {})
            )
            if users:
                add(
                    f"search.{name}",
                    rule,
                    "所有未单独覆盖此字段的市场使用的搜索设置",
                    target=_CONSTRAINTS,
                    markets=users,
                )
        for name in ("buy_limit_levels", "sell_limit_levels"):
            add(
                f"simplified_search.{name}",
                _Rule("positive_numbers"),
                "所有策略使用的单次交易现金档位",
                target=_CONSTRAINTS,
            )
        for selector, registry, rules in (
            ("execution_profile", "execution_profiles", _EXECUTION_RULES),
            ("walk_forward_profile", "walk_forward_profiles", _WALK_FORWARD_RULES),
        ):
            for profile, values in constraints.get(registry, {}).items():
                users = affected(selector, profile)
                if not users or not isinstance(values, dict):
                    continue
                for name, rule in rules.items():
                    if name in values:
                        add(
                            f"{registry}.{profile}.{name}",
                            rule,
                            f"共享配置方案 {profile} 的 {name}",
                            target=_CONSTRAINTS,
                            markets=users,
                        )
                if registry == "execution_profiles":
                    for section, rule in (
                        ("lot_sizes", _Rule("integer", 1, 100_000)),
                        ("fx_rates", _Rule("number", 0.000001, 1_000_000)),
                        ("withholding_rates", _Rule("number", 0, 0.999999)),
                    ):
                        for group in users:
                            add(
                                f"{registry}.{profile}.{section}.{group}",
                                rule,
                                f"{profile} 的 {group} 执行参数",
                                target=_CONSTRAINTS,
                                markets=(group,),
                            )
        for profile, values in constraints.get("gate_profiles", {}).items():
            users = affected("gate_profile", profile)
            if not users or not isinstance(values, dict):
                continue
            for index, gate in enumerate(values.get("rules", [])):
                if not isinstance(gate, dict) or "value" not in gate:
                    continue
                gate_id, metric = gate.get("id"), str(gate.get("metric", ""))
                if not isinstance(gate_id, str) or not re.fullmatch(
                    r"[a-zA-Z0-9_-]+", gate_id
                ):
                    continue
                rule = _gate_value_rule(metric)
                add(
                    f"gate_profiles.{profile}.rules.{gate_id}.value",
                    rule,
                    f"{profile} / {metric} 阈值（{gate.get('operator')}）",
                    target=_CONSTRAINTS,
                    path=("gate_profiles", profile, "rules", index, "value"),
                    markets=users,
                )
                if gate.get("mode") == "penalty":
                    add(
                        f"gate_profiles.{profile}.rules.{gate_id}.penalty",
                        _Rule("number", 0, 1000),
                        f"{profile} / {gate_id} 惩罚权重",
                        target=_CONSTRAINTS,
                        path=("gate_profiles", profile, "rules", index, "penalty"),
                        default=0,
                        markets=users,
                    )
        return fields

    def describe(self) -> dict[str, Any]:
        """Return allowlisted values and metadata; never return raw config."""
        snapshots = self._read()
        announcement_values = _announcement_values(snapshots[_APP])
        return {
            "fields": [
                {
                    "key": spec.key,
                    "writable": True,
                    "value": spec.value,
                    "description": spec.description,
                    "target": spec.target,
                    "effect": spec.effect,
                    "affected_markets": list(spec.markets),
                    **spec.rule.describe(),
                    **announcement_values.get(spec.key, {}),
                }
                for spec in self._registry(snapshots).values()
            ]
        }

    def _prepare(
        self, changes: list[dict], snapshots: dict[str, dict]
    ) -> list[ConfigProposal]:
        registry = self._registry(snapshots, changes)
        announcement_values = _announcement_values(snapshots[_APP])
        grouped: dict[str, list[tuple[_Field, Any]]] = {}
        for change in changes:
            key, value = change["key"], change["value"]
            spec = registry.get(key)
            if spec is None:
                raise ValueError(f"不可修改的配置字段：{key}；请先查询可编辑字段")
            if key in announcement_values and not announcement_values[key]["valid"]:
                raise ValueError(f"{key} 原配置类型异常，未生成变更；原值未展示")
            try:
                spec.rule.validate(value)
            except OverflowError:
                raise ValueError(f"{key}: 数值超出有限范围") from None
            except ValueError as exc:
                raise ValueError(f"{key}: {exc}") from exc
            if spec.value != value:
                grouped.setdefault(spec.target, []).append((spec, value))
        proposals = []
        for target, items in grouped.items():
            proposed = deepcopy(snapshots)
            for spec, value in items:
                _set(proposed[target], spec.path, value)
            keys = [spec.key for spec, _ in items]
            if target == _APP and any(key.startswith("scheduler.") for key in keys):
                ScheduleManager._scheduled_specs(proposed[_APP])
            optimizer = target == _CONSTRAINTS or any(
                key.startswith("optimizer.") for key in keys
            )
            self._validate(proposed, optimizer=optimizer, before=snapshots)
            dependencies = {
                other: _revision(snapshots[other])
                for other in (_APP, _CONSTRAINTS)
                if other != target and optimizer
            }
            markets = sorted({group for spec, _ in items for group in spec.markets})
            effects = list(dict.fromkeys(spec.effect for spec, _ in items))
            proposals.append(
                ConfigProposal(
                    target=target,
                    revision=_revision(snapshots[target]),
                    changes=[
                        {"key": spec.key, "value": value} for spec, value in items
                    ],
                    diff=[
                        {
                            "key": spec.key,
                            "old": spec.value,
                            "new": value,
                            "path": list(spec.path),
                            "effect": spec.effect,
                            "affected_markets": list(spec.markets),
                            **(
                                {
                                    "old_configured": announcement_values[spec.key][
                                        "configured"
                                    ],
                                    "default": announcement_values[spec.key]["default"],
                                }
                                if spec.key in announcement_values
                                else {}
                            ),
                        }
                        for spec, value in items
                    ],
                    effect="；".join(effects),
                    affected_markets=markets,
                    dependencies=dependencies,
                )
            )
        if not proposals:
            raise ValueError("所选字段已经是指定值，无需修改")
        return proposals

    def propose(self, changes: list[dict]) -> list[dict]:
        """Validate independent changes, returning one preview per YAML file."""
        normalized = _check_changes(changes)
        with self._transaction._locked(5.0), ExitStack() as locks:
            for store in self._stores.values():
                locks.enter_context(store._locked(5.0))
            return [
                proposal.to_dict()
                for proposal in self._prepare(normalized, self._read())
            ]

    def _validate(
        self, snapshots: dict[str, dict], *, optimizer: bool, before: dict[str, dict]
    ) -> None:
        if not optimizer:
            return
        from ...search.config import get_market_optimizer_configs

        _validate_market_horizons(
            snapshots[_APP], snapshots[_CONSTRAINTS], before[_APP], before[_CONSTRAINTS]
        )
        path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".yaml",
                prefix=".assistant-validate-",
                dir=self.project_root / "config",
                delete=False,
            ) as output:
                path = Path(output.name)
                yaml.safe_dump(snapshots[_CONSTRAINTS], output, allow_unicode=True)
            # Use the same strict parser as real optimization and backtesting.
            get_market_optimizer_configs(snapshots[_APP], constraints_path=path)
        finally:
            if path is not None:
                path.unlink(missing_ok=True)

    def apply(self, proposal: dict) -> dict:
        """Persist a confirmed preview, checking versions inside locked mutate."""
        preview = ConfigProposal.from_dict(proposal)
        normalized = _check_changes(preview.changes)
        target = preview.target
        with self._transaction._locked(5.0), ExitStack() as locks:
            # Hold the other file stable during version checks and validation.
            for other, store in self._stores.items():
                if other != target:
                    locks.enter_context(store._locked(5.0))

            def mutate(raw: dict) -> None:
                snapshots = self._read()
                snapshots[target] = raw
                if _revision(raw) != preview.revision:
                    raise ConfigurationConflict("配置已变化，请重新预览并确认")
                for other, revision in preview.dependencies.items():
                    if _revision(snapshots[other]) != revision:
                        raise ConfigurationConflict("关联配置已变化，请重新预览并确认")
                actual = self._prepare(normalized, snapshots)
                if len(actual) != 1 or actual[0].to_dict() != preview.to_dict():
                    raise ConfigurationConflict(
                        "提案内容或影响范围已变化，请重新预览并确认"
                    )
                registry = self._registry(snapshots, normalized)
                for change in normalized:
                    _set(raw, registry[change["key"]].path, change["value"])

            try:
                saved = self._stores[target].update(mutate)
            except yaml.YAMLError:
                raise ValueError("配置文件无法解析，未写入变更") from None
        scheduler_changed = target == _APP and any(
            change["key"].startswith("scheduler.") for change in normalized
        )
        effect, refresh_error = preview.effect, None
        if scheduler_changed:
            manager = get_schedule_manager()
            if (
                manager is None
                or Path(manager.config_path).resolve() != self._stores[_APP].path
            ):
                effect = "配置已保存；调度器未在本进程运行，服务启动后生效"
            else:
                try:
                    manager.reload_config()
                    effect = "立即生效（运行中的调度器已刷新）；日报频次等业务设置在下次任务读取"
                except Exception as exc:  # noqa: BLE001 - the YAML is already committed
                    effect = "配置已保存，但调度器刷新失败；需要重启服务"
                    # Never propagate a runtime exception containing config/credentials.
                    refresh_error = type(exc).__name__
        return {
            "status": "applied" if refresh_error is None else "applied_reload_failed",
            "target": target,
            "revision": _revision(saved),
            "diff": preview.diff,
            "affected_markets": preview.affected_markets,
            "effect": effect,
            "runtime_refresh_error": refresh_error,
        }


def _gate_value_rule(metric: str) -> _Rule:
    if metric.endswith("_windows") or metric in {
        "ranking_window_count",
        "minimum_trade_count",
    }:
        return _Rule("integer", 0, 100_000)
    if metric.endswith("_ratio") and metric != "mean_sharpe_ratio":
        return _Rule("number", 0, 1)
    if metric == "average_position_pct":
        return _Rule("number", 0, 100)
    if metric == "minimum_drawdown_pct":
        return _Rule("number", -100, 0)
    if metric.endswith("range_pct") or metric == "excess_return_std_pct":
        return _Rule("number", 0, 1000)
    return _Rule("number", -10_000, 10_000)
