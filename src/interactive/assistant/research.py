"""Approved, reproducible research jobs with an offline Docker boundary.

No model-supplied command, path, mount, environment variable or Python source is
ever executed by the host. The host runs only the trusted data preparer. The
assistant service owns confirmation, authorization, persistence and its queue.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import json
import logging
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import textwrap
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal

import yaml
from pydantic import BaseModel, Field, StrictStr, root_validator, validator

SCRIPT_IDS = ("current_strategy", "justified_pb_value", "capm_dcf_value", "generated")
_JOB_ID = re.compile(r"[a-z0-9][a-z0-9_-]{7,63}\Z")
_CODE = re.compile(r"(?:\d{5,6}|[A-Z][A-Z0-9.-]{0,15})\Z")
_SECRET_KEY = re.compile(
    r"secret|password|token|api.?key|credential|private.?key|webhook", re.IGNORECASE
)
_CALCULATION_KEYS = {
    "stocks",
    "skip_search",
    "skip_signals",
    "optimizer",
    "point_in_time_data",
    "provider_access",
    "instrument_catalog",
    "instrument_audit",
    "capm_dcf_value",
}
_REGISTERED_SOURCE = {
    "current_strategy": "src/backtest/engine.py",
    "justified_pb_value": "scripts/backtest_justified_pb_value.py",
    "capm_dcf_value": "scripts/backtest_capm_dcf_value.py",
}
logger = logging.getLogger(__name__)


class PreparationMemoryError(RuntimeError):
    """Host preparation was stopped by its memory monitor."""


def _linux_memory_snapshot(pgid: int | None, proc_root: Path = Path("/proc")) -> dict:
    """Read host availability and aggregate RSS plus swap for one owned group."""
    values = {}
    for line in (proc_root / "meminfo").read_text(encoding="ascii").splitlines():
        fields = line.split()
        if fields and fields[0] in {"MemAvailable:", "MemTotal:"}:
            if len(fields) != 3 or fields[2] != "kB":
                raise ValueError("unsupported /proc/meminfo units")
            values[fields[0].rstrip(":")] = int(fields[1]) * 1024
    if "MemAvailable" not in values:
        raise ValueError("Linux MemAvailable is unavailable")
    rss, swap, pids = 0, 0, []
    if pgid is not None:
        with os.scandir(proc_root) as entries:
            for entry in entries:
                if not entry.name.isdecimal():
                    continue
                path = Path(entry.path)
                try:
                    # comm may itself contain spaces and parentheses.
                    fields = (
                        (path / "stat")
                        .read_text(encoding="utf-8", errors="replace")
                        .rsplit(")", 1)[1]
                        .split()
                    )
                    if int(fields[2]) != pgid or int(fields[3]) != pgid:
                        continue
                    status = (path / "status").read_text(
                        encoding="utf-8", errors="replace"
                    )
                except FileNotFoundError:
                    continue  # A process exited during this read.
                memory = {}
                for line in status.splitlines():
                    fields = line.split()
                    if fields and fields[0] in {"VmRSS:", "VmSwap:"}:
                        if len(fields) != 3 or fields[2] != "kB":
                            raise ValueError("unsupported /proc/status units")
                        memory[fields[0]] = int(fields[1]) * 1024
                if "VmRSS:" not in memory or "VmSwap:" not in memory:
                    if "State:\tZ" in status or "State:\tX" in status:
                        continue
                    raise ValueError("owned process RSS is unavailable")
                if any(value < 0 for value in memory.values()):
                    raise ValueError("invalid owned process memory counters")
                rss += memory["VmRSS:"]
                swap += memory.get("VmSwap:", 0)
                pids.append(int(entry.name))
    return {
        "rss_bytes": rss,
        "swap_bytes": swap,
        "rss_plus_swap_bytes": rss + swap,
        "available_bytes": values["MemAvailable"],
        "process_ids": sorted(pids),
    }


def _positive_months(value: Any, name: str, *, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _evaluation_horizon(constraints: Any, constraints_path: Path) -> dict:
    """Use the shared policy; preserve a strict boundary on older deployments."""
    walk_forward = constraints.walk_forward
    test_months = _positive_months(walk_forward.test_months, "walk_forward.test_months")
    _positive_months(
        walk_forward.state_lookback_months, "walk_forward.state_lookback_months"
    )
    if hasattr(constraints, "evaluation_horizon"):
        # A malformed modern object must fail, never fall back to the old core.
        policy = constraints.evaluation_horizon
        minimum = _positive_months(
            policy.minimum_evaluation_months,
            "evaluation_horizon.minimum_evaluation_months",
            minimum=24,
        )
        source = "shared_constraints.evaluation_horizon"
    else:
        try:
            raw = yaml.safe_load(constraints_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ValueError("invalid optimizer_constraints.yaml") from exc
        if not isinstance(raw, dict):
            raise ValueError("optimizer_constraints.yaml must be a mapping")
        minimum = 24
        source = "assistant_legacy_minimum_24_months"
        if "evaluation_horizon_policy" in raw:
            policy = raw["evaluation_horizon_policy"]
            defaults = {
                "contract": "minimum-two-year-evaluation/1",
                "minimum_evaluation_months": 24,
                "daily_portfolio_history_months": 36,
                "default_backtest_months": 36,
                "default_research_horizon_profile": "existing_data_66m_24m",
            }
            if not isinstance(policy, dict) or set(policy) - set(defaults):
                raise ValueError("invalid evaluation_horizon_policy declaration")
            policy = {**defaults, **policy}
            for field in ("contract", "default_research_horizon_profile"):
                if not isinstance(policy[field], str) or not policy[field].strip():
                    raise ValueError(f"evaluation_horizon_policy.{field} is invalid")
            minimum = _positive_months(
                policy["minimum_evaluation_months"],
                "evaluation_horizon_policy.minimum_evaluation_months",
                minimum=24,
            )
            for field in ("daily_portfolio_history_months", "default_backtest_months"):
                _positive_months(policy[field], field, minimum=minimum)
            source = (
                "optimizer_constraints.yaml:evaluation_horizon_policy (legacy core)"
            )
    return {
        "minimum_evaluation_months": max(minimum, test_months),
        "declared_minimum_evaluation_months": minimum,
        "walk_forward_test_months": test_months,
        "source": source,
        "note": "回测区间不低于24个月，且不短于已配置的测试窗口；沿用部署主机现有回测及基准实现。",
    }


def _provider_uses_shared_session(provider: Any) -> bool:
    """Verify that the provider consumes the guarded SDK and its own config."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(provider.fetch)))
    for node in ast.walk(tree):
        if not isinstance(node, ast.withitem):
            continue
        call = node.context_expr
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "baostock_session"
            and isinstance(node.optional_vars, ast.Name)
            and node.optional_vars.id == "bs"
        ):
            continue
        arguments = [
            *call.args[2:],
            *[item.value for item in call.keywords if item.arg == "config"],
        ]
        if any(
            isinstance(item, ast.Attribute)
            and isinstance(item.value, ast.Name)
            and item.value.id == "self"
            and item.attr == "config"
            for item in arguments
        ):
            return True
    return False


class ResearchJobSpec(BaseModel):
    """User-controlled research inputs; operational paths are never accepted."""

    script_id: Literal[
        "current_strategy", "justified_pb_value", "capm_dcf_value", "generated"
    ]
    codes: list[StrictStr] = Field(..., min_items=1, max_items=1000)
    start: StrictStr = Field(..., description="Evaluation start, YYYY-MM-DD")
    end: StrictStr = Field(..., description="Evaluation end, YYYY-MM-DD")
    dataset_id: StrictStr | None = Field(
        None, min_length=1, max_length=128,
        description="Optional source dataset from the unified catalog",
    )
    parameters: dict[str, Any] = Field(default_factory=dict)
    base_script: Literal["current_strategy", "justified_pb_value", "capm_dcf_value"] = (
        Field("current_strategy", description="Baseline used by generated research")
    )
    generated_code: StrictStr = Field("", max_length=100000)

    class Config:
        extra = "forbid"

    @validator("codes")
    def valid_codes(cls, values: list[str]) -> list[str]:
        normalized = [value.strip().upper() for value in values]
        if any(not _CODE.fullmatch(value) for value in normalized):
            raise ValueError("codes must be A-share, HK or US instrument identifiers")
        return list(dict.fromkeys(normalized))

    @validator("start", "end")
    def valid_date(cls, value: str) -> str:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("dates must use YYYY-MM-DD")
        return date.fromisoformat(value).isoformat()

    @validator("parameters")
    def primitive_parameters(cls, values: dict) -> dict:
        if len(values) > 100 or any(
            not isinstance(key, str)
            or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,63}", key)
            or type(value) not in (str, int, float, bool)
            for key, value in values.items()
        ):
            raise ValueError("parameters must contain named scalar strategy values")
        json.dumps(values, allow_nan=False)
        return values

    @root_validator(skip_on_failure=True)
    def consistent_job(cls, values: dict) -> dict:
        if values["end"] < values["start"]:
            raise ValueError("end precedes start")
        if date.fromisoformat(values["end"]) > datetime.now(timezone.utc).date():
            raise ValueError("a historical backtest cannot end in the future")
        generated = values["script_id"] == "generated"
        code = values.get("generated_code", "")
        if generated != bool(code.strip()):
            raise ValueError("generated_code is required only for generated research")
        if generated:
            # Parsing checks syntax only. Docker, not an AST allowlist, is the
            # security boundary for approved arbitrary Python experiments.
            try:
                ast.parse(code, filename="research.py")
            except (SyntaxError, RecursionError) as exc:
                raise ValueError("generated Python has invalid syntax") from exc
        return values


class JobResult(BaseModel):
    job_id: str
    status: Literal["completed", "failed", "cancelled", "interrupted"]
    summary: str
    artifacts: list[str] = Field(default_factory=list)
    exit_code: int | None = None
    errors: list[str] = Field(default_factory=list)


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_write(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def _safe_path(path: Path, root: Path, *, regular: bool = False) -> Path:
    """Reject symlink/junction escapes, including links in ancestor directories."""
    root = root.absolute()
    path = path.absolute()
    try:
        path.relative_to(root)
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("research path escapes its managed root") from exc
    for item in (path, *path.parents):
        if item.is_symlink() or (
            item.exists() and getattr(item.lstat(), "st_file_attributes", 0) & 0x400
        ):
            raise ValueError("research paths cannot use symbolic links or junctions")
        if item == root:
            break
    if regular and (not path.is_file() or not stat.S_ISREG(path.stat().st_mode)):
        raise ValueError("research input/artifact must be a regular file")
    return path


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _sanitize(item)
            for key, item in value.items()
            if not _SECRET_KEY.search(str(key))
        }
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


class ResearchRunner:
    """Freeze proposals, prepare real inputs, and run a bounded offline job."""

    def __init__(self, project_root: Path, settings: dict):
        self.project_root = Path(project_root).resolve()
        configured_root = Path(settings.get("workspace_root", "data/feishu_research"))
        self.workspace_root = (
            configured_root
            if configured_root.is_absolute()
            else self.project_root / configured_root
        ).absolute()
        # This is a deployment-owned setting; a dedicated folder may live on
        # another disk. It must not be the project, config or filesystem root.
        if self.workspace_root in {
            self.project_root,
            self.project_root / "config",
            Path(self.workspace_root.anchor),
        }:
            raise ValueError("research workspace must be a dedicated directory")
        _safe_path(self.workspace_root, self.workspace_root)
        self.settings = {
            "docker_image": str(
                settings.get("docker_image", "trade-eyes-research:local")
            ),
            "cpus": float(settings.get("cpus", 2)),
            "memory_mb": int(settings.get("memory_mb", 4096)),
            "prepare_memory_mb": int(
                settings.get("memory_mb", 4096)
                if settings.get("prepare_memory_mb") is None
                else settings["prepare_memory_mb"]
            ),
            "prepare_min_available_mb": int(
                settings.get("prepare_min_available_mb", 128)
            ),
            "timeout_seconds": int(settings.get("timeout_seconds", 3600)),
            "prepare_timeout_seconds": int(
                settings.get("prepare_timeout_seconds", 3600)
            ),
            "max_output_mb": int(settings.get("max_output_mb", 100)),
            "stock_data_auto_backfill": bool(
                settings.get("stock_data_auto_backfill", True)
            ),
            "stock_data_backfill_cooldown_seconds": int(
                settings.get("stock_data_backfill_cooldown_seconds", 1800)
            ),
            "stock_data_backfills_per_hour": int(
                settings.get("stock_data_backfills_per_hour", 6)
            ),
        }
        self._stock_backfill_lock = threading.Lock()
        self._stock_backfill_attempts: dict[str, tuple[float, dict]] = {}
        self._stock_backfill_started: list[float] = []
        if not re.fullmatch(
            r"[a-zA-Z0-9][a-zA-Z0-9_.:/@-]{0,254}", self.settings["docker_image"]
        ):
            raise ValueError("invalid Docker image")
        for key in (
            "cpus",
            "memory_mb",
            "prepare_memory_mb",
            "prepare_min_available_mb",
            "timeout_seconds",
            "prepare_timeout_seconds",
            "max_output_mb",
        ):
            if not math.isfinite(self.settings[key]) or self.settings[key] <= 0:
                raise ValueError(f"{key} must be positive")
        for key, lower in (
            ("prepare_memory_mb", 512),
            ("prepare_min_available_mb", 64),
        ):
            if not lower <= self.settings[key] <= 65536:
                raise ValueError(f"{key} must be between {lower} and 65536 MiB")
        self._processes: dict[str, subprocess.Popen] = {}
        self._events: dict[str, threading.Event] = {}
        self._lock = threading.RLock()
        self._owner = hashlib.sha256(str(self.workspace_root).encode()).hexdigest()[:16]

    @staticmethod
    def _host_prepare_support() -> dict:
        if not sys.platform.startswith("linux"):
            return {
                "ready": False,
                "error": "宿主补数内存监控目前仅支持Linux /proc；Windows/macOS暂不执行研究任务，可继续问答和只读数据查询。",
            }
        if not Path("/proc/meminfo").is_file():
            return {"ready": False, "error": "宿主补数需要可读取的Linux /proc内存统计"}
        try:
            _linux_memory_snapshot(None)
        except (OSError, ValueError) as exc:
            return {"ready": False, "error": f"宿主补数无法读取Linux内存统计：{exc}"}
        return {
            "ready": True,
            "mechanism": "Linux /proc polling every 100 ms; not a cgroup hard limit",
        }

    def _job_dir(self, job_id: str) -> Path:
        if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
            raise ValueError("invalid research job identifier")
        return _safe_path(self.workspace_root / job_id, self.workspace_root)

    def describe(self) -> dict:
        from ...strategy import list_strategies

        return {
            "scripts": [
                {
                    "id": name,
                    "source": _REGISTERED_SOURCE.get(name, "approved Python"),
                    "uses_shared_execution": True,
                }
                for name in SCRIPT_IDS
            ],
            "strategies": list_strategies(),
            "input_schema": ResearchJobSpec.schema(),
            "generated_contract": (
                "A shared-contract baseline backtest runs first. The approved Python "
                "then runs in Docker with CONTEXT containing spec, baseline, "
                "input_dir=/input, output_dir=/output. Import project APIs from src; "
                "write additional JSON/CSV/PNG under /output. No network or secrets. "
                "Choose base_script explicitly for a different baseline strategy. "
                "spec.dataset_id selects an indexed source dataset; only approved "
                "symbols are copied to a private host preparation cache, then the "
                "validated snapshot is exposed in /input/data. "
                "Read bundles via from src.data.market_history import PointInTimeMarketStore; "
                "PointInTimeMarketStore(Path(CONTEXT['input_dir'])/'data').read(code). "
                "Read point-in-time statements via from src.instruments.point_in_time import PointInTimeFundamentalStore; "
                "PointInTimeFundamentalStore(Path(CONTEXT['input_dir'])/'data').as_of(code, date.fromisoformat(CONTEXT['spec']['end'])); "
                "bundle.prices contains date, raw_open/high/low/close, qfq_open/high/low/close, "
                "qfq_factor, volume and tradable; bundle.actions contains dated corporate actions. "
                "CONTEXT['baseline'] has metrics, evaluation_report (including nav_dates/nav_series), "
                "strategy_id, market, spec, data_manifest and context_contract. "
                "Approved parameters/strategy live in /input/job.json resolved; "
                "selected data hashes and market cutoffs live in /input/data_manifest.json."
            ),
            "scope": "One market per job. Explicit codes and dates required; no silent subset.",
            "data": "Real raw/qfq prices, dated corporate actions, PIT statements and configured benchmarks.",
        }

    def describe_data(self, codes: list[str], dataset_id: str | None = None) -> dict:
        """Inspect local availability only; date spans are not completeness gates."""
        from ...core.config_store import ConfigStore
        from ...data.market_history import PointInTimeMarketStore
        from ...instruments.point_in_time import PointInTimeFundamentalStore

        if not isinstance(codes, list) or not 1 <= len(codes) <= 100:
            raise ValueError("data queries require 1 to 100 explicit instrument codes")
        if any(not isinstance(code, str) for code in codes):
            raise TypeError("instrument codes must be strings")
        selected = ResearchJobSpec.valid_codes(codes)
        if dataset_id is not None:
            from ...data.dataset_catalog import DatasetCatalog

            if not isinstance(dataset_id, str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{1,128}", dataset_id
            ):
                raise ValueError("invalid dataset identifier")
            root = DatasetCatalog(self.project_root).resolve_dataset_root(dataset_id)
        else:
            config = ConfigStore(self.project_root / "config/config.yaml").load_raw()
            configured = config.get("point_in_time_data", {}) or {}
            root = Path(configured.get("output_dir", "data/point_in_time"))
            root = root if root.is_absolute() else self.project_root / root
        market_store = PointInTimeMarketStore(root)
        statement_store = PointInTimeFundamentalStore(root)
        rows = []
        for code in selected:
            item = {
                "code": code,
                "market": {"status": "missing", "rows": 0},
                "fundamentals": {"status": "missing", "statements": 0},
            }
            try:
                stem = market_store._safe_code(code)
                for suffix in (".csv", ".actions.json", ".meta.json"):
                    _safe_path(root / "market" / (stem + suffix), root)
                bundle = market_store.read(code)
                if bundle is not None:
                    dates = bundle.prices["date"]
                    disclosures = [
                        action.published_at
                        for action in bundle.actions
                        if action.published_at is not None
                    ]
                    item["market"] = {
                        "status": "available",
                        "rows": len(dates),
                        "start": str(dates.min().date()),
                        "end": str(dates.max().date()),
                        "corporate_actions": len(bundle.actions),
                        "currency": bundle.currency,
                        "actions_published_start": str(min(disclosures))
                        if disclosures
                        else None,
                        "actions_published_end": str(max(disclosures))
                        if disclosures
                        else None,
                        "undated_actions": len(bundle.actions) - len(disclosures),
                    }
            except (OSError, ValueError, TypeError, KeyError) as exc:
                item["market"] = {"status": "invalid", "error": type(exc).__name__}
            try:
                _safe_path(statement_store._path(code), root)
                statements = statement_store.read_all(code)
                published = [
                    row.published_at
                    for row in statements
                    if row.published_at is not None
                ]
                if statements:
                    item["fundamentals"] = {
                        "status": "available",
                        "statements": len(statements),
                        "published_start": str(min(published)) if published else None,
                        "published_end": str(max(published)) if published else None,
                        "undated_statements": len(statements) - len(published),
                    }
            except (OSError, ValueError, TypeError, KeyError) as exc:
                item["fundamentals"] = {
                    "status": "invalid",
                    "error": type(exc).__name__,
                }
            rows.append(item)
        return {
            "instruments": rows,
            "completeness_checked": False,
            "configured_store": "dataset_catalog"
            if dataset_id
            else "point_in_time_data.output_dir",
            "dataset_id": dataset_id,
            "note": "只读缓存统计；首尾日期及条数不代表连续覆盖或财报完整性，未触发补数。",
        }

    def query_stock_fundamentals(
        self,
        code: str,
        as_of: str | None = None,
        dataset_id: str | None = None,
        *,
        _allow_backfill: bool = True,
    ) -> dict:
        """Find a stock across indexed datasets and return disclosure-safe metrics."""
        import pandas as pd

        from ...data.dataset_catalog import DatasetCatalog
        from ...data.market_history import PointInTimeMarketStore
        from ...instruments.calculations import derive_company_fundamentals
        from ...instruments.models import MetricStatus, MetricValue
        from ...instruments.point_in_time import (
            PointInTimeFundamentalStore,
            adjust_statement_shares,
        )

        selected = ResearchJobSpec.valid_codes([code])
        if len(selected) != 1:
            raise ValueError("code must be one supported instrument code")
        code = selected[0]
        today = datetime.now(timezone.utc).astimezone().date()
        evaluation_date = date.fromisoformat(as_of) if as_of else today
        if evaluation_date > today:
            raise ValueError("as_of cannot be in the future")
        catalog = DatasetCatalog(self.project_root)
        if dataset_id is not None:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", dataset_id):
                raise ValueError("invalid dataset identifier")
            all_matches = catalog.files(
                dataset_id=dataset_id, code=code, limit=20
            )["items"]
        else:
            matches = catalog.files(code=code, limit=100, offset=0)
            all_matches = list(matches["items"])
            while len(all_matches) < matches["total"] and len(all_matches) < 10_000:
                page = catalog.files(
                    code=code, limit=100, offset=len(all_matches)
                )
                if not page["items"]:
                    break
                all_matches.extend(page["items"])
        data_matches = [
            row
            for row in all_matches
            if row.get("kind") in {"price", "fundamental"}
            and Path(row.get("relative_path", "")).name.startswith(code + ".")
        ]
        reference_matches = [
            row
            for row in all_matches
            if Path(row.get("relative_path", "")).name.lower()
            == f"{code}.json"
            and "/codes/" in row.get("relative_path", "").lower()
        ]
        candidates = []
        for candidate_id in list(dict.fromkeys(
            [row["dataset_id"] for row in data_matches]
            + ([dataset_id] if dataset_id is not None else [])
        ))[:100]:
            try:
                root = catalog.resolve_dataset_root(candidate_id)
                market = PointInTimeMarketStore(root).read(code)
                store = PointInTimeFundamentalStore(root)
                statements = store.as_of(code, evaluation_date)
                item = {"dataset_id": candidate_id, "status": "unavailable"}
                if market is None:
                    item["reason"] = "market bundle missing"
                elif not statements:
                    item["reason"] = "no statements disclosed by as_of"
                    item["price_end"] = str(pd.to_datetime(market.prices["date"]).max().date())
                else:
                    prices = market.prices.copy()
                    prices["date"] = pd.to_datetime(prices["date"]).dt.date
                    prices = prices.loc[prices["date"] <= evaluation_date]
                    if prices.empty:
                        item["reason"] = "no market price on or before as_of"
                    else:
                        quote = prices.iloc[-1]
                        statements = adjust_statement_shares(
                            statements, market.actions, evaluation_date
                        )
                        company = derive_company_fundamentals(
                            statements,
                            current_price=MetricValue(
                                value=float(quote["raw_close"]),
                                status=MetricStatus.OBSERVED,
                                as_of=quote["date"],
                                source="selected_dataset_raw_close",
                                currency=market.currency,
                            ),
                            evaluation_date=evaluation_date,
                        )
                        published = [row.published_at for row in statements if row.published_at]
                        item.update(
                            status="available",
                            as_of=evaluation_date.isoformat(),
                            price_date=quote["date"].isoformat(),
                            raw_close=float(quote["raw_close"]),
                            statement_published_at=max(published).isoformat() if published else None,
                            metrics=json.loads(
                                company.json(exclude={"statements"})
                            ),
                            statement_count=len(statements),
                            currency=market.currency,
                        )
                candidates.append(item)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                candidates.append({
                    "dataset_id": candidate_id,
                    "status": "not_materialized",
                    "reason": str(exc)[:200] or type(exc).__name__,
                })
        available = [item for item in candidates if item["status"] == "available"]
        available.sort(key=lambda item: item.get("statement_published_at") or "", reverse=True)
        metadata_only = [
            {
                "dataset_id": row["dataset_id"],
                "status": "reference_metadata_only",
                "reason": "证券代码只出现在参考批次代码清单中，没有配套行情或财报文件。",
            }
            for row in reference_matches[:10]
        ]
        checked = list(dict.fromkeys(
            [item["dataset_id"] for item in candidates]
            + [item["dataset_id"] for item in metadata_only]
        ))
        response = {
            "code": code,
            "as_of": evaluation_date.isoformat(),
            "selected": available[0] if available else None,
            "datasets_checked": len(checked),
            "dataset_results": available[:5]
            if available
            else (candidates[:10] + metadata_only),
            "matched_file_inventory": [
                {
                    "dataset_id": row["dataset_id"],
                    "relative_path": row["relative_path"],
                    "kind": row["kind"],
                    "extension": Path(row["relative_path"]).suffix.lower(),
                }
                for row in (data_matches + reference_matches)[:20]
            ],
            "searched_all_indexed_datasets": dataset_id is None,
            "note": "仅使用披露日不晚于as_of的财报和as_of之前不复权收盘；指标含观察/推导状态、来源和缺失说明。数据集索引命中不等于完整性验收。",
        }
        if (
            response["selected"] is None
            and dataset_id is None
            and _allow_backfill
            and self.settings["stock_data_auto_backfill"]
        ):
            backfill = self._backfill_stock_data(code, evaluation_date)
            response["automatic_backfill"] = backfill
            if backfill.get("indexed_files", 0):
                refreshed = self.query_stock_fundamentals(
                    code,
                    as_of=evaluation_date.isoformat(),
                    _allow_backfill=False,
                )
                refreshed["automatic_backfill"] = backfill
                return refreshed
        return response

    def _backfill_stock_data(self, code: str, evaluation_date: date) -> dict:
        """Fetch one missing security through the shared point-in-time providers."""
        if not self.settings["stock_data_auto_backfill"]:
            return {"status": "disabled", "indexed_files": 0}
        now = time.monotonic()
        with self._stock_backfill_lock:
            previous = self._stock_backfill_attempts.get(code)
            cooldown = self.settings["stock_data_backfill_cooldown_seconds"]
            if previous and now - previous[0] < cooldown:
                prior = dict(previous[1])
                return {
                    **prior,
                    "status": "cooldown" if prior.get("status") != "running" else "running",
                    "indexed_files": 0,
                }
            self._stock_backfill_started = [
                started for started in self._stock_backfill_started
                if now - started < 3600
            ]
            if len(self._stock_backfill_started) >= self.settings["stock_data_backfills_per_hour"]:
                return {"status": "rate_limited", "indexed_files": 0}
            self._stock_backfill_started.append(now)
            self._stock_backfill_attempts[code] = (now, {"status": "running"})
        try:
            from ...core.config_store import ConfigStore
            from ...data.dataset_catalog import DatasetCatalog
            from ...data.market_history import PointInTimeMarketStore
            from ...data.point_in_time_backfill import PointInTimeBackfillService
            from ...instruments.point_in_time import PointInTimeFundamentalStore

            config = ConfigStore(self.project_root / "config/config.yaml").load_runtime()
            data_root = self.project_root / "data/point_in_time/assistant_ad_hoc"
            settings = config.setdefault("point_in_time_data", {})
            settings["output_dir"] = str(data_root)
            access = config.setdefault("provider_access", {}).setdefault("baostock", {})
            state_dir = Path(access.get("state_dir") or "data/provider_state/baostock")
            access["state_dir"] = str(
                (state_dir if state_dir.is_absolute() else self.project_root / state_dir)
                .resolve()
            )
            report = PointInTimeBackfillService(config).run(
                codes=[code], evaluation_date=evaluation_date
            )
            row = next(
                (
                    item
                    for item in report.get("instruments", [])
                    if item.get("code") == code
                ),
                {},
            )
            market = row.get("market_history", {}) or {}
            fundamentals = row.get("statements", {}) or {}
            market_store = PointInTimeMarketStore(data_root)
            statement_store = PointInTimeFundamentalStore(data_root)
            market_path = market_store.market_dir
            statement_path = statement_store._path(code)
            stem = PointInTimeMarketStore._safe_code(code)
            paths = [
                path
                for path in (
                    market_path / f"{stem}.csv",
                    market_path / f"{stem}.actions.json",
                    market_path / f"{stem}.meta.json",
                    market_path / f"{stem}.request.json",
                    statement_path,
                )
                if path.is_file()
            ]
            index = DatasetCatalog(self.project_root).index_paths(
                [path.relative_to(self.project_root).as_posix() for path in paths]
            ) if paths else {"indexed_files": 0}
            indexed_files = int(index.get("indexed_files", 0))
            status = (
                "completed"
                if market.get("status") == "success"
                and fundamentals.get("status") == "success"
                else "partial"
                if indexed_files
                else "failed"
            )
            result = {
                "status": status,
                "as_of": evaluation_date.isoformat(),
                "market_status": str(market.get("status", "missing")),
                "market_source": str(market.get("source", ""))[:100],
                "market_end": str(market.get("actual_end", "")),
                "fundamental_status": str(fundamentals.get("status", "missing")),
                "fundamental_last_period": str(fundamentals.get("last_period", "")),
                "indexed_files": indexed_files,
            }
        except Exception as exc:  # noqa: BLE001 - stock lookup must still explain the gap
            logger.warning(
                "Ad-hoc stock backfill failed for %s: %s", code, type(exc).__name__
            )
            result = {
                "status": "failed",
                "as_of": evaluation_date.isoformat(),
                "error": type(exc).__name__,
                "indexed_files": 0,
            }
        with self._stock_backfill_lock:
            self._stock_backfill_attempts[code] = (time.monotonic(), result)
        return result

    def calculate_gordon_ke_sensitivity(
        self, dataset_id: str, codes: list[str], as_of: str, growth_rates: list[float]
    ) -> dict:
        """Calculate disclosure-causal Gordon implied Ke for explicit g scenarios."""
        import numpy as np
        import pandas as pd

        from ...data.dataset_catalog import DatasetCatalog
        from ...data.market_history import PointInTimeMarketStore
        from ...instruments.calculations import derive_company_fundamentals
        from ...instruments.models import MetricStatus, MetricValue
        from ...instruments.point_in_time import (
            PointInTimeFundamentalStore,
            adjust_statement_shares,
        )

        if not isinstance(codes, list) or not 1 <= len(codes) <= 100:
            raise ValueError("Ke情景查询需要1到100个明确标的")
        selected = ResearchJobSpec.valid_codes(codes)
        if len(selected) != len(codes):
            raise ValueError("codes必须是不重复的明确标的")
        if not isinstance(dataset_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", dataset_id
        ):
            raise ValueError("dataset_id不是数据目录中的有效ID")
        if not isinstance(as_of, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of):
            raise ValueError("as_of必须为YYYY-MM-DD")
        evaluation_date = date.fromisoformat(as_of)
        if evaluation_date > datetime.now(timezone.utc).astimezone().date():
            raise ValueError("as_of不能晚于当前日期")
        if (
            not isinstance(growth_rates, list)
            or not 1 <= len(growth_rates) <= 12
            or any(type(value) not in (int, float) for value in growth_rates)
            or any(not math.isfinite(value) or not 0.0 <= value <= 0.20 for value in growth_rates)
            or len(set(growth_rates)) != len(growth_rates)
        ):
            raise ValueError("growth_rates需为1到12个不重复的0~0.20小数")
        root = DatasetCatalog(self.project_root).resolve_dataset_root(dataset_id)
        market_store = PointInTimeMarketStore(root)
        fundamental_store = PointInTimeFundamentalStore(root)
        rows = []
        for code in selected:
            item = {"code": code, "status": "unavailable", "scenarios": []}
            try:
                bundle = market_store.read(code)
                statements = fundamental_store.as_of(code, evaluation_date)
                if bundle is None:
                    item["reason"] = "selected dataset has no readable market bundle"
                elif not statements:
                    item["reason"] = "selected dataset has no statements published by as_of"
                else:
                    prices = bundle.prices.copy()
                    prices["date"] = pd.to_datetime(prices["date"]).dt.date
                    eligible = prices.loc[prices["date"] <= evaluation_date]
                    if eligible.empty:
                        item["reason"] = "no market price on or before as_of"
                    else:
                        quote = eligible.iloc[-1]
                        statements = adjust_statement_shares(
                            statements, bundle.actions, evaluation_date
                        )
                        company = derive_company_fundamentals(
                            statements,
                            current_price=MetricValue(
                                value=float(quote["raw_close"]),
                                status=MetricStatus.OBSERVED,
                                as_of=quote["date"],
                                source="selected_dataset_raw_close",
                                currency=bundle.currency,
                            ),
                            evaluation_date=evaluation_date,
                        )
                        roe, pb = company.roe_ttm.value, company.pb.value
                        if (
                            roe is None
                            or pb is None
                            or not np.isfinite(roe)
                            or not np.isfinite(pb)
                            or pb <= 0
                        ):
                            item.update(
                                reason="point-in-time ROE or positive PB is unavailable",
                                price_date=quote["date"].isoformat(),
                                statement_published_at=max(
                                    row.published_at for row in statements if row.published_at
                                ).isoformat(),
                            )
                        else:
                            item.update(
                                status="available",
                                price_date=quote["date"].isoformat(),
                                statement_published_at=max(
                                    row.published_at for row in statements if row.published_at
                                ).isoformat(),
                                currency=bundle.currency,
                                roe=float(roe) / 100.0,
                                pb=float(pb),
                                pb_method=company.pb.note,
                                scenarios=[
                                    {
                                        "g": float(growth),
                                        "implied_ke": float(growth)
                                        + (float(roe) / 100.0 - float(growth))
                                        / float(pb),
                                    }
                                    for growth in growth_rates
                                ],
                            )
            except (OSError, ValueError, TypeError, KeyError) as exc:
                item["reason"] = type(exc).__name__
            rows.append(item)
        return {
            "dataset_id": dataset_id,
            "as_of": as_of,
            "formula": "Ke_implied = g + (ROE_TTM - g) / PB",
            "roe_unit": "decimal ratio; 0.12 means 12%",
            "growth_unit": "decimal ratio; 0.02 means 2%",
            "point_in_time_only": True,
            "notice": "按披露日筛财报、按as_of选此前不复权收盘；逐标的返回PB来源。该隐含Ke不是CAPM Ke或收益预测。缺ROE/PB时不猜算。",
            "instruments": rows,
        }

    def _source_paths(self) -> list[Path]:
        paths = list((self.project_root / "src").rglob("*.py"))
        paths.extend((self.project_root / "config").glob("*.py"))
        paths.extend(
            self.project_root / name
            for name in (
                "main.py",
                "scripts/backtest_justified_pb_value.py",
                "scripts/backtest_capm_dcf_value.py",
            )
            if (self.project_root / name).is_file()
        )
        return sorted(set(paths))

    def _resolve_spec(self, spec: ResearchJobSpec, config: dict) -> dict:
        import pandas as pd

        from ...markets import _detect_fine_group
        from ...search.artifacts import load_latest_strategy_run
        from ...search.config import get_market_optimizer_config
        from ...strategy import Params, get_strategy

        groups = sorted({_detect_fine_group(code) for code in spec.codes})
        if len(groups) != 1:
            raise ValueError("请按市场分别准备研究任务；跨市场标的不会被自动忽略")
        market = groups[0]
        market_config = get_market_optimizer_config(
            market,
            application_config=config,
            constraints_path=self.project_root / "config/optimizer_constraints.yaml",
        )
        constraints = market_config.constraints
        horizon = _evaluation_horizon(
            constraints, self.project_root / "config/optimizer_constraints.yaml"
        )
        minimum_end = (
            pd.Timestamp(spec.start)
            + pd.DateOffset(months=horizon["minimum_evaluation_months"])
            - pd.Timedelta(days=1)
        )
        if pd.Timestamp(spec.end) < minimum_end:
            raise ValueError(
                f"回测区间至少 {horizon['minimum_evaluation_months']} 个月"
            )
        selected = spec.base_script if spec.script_id == "generated" else spec.script_id
        approval_files = []
        if selected == "current_strategy":
            root = self.project_root / "data/optimizer"
            pointer = root / "latest_strategy.yaml"
            _safe_path(pointer, self.project_root, regular=True)
            approval_files.append(pointer)
            manifest = yaml.safe_load(pointer.read_text(encoding="utf-8"))
            group_entry = (manifest.get("groups", {}) or {}).get(market, {})
            if group_entry.get("artifact"):
                approval_files.append(
                    _safe_path(root / group_entry["artifact"], root, regular=True)
                )
            active = load_latest_strategy_run(root=root, groups=(market,))
            strategy = active.strategy_for(market) if active else None
            params = active.params_by_group.get(market) if active else None
            if strategy is None or params is None:
                raise ValueError(f"{market} 没有已激活策略及参数")
            params = params.clone()
        else:
            strategy = get_strategy(selected)
            params = Params(values={}, _engine=selected)
        if strategy is None or not strategy.supports_market(market):
            raise ValueError(f"策略 {selected} 不支持 {market}")
        if strategy.name == "justified_pb_value" and len(spec.codes) < 2:
            raise ValueError(
                "Justified PB 横截面策略至少需要2只股票，并需在同一季度具有可用的真实财报与行情；请明确提供标的。"
            )
        if strategy.name == "capm_dcf_value":
            from ...data.market_history import PointInTimeMarketStore

            aliases = {
                code: PointInTimeMarketStore._safe_code(code)
                for code in spec.codes
                if PointInTimeMarketStore._safe_code(code) != code
            }
            if aliases:
                suggestions = ", ".join(
                    f"{old} → {new}" for old, new in aliases.items()
                )
                raise ValueError(
                    f"CAPM/DCF 需要PIT缓存规范标识，请明确改用：{suggestions}"
                )
        # Fixed research strategies start at the first declared execution tier;
        # the resolved amounts are displayed, frozen, and never randomized.
        defaults = {item.name: 0 for item in strategy.param_space.dims}
        params.values = strategy.parameter_schema.validate(
            {**defaults, **params.values, **spec.parameters}
        )
        if any(params.values[key] != value for key, value in spec.parameters.items()):
            raise ValueError("strategy parameter is outside its declared schema")
        if {"buy_cash_tier", "sell_cash_tier"}.intersection(spec.parameters):
            params.execution_snapshot = {}
        params.execution_snapshot = strategy.execution_params(params)
        benchmarks = [
            item
            for item in constraints.benchmark_codes_for(market)
            if item not in {"risk_free", "universe_equal_weight"}
        ]
        data_start = (
            pd.Timestamp(spec.start)
            - pd.DateOffset(months=constraints.walk_forward.state_lookback_months)
        ).date()
        lookback = {
            "walk_forward_months": constraints.walk_forward.state_lookback_months,
            "state_start": data_start.isoformat(),
        }
        if strategy.name == "capm_dcf_value":
            from ...fundamental_embedding.capital_cost import CapitalCostConfig
            from ...strategy.capm_dcf_value_context import (
                CapmDcfValueContextConfig,
                _market_settings,
                _value_settings,
            )

            years = list(CapitalCostConfig().beta_horizons_years)
            if not years or any(type(value) is not int or value < 1 for value in years):
                raise ValueError("invalid CapitalCostConfig.beta_horizons_years")
            selected = _market_settings(_value_settings(config), market)
            snapshot_config = CapmDcfValueContextConfig(
                maximum_snapshot_age_days=int(
                    selected.get(
                        "maximum_snapshot_age_days",
                        CapmDcfValueContextConfig().maximum_snapshot_age_days,
                    )
                )
            ).validate()
            # The first state day may carry a previously published quarterly
            # valuation. Its beta needs the full price window before that
            # snapshot, not merely before the requested evaluation start.
            earliest_snapshot = data_start - timedelta(
                days=snapshot_config.maximum_snapshot_age_days
            )
            beta_days = round(max(years) * 365.25)
            data_start = earliest_snapshot - timedelta(days=beta_days)
            lookback["beta_horizons_years"] = years
            lookback["beta_lookback_days"] = beta_days
            lookback["beta_source"] = "CapitalCostConfig.beta_horizons_years"
            lookback["maximum_snapshot_age_days"] = (
                snapshot_config.maximum_snapshot_age_days
            )
            lookback["earliest_snapshot_date"] = earliest_snapshot.isoformat()
            lookback["snapshot_age_source"] = (
                "CapmDcfValueContextConfig.maximum_snapshot_age_days with selected market overrides"
            )
        return {
            "market": market,
            "strategy_id": strategy.name,
            "parameters": params.values,
            "execution_snapshot": params.execution_snapshot,
            "benchmark_codes": benchmarks,
            "data_start": data_start.isoformat(),
            "history_lookback": lookback,
            "evaluation_horizon": horizon,
            "fundamental_dependencies": list(strategy.fundamental_feature_dependencies),
            "approval_files": [str(path) for path in approval_files],
        }

    def _freeze_value_dependencies(
        self, config: dict, resolved: dict, input_dir: Path
    ) -> list[Path]:
        if resolved["strategy_id"] != "capm_dcf_value":
            return []
        from ...strategy.capm_dcf_value_context import (
            CapmDcfValuePolicy,
            _market_settings,
            _value_settings,
        )

        selected = dict(_market_settings(_value_settings(config), resolved["market"]))
        missing = [
            key
            for key in ("data_root", "risk_free_rates_json", "frozen_policy_report")
            if not selected.get(key)
        ]
        if not selected.get("benchmark_symbol") and not selected.get(
            "benchmark_prices"
        ):
            missing.append("benchmark_symbol|benchmark_prices")
        if missing:
            raise ValueError("CAPM/DCF 缺少已配置依赖: " + ", ".join(missing))
        resolved["host_data_root"] = str(selected["data_root"])
        files = []
        dependencies = input_dir / "dependencies"
        dependencies.mkdir()
        for key in (
            "risk_free_rates_json",
            "frozen_policy_report",
            "industry_history",
            "benchmark_prices",
        ):
            if not selected.get(key):
                continue
            path = Path(str(selected[key]))
            path = path if path.is_absolute() else self.project_root / path
            _safe_path(path, self.project_root, regular=True)
            if (
                path.suffix.lower() not in {".json", ".csv"}
                or path.stat().st_size > 20 * 1024 * 1024
            ):
                raise ValueError(f"unsupported or oversized policy dependency: {key}")
            destination = dependencies / (key + path.suffix.lower())
            shutil.copyfile(path, destination)
            if key == "frozen_policy_report":
                CapmDcfValuePolicy.from_report(
                    destination, expected_market=resolved["market"]
                )
            files.append(path)
            selected[key] = "/input/dependencies/" + destination.name
        selected["data_root"] = "/input/data"
        selected.pop("markets", None)
        config.setdefault("optimizer", {})["capm_dcf_value"] = {
            "markets": {resolved["market"]: selected}
        }
        extras = [selected.get("benchmark_symbol", "")]
        extras.extend((selected.get("currency_conversion_symbols", {}) or {}).values())
        for symbol in extras:
            if symbol and symbol not in resolved["benchmark_codes"]:
                if not (
                    _CODE.fullmatch(str(symbol))
                    or re.fullmatch(r"[A-Z]{6}=X", str(symbol))
                ):
                    raise ValueError("unsupported value-policy market/FX symbol")
                resolved["benchmark_codes"].append(str(symbol))
        return files

    def prepare(self, spec: dict, job_id: str) -> dict:
        """Freeze a preview. This performs no data fetching or code execution."""
        supported = self._host_prepare_support()
        if not supported["ready"]:
            raise ValueError(supported["error"])
        request = ResearchJobSpec.parse_obj(spec)
        job_dir = self._job_dir(job_id)
        if job_dir.exists():
            raise ValueError("research job already exists")
        config_path = _safe_path(
            self.project_root / "config/config.yaml", self.project_root, regular=True
        )
        config_bytes = config_path.read_bytes()
        raw = yaml.safe_load(config_bytes)
        if not isinstance(raw, dict):
            raise TypeError("application configuration must be a mapping")
        resolved = self._resolve_spec(request, raw)
        snapshot = _sanitize(
            {key: value for key, value in raw.items() if key in _CALCULATION_KEYS}
        )
        input_dir = job_dir / "input"
        (input_dir / "code").mkdir(parents=True)
        (job_dir / "output").mkdir()
        if os.name != "nt":
            (job_dir / "output").chmod(0o777)
        approval = {"config/config.yaml": hashlib.sha256(config_bytes).hexdigest()}
        source_inventory = {}
        files = self._source_paths()
        files.extend(
            self.project_root / "config" / name
            for name in ("optimizer_constraints.yaml", "alerts.yaml")
            if (self.project_root / "config" / name).is_file()
        )
        for path in files:
            _safe_path(path, self.project_root, regular=True)
            relative = path.relative_to(self.project_root).as_posix()
            content = path.read_bytes()
            approval[relative] = hashlib.sha256(content).hexdigest()
            target = input_dir / "code" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if path.suffix == ".yaml":
                target.write_text(
                    yaml.safe_dump(
                        _sanitize(yaml.safe_load(content)), allow_unicode=True
                    ),
                    encoding="utf-8",
                )
            else:
                target.write_bytes(content)
        if request.dataset_id:
            from ...data.dataset_catalog import DatasetCatalog
            from ...data.market_history import PointInTimeMarketStore
            from ...instruments.point_in_time import PointInTimeFundamentalStore

            catalog = DatasetCatalog(self.project_root)
            source_root = catalog.resolve_dataset_root(request.dataset_id)
            source_details = catalog.details(request.dataset_id)
            seed_root = input_dir / "data_seed"
            market_seed = seed_root / "market"
            fundamental_seed = seed_root / "fundamentals"
            market_seed.mkdir(parents=True, exist_ok=True)
            fundamental_seed.mkdir(parents=True, exist_ok=True)
            market_codes, statement_codes = set(), set()
            for code in dict.fromkeys([*request.codes, *resolved["benchmark_codes"]]):
                stem = PointInTimeMarketStore._safe_code(code)
                for suffix in (".csv", ".actions.json", ".meta.json"):
                    source = source_root / "market" / (stem + suffix)
                    if not source.exists():
                        continue
                    _safe_path(source, source_root, regular=True)
                    relative = source.relative_to(self.project_root).as_posix()
                    digest = _sha(source)
                    target = market_seed / source.name
                    shutil.copyfile(source, target)
                    if _sha(target) != digest:
                        raise ValueError("所选数据集在冻结研究输入时发生变化")
                    approval[relative] = digest
                    source_inventory[relative] = digest
                    if suffix == ".csv":
                        market_codes.add(code)
                statement_path = source_root / "fundamentals" / (
                    PointInTimeFundamentalStore._safe_code(code) + ".statements.json"
                )
                if statement_path.exists():
                    _safe_path(statement_path, source_root, regular=True)
                    relative = statement_path.relative_to(self.project_root).as_posix()
                    digest = _sha(statement_path)
                    target = fundamental_seed / statement_path.name
                    shutil.copyfile(statement_path, target)
                    if _sha(target) != digest:
                        raise ValueError("所选财报在冻结研究输入时发生变化")
                    approval[relative] = digest
                    source_inventory[relative] = digest
                    statement_codes.add(code)
            resolved.update(
                source_dataset_id=request.dataset_id,
                source_dataset_logical_root=source_details["logical_root"],
                source_dataset_physical_root=source_details["physical_root"],
                source_dataset_file_count=len(source_inventory),
                source_market_codes=sorted(market_codes),
                source_statement_codes=sorted(statement_codes),
                source_missing_market_codes=sorted(set(request.codes) - market_codes),
                source_missing_statement_codes=sorted(
                    set(request.codes) - statement_codes
                ),
            )
        dependency_files = self._freeze_value_dependencies(
            snapshot, resolved, input_dir
        )
        for path in [*map(Path, resolved.pop("approval_files", [])), *dependency_files]:
            approval[path.relative_to(self.project_root).as_posix()] = _sha(path)
        # Host-only data settings freeze the approved provider paths and limits.
        # They are outside the Docker bind and contain no credentials.
        host_config = _sanitize(
            {
                key: value
                for key, value in raw.items()
                if key in _CALCULATION_KEYS | {"llm"}
            }
        )
        _json_write(job_dir / "host_config.json", host_config)
        snapshot.setdefault("point_in_time_data", {})["output_dir"] = "/input/data"
        snapshot["stocks"] = request.codes
        snapshot.pop("provider_access", None)
        snapshot["alerts"] = {
            "config_path": "/input/code/config/alerts.yaml",
            "enabled": False,
        }
        snapshot["notification"] = {
            name: {"enabled": False} for name in ("email", "feishu", "telegram")
        }
        snapshot["interactive"] = {
            name: {"enabled": False} for name in ("feishu", "telegram")
        }
        snapshot["logging"] = {"level": "WARNING", "file": "/tmp/research.log"}
        config_target = input_dir / "code/config/config.yaml"
        config_target.parent.mkdir(parents=True, exist_ok=True)
        config_target.write_text(
            yaml.safe_dump(snapshot, allow_unicode=True), encoding="utf-8"
        )
        script = (
            request.generated_code
            if request.script_id == "generated"
            else (
                "# Approved adapter for the project's shared research contract.\n"
                "from src.interactive.assistant.research_worker import run_registered\n"
                "run_registered()\n"
            )
        )
        (input_dir / "research.py").write_text(script, encoding="utf-8")
        normalized = json.loads(request.json())
        normalized.pop("generated_code")
        _json_write(
            input_dir / "job.json",
            {"job_id": job_id, "spec": normalized, "resolved": resolved},
        )
        hashes = {
            path.relative_to(input_dir).as_posix(): _sha(path)
            for path in input_dir.rglob("*")
            if path.is_file()
        }
        configured_codes = {
            str(item.get("code", "") if isinstance(item, dict) else item)
            for item in raw.get("stocks", [])
        }
        preparation = {
            "start": resolved["data_start"],
            "end": request.end,
            "fundamentals": resolved["fundamental_dependencies"],
            "container_price_start": resolved["data_start"],
            "history_lookback": resolved.get("history_lookback", {}),
            "source_dataset_id": request.dataset_id,
            "source_dataset_files_frozen": len(source_inventory),
        }
        if resolved["fundamental_dependencies"]:
            settings = raw.get("point_in_time_data", {}) or {}
            years = max(1, int(settings.get("history_years", 6)))
            # Match PointInTimeBackfillService.run exactly, including leap days.
            backfill_start = (
                date.fromisoformat(request.end) - timedelta(days=int(years * 365.25))
            ).isoformat()
            preparation.update(
                {
                    "fundamental_backfill_start": backfill_start,
                    "fundamental_backfill_end": request.end,
                    "history_years": years,
                    "market_backfill_check_start": min(
                        backfill_start, resolved["data_start"]
                    ),
                    "note": "财报补齐沿用共享流程，也会检查此较长历史区间的股票行情；容器价格输入仍从container_price_start起。上游财报可按整年查询，证券公开日期和结果区间继续校验。",
                }
            )
        preview = {
            "script_id": request.script_id,
            "strategy_id": resolved["strategy_id"],
            "market": resolved["market"],
            "codes": request.codes,
            "start": request.start,
            "end": request.end,
            "parameters": resolved["parameters"],
            "evaluation_horizon": resolved.get("evaluation_horizon", {}),
            "execution": resolved["execution_snapshot"],
            "benchmark_codes": resolved["benchmark_codes"],
            "data_preparation": preparation,
            "source_dataset_id": request.dataset_id,
            "source_dataset_logical_root": resolved.get("source_dataset_logical_root"),
            "source_dataset_physical_root": resolved.get("source_dataset_physical_root"),
            "source_market_codes": resolved.get("source_market_codes", []),
            "source_statement_codes": resolved.get("source_statement_codes", []),
            "source_missing_market_codes": resolved.get(
                "source_missing_market_codes", []
            ),
            "source_missing_statement_codes": resolved.get(
                "source_missing_statement_codes", []
            ),
            "scope": "full_configured_universe"
            if set(request.codes) == configured_codes
            else "scoped_research",
            "formal_acceptance": False,
            "output_dir": str(job_dir / "output"),
            "limits": self.settings,
            "script_sha256": hashes["research.py"],
        }
        _json_write(job_dir / "preview.json", preview)
        proposal = {
            "job_id": job_id,
            "job_dir": str(job_dir),
            "spec": normalized,
            "preview": preview,
            "input_hashes": hashes,
            "approval_sources": approval,
            "preview_files": [
                str(input_dir / "research.py"),
                str(job_dir / "preview.json"),
            ],
            "host_config_sha256": _sha(job_dir / "host_config.json"),
            "settings": dict(self.settings),
        }
        self.check(proposal)
        return proposal

    def check(self, proposal: dict) -> None:
        job_dir = self._job_dir(proposal["job_id"])
        if Path(proposal["job_dir"]).absolute() != job_dir:
            raise ValueError("research workspace changed")
        if proposal.get("settings") != self.settings:
            raise ValueError("research execution settings changed; preview again")
        for name, digest in proposal["approval_sources"].items():
            path = _safe_path(self.project_root / name, self.project_root, regular=True)
            if _sha(path) != digest:
                raise ValueError(f"配置或源码已变化，请重新预览: {name}")
        self._check_frozen(proposal)

    def _check_frozen(self, proposal: dict) -> None:
        job_dir = self._job_dir(proposal["job_id"])
        if Path(proposal["job_dir"]).absolute() != job_dir:
            raise ValueError("research workspace changed")
        if (
            _sha(_safe_path(job_dir / "host_config.json", job_dir, regular=True))
            != proposal["host_config_sha256"]
        ):
            raise ValueError("approved host data configuration changed")
        actual = {
            path.relative_to(job_dir / "input").as_posix()
            for path in (job_dir / "input").rglob("*")
            if path.is_file()
        }
        if actual != set(proposal["input_hashes"]):
            raise ValueError("approved research input file set changed")
        for name, digest in proposal["input_hashes"].items():
            path = _safe_path(job_dir / "input" / name, job_dir, regular=True)
            if _sha(path) != digest:
                raise ValueError("approved research script or snapshot changed")

    def preflight(self) -> dict:
        supported = self._host_prepare_support()
        if not supported["ready"]:
            return supported
        docker = shutil.which("docker")
        if not docker:
            return {"ready": False, "error": "Docker CLI 未安装或不在 PATH"}
        try:
            server = subprocess.run(
                [docker, "info", "--format", "{{json .}}"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            info = json.loads(server.stdout) if not server.returncode else {}
            if info.get("OSType") != "linux":
                return {"ready": False, "error": "需要可连接的 Linux Docker daemon"}
            memory = int(info.get("MemTotal", 0))
            cpus = int(info.get("NCPU", 0))
            if (
                memory < self.settings["memory_mb"] * 1024 * 1024
                or cpus < self.settings["cpus"]
            ):
                return {
                    "ready": False,
                    "error": "Docker 主机总资源小于研究任务配置上限",
                    "host_memory_mb": memory // (1024 * 1024),
                    "host_cpus": cpus,
                }
            result = subprocess.run(
                [
                    docker,
                    "image",
                    "inspect",
                    self.settings["docker_image"],
                    "--format",
                    "{{.Id}}",
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            image_id = result.stdout.strip()
            if result.returncode or not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
                return {
                    "ready": False,
                    "error": "研究镜像不存在；请先构建部署文档指定镜像",
                }
            compatibility = self.project_compatibility(self.project_root)
            if not compatibility["ready"]:
                return {
                    "ready": False,
                    "error": "部署代码缺少研究执行所需合同",
                    "compatibility": compatibility,
                }
            return {
                "ready": True,
                "docker": docker,
                "image_id": image_id,
                "host_memory_mb": memory // (1024 * 1024),
                "host_cpus": cpus,
                "limits": dict(self.settings),
                "compatibility": compatibility,
            }
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return {"ready": False, "error": "Docker 预检失败或超时"}

    @staticmethod
    def project_compatibility(project_root: Path | None = None) -> dict:
        """Inspect required contracts without evaluating, fetching, or migrating."""
        contracts = {
            "src.core.config_store": {"ConfigStore.load_runtime": set()},
            "src.core.process_lock": {"exclusive_process_lock": set()},
            "src.data.backtest_data": {
                "prepare_backtest_data": {
                    "config",
                    "codes",
                    "start_date",
                    "end_date",
                    "benchmark_codes",
                    "strategy",
                    "readiness_path",
                },
                "validate_market_bundle": {"bundle", "code", "start", "end"},
            },
            "src.data.market_history": {
                "PointInTimeMarketStore.read": {"code"},
                "PointInTimeMarketStore.write": {"bundle"},
            },
            "src.data.market_calendar": {"resolve_market_data_cutoff": set()},
            "src.data.baostock_access": {"guarded_baostock_session": set()},
            "src.data.point_in_time_backfill": {
                "PointInTimeBackfillService.run": {"codes", "evaluation_date"}
            },
            "src.instruments.point_in_time": {
                "PointInTimeFundamentalStore.read_all": {"code"},
                "PointInTimeFundamentalStore.upsert": {"code", "statements"},
            },
            "src.search.config": {
                "get_market_optimizer_config": {
                    "group",
                    "application_config",
                    "constraints_path",
                }
            },
            "src.search.artifacts": {"load_latest_strategy_run": {"groups", "root"}},
            "src.strategy.api": {
                "TradingStrategy.make_context_enricher": {"config", "market", "symbols"}
            },
            "src.strategy.plugins.justified_pb_value": {},
            "src.strategy.plugins.capm_dcf_value": {},
            "src.fundamental_embedding.capital_cost": {
                "estimate_robust_beta": {"company", "benchmark", "evaluation_date"}
            },
            "src.backtest.engine": {
                "evaluate_all_groups": {
                    "benchmark_bundles",
                    "market_bundles",
                    "context_enricher",
                    "market_constraints",
                }
            },
        }
        files, issues = {}, []
        resolved_horizons = {}
        dependencies = {}
        for package in ("exchange_calendars", "numba"):
            try:
                module = importlib.import_module(package)
                dependencies[package] = module.__version__
                if package == "exchange_calendars":
                    from packaging.version import Version

                    if (
                        not Version("4.13.1")
                        <= Version(module.__version__)
                        < Version("5")
                    ):
                        raise ValueError("exchange-calendars>=4.13.1,<5 is required")
            except (ImportError, AttributeError, ValueError) as exc:
                issues.append({"module": package, "reason": str(exc)[:300]})
        for module_name, members in contracts.items():
            try:
                module = importlib.import_module(module_name)
                path = Path(module.__file__)
                files[module_name.replace(".", "/") + ".py"] = _sha(path)
                for member, required in members.items():
                    target = module
                    for part in member.split("."):
                        target = getattr(target, part)
                    missing = required - set(inspect.signature(target).parameters)
                    if missing:
                        raise ValueError(f"{member} lacks {sorted(missing)}")
                if module_name == "src.data.market_history":
                    columns = set(module.PriceHistoryBundle.REQUIRED_COLUMNS)
                    if not {
                        "raw_close",
                        "qfq_close",
                        "qfq_factor",
                        "tradable",
                    }.issubset(columns):
                        raise ValueError("raw/qfq market contract missing")
                if (
                    module_name == "src.strategy.api"
                    and "execution_snapshot" not in module.Params.__dataclass_fields__
                ):
                    raise ValueError("immutable execution snapshot contract missing")
                if module_name == "src.fundamental_embedding.capital_cost":
                    horizons = module.CapitalCostConfig().beta_horizons_years
                    if not horizons or any(
                        type(value) is not int or value < 1 for value in horizons
                    ):
                        raise ValueError(
                            "CapitalCostConfig.beta_horizons_years contract missing"
                        )
            except (ImportError, AttributeError, OSError, TypeError, ValueError) as exc:
                issues.append({"module": module_name, "reason": str(exc)[:300]})
        try:
            from ...data.baostock_access import guarded_baostock_session
            from ...data.market_history import BaostockMarketHistoryProvider
            from ...instruments.point_in_time import BaostockStatementProvider

            for provider in (BaostockMarketHistoryProvider, BaostockStatementProvider):
                module = importlib.import_module(provider.__module__)
                if (
                    module.baostock_session is not guarded_baostock_session
                    or not _provider_uses_shared_session(provider)
                ):
                    raise ValueError(
                        f"{provider.__name__} lacks shared provider limits"
                    )
        except (ImportError, AttributeError, OSError, TypeError, ValueError) as exc:
            issues.append(
                {"module": "src.data.baostock_access", "reason": str(exc)[:300]}
            )
        try:
            from ...core.config_store import ConfigStore
            from ...search.config import get_market_optimizer_config

            root = project_root or Path(__file__).resolve().parents[3]
            config = ConfigStore(root / "config/config.yaml").load_raw()
            markets = config.get("optimizer", {}).get("markets", {})
            if not isinstance(markets, dict) or not markets:
                raise ValueError("optimizer.markets is missing")
            path = root / "config/optimizer_constraints.yaml"
            for market in markets:
                constraints = get_market_optimizer_config(
                    market, application_config=config, constraints_path=path
                ).constraints
                resolved_horizons[market] = _evaluation_horizon(constraints, path)
        except (ImportError, AttributeError, OSError, TypeError, ValueError) as exc:
            issues.append({"module": "src.search.config", "reason": str(exc)[:300]})
        return {
            "ready": not issues,
            "issues": issues,
            "required_files_sha256": files,
            "dependencies": dependencies,
            "evaluation_horizons": resolved_horizons,
            "boundary": "API/source probe only; raw-price and PIT regression tests remain required when these files differ.",
        }

    def _container_name(self, job_id: str) -> str:
        self._job_dir(job_id)
        return f"feishu-research-{self._owner}-{job_id}"

    @staticmethod
    def _make_inputs_readable(input_dir: Path) -> None:
        """A service's private umask must not prevent the non-root container."""
        if os.name == "nt":
            return
        for path in [input_dir, *input_dir.rglob("*")]:
            _safe_path(path, input_dir)
            if path.is_dir():
                path.chmod(0o755)
            else:
                _safe_path(path, input_dir, regular=True).chmod(0o444)

    def docker_command(
        self,
        proposal: dict,
        *,
        docker: str,
        image_id: str,
        custom: bool = False,
        timeout: int | None = None,
    ) -> list[str]:
        job_dir = self._job_dir(proposal["job_id"])
        for path in (job_dir / "input", job_dir / "output"):
            _safe_path(path, self.workspace_root)
            if "," in str(path):
                raise ValueError("Docker mount paths cannot contain commas")
        limit = self.settings
        output_dir = job_dir / "output/custom" if custom else job_dir / "output"
        _safe_path(output_dir, job_dir)
        return [
            docker,
            "run",
            "--rm",
            "--pull=never",
            "--init",
            "--name",
            self._container_name(proposal["job_id"]),
            "--label",
            f"trade-eyes.feishu-research={self._owner}",
            "--label",
            f"trade-eyes.job={proposal['job_id']}",
            "--network=none",
            "--read-only",
            "--user=65532:65532",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges=true",
            "--pids-limit=128",
            f"--cpus={limit['cpus']:g}",
            f"--memory={limit['memory_mb']}m",
            f"--memory-swap={limit['memory_mb']}m",
            "--ulimit",
            "nofile=256:256",
            "--ulimit",
            "core=0:0",
            "--ulimit",
            f"fsize={limit['max_output_mb'] * 1024 * 1024}",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=256m,mode=1777",
            "--mount",
            f"type=bind,source={job_dir / 'input'},target=/input,readonly",
            "--mount",
            f"type=bind,source={output_dir},target=/output",
            "--workdir=/input/code",
            "--env=PYTHONPATH=/input/code",
            "--env=PYTHONDONTWRITEBYTECODE=1",
            "--env=PYTHONUTF8=1",
            "--env=MPLCONFIGDIR=/tmp/matplotlib",
            "--env=NUMBA_CACHE_DIR=/tmp/numba",
            "--env=HOME=/tmp",
            "--env=SKIP_EMAIL=true",
            "--env=OPENBLAS_NUM_THREADS=1",
            "--env=OMP_NUM_THREADS=1",
            "--entrypoint=timeout",
            image_id,
            "--signal=TERM",
            "--kill-after=10s",
            f"{timeout or limit['timeout_seconds']}s",
            "python",
            "-B",
            "-m",
            "src.interactive.assistant.research_worker",
            "custom" if custom else "execute",
        ]

    @staticmethod
    def _terminate(
        process: subprocess.Popen, *, include_group_after_exit: bool = False
    ) -> None:
        if process.poll() is not None and not (
            include_group_after_exit and os.name != "nt"
        ):
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
                if process.poll() is None:
                    process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def cleanup_recovered(self, proposal: dict) -> None:
        """Remove only this runner's deterministically named stale container."""
        docker = shutil.which("docker")
        if docker:
            try:
                subprocess.run(
                    [docker, "rm", "-f", self._container_name(proposal["job_id"])],
                    capture_output=True,
                    timeout=15,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass

    def cancel(self, job_id: str) -> None:
        with self._lock:
            event = self._events.get(job_id)
            process = self._processes.get(job_id)
        if event:
            event.set()
        if process:
            self._terminate(process)
        self.cleanup_recovered({"job_id": job_id})

    def stop(self) -> None:
        with self._lock:
            identifiers = list(self._events)
        for job_id in identifiers:
            self.cancel(job_id)

    def _output_size(
        self, job_dir: Path, cancel_event: threading.Event | None = None
    ) -> int:
        size = 0
        count = 0
        pending = [_safe_path(job_dir / "output", job_dir)]
        # scandir is incremental; pathlib.rglob may materialize an adversarial
        # directory before yielding and therefore delay cancellation/limits.
        while pending:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    if cancel_event is not None and cancel_event.is_set():
                        raise InterruptedError("研究任务已取消")
                    count += 1
                    if count > 1000:
                        raise ValueError(
                            "research output exceeds 1000 filesystem entries"
                        )
                    path = _safe_path(Path(entry.path), job_dir)
                    information = entry.stat(follow_symlinks=False)
                    if stat.S_ISREG(information.st_mode):
                        size += information.st_size
                        if size > self.settings["max_output_mb"] * 1024 * 1024:
                            raise ValueError(
                                "research output exceeds the configured size limit"
                            )
                    elif stat.S_ISDIR(information.st_mode):
                        pending.append(path)
                    else:
                        raise ValueError("research produced a non-regular artifact")
        return size

    def _run_process(
        self,
        command: list[str],
        proposal: dict,
        cancel_event: threading.Event,
        *,
        timeout: int,
        log_name: str,
        cwd: Path | None = None,
        protect_host_memory: bool = False,
    ) -> int:
        job_dir = self._job_dir(proposal["job_id"])
        resources = {
            "contract": "feishu-host-prepare-memory-monitor/1",
            "mechanism": "Linux /proc polling; not a kernel/cgroup hard memory limit",
            "sample_interval_ms": 100,
            "memory_limit_bytes": self.settings["prepare_memory_mb"] * 1024 * 1024,
            "minimum_available_bytes": self.settings["prepare_min_available_mb"]
            * 1024
            * 1024,
            "peak_rss_bytes": 0,
            "peak_swap_bytes": 0,
            "peak_rss_plus_swap_bytes": 0,
            "minimum_observed_available_bytes": None,
            "samples": 0,
            "status": "preflight",
        }
        resource_path = job_dir / "prepare_resources.json"
        if protect_host_memory:
            try:
                supported = self._host_prepare_support()
                if not supported["ready"]:
                    raise PreparationMemoryError(supported["error"])
                self._observe_prepare_memory(None, resources)
            except PreparationMemoryError as exc:
                resources.update(status="failed", reason=str(exc))
                _json_write(resource_path, resources)
                raise
            _json_write(resource_path, resources)
        env = dict(os.environ)
        # Only trusted host preparation gets the host environment. Docker's
        # child environment is fixed explicitly in docker_command.
        env.update(
            {
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUTF8": "1",
                "PYTHONUNBUFFERED": "1",
                "SKIP_EMAIL": "true",
            }
        )
        kwargs = (
            {
                "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_NO_WINDOW
            }
            if os.name == "nt"
            else {"start_new_session": True}
        )
        process = subprocess.Popen(
            command,
            cwd=cwd or self.project_root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            **kwargs,
        )
        with self._lock:
            self._processes[proposal["job_id"]] = process
        overflow = threading.Event()
        log_limit = min(self.settings["max_output_mb"] * 1024 * 1024, 2 * 1024 * 1024)

        def drain() -> None:
            written = 0
            with (job_dir / log_name).open("wb") as stream:
                while True:
                    chunk = process.stdout.read1(4096)
                    if not chunk:
                        break
                    available = max(0, log_limit - written)
                    stream.write(chunk[:available])
                    stream.flush()
                    written += len(chunk)
                    if written > log_limit:
                        overflow.set()

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout
        next_resource_write = 0.0
        try:
            while process.poll() is None:
                if cancel_event.wait(0.1):
                    raise InterruptedError("研究任务已取消")
                if protect_host_memory:
                    self._observe_prepare_memory(process.pid, resources)
                    if time.monotonic() >= next_resource_write:
                        _json_write(resource_path, resources)
                        next_resource_write = time.monotonic() + 1.0
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"研究阶段超时（{timeout} 秒）")
                if (
                    overflow.is_set()
                    or self._output_size(job_dir, cancel_event)
                    > self.settings["max_output_mb"] * 1024 * 1024
                ):
                    raise ValueError("研究输出超过配置限额")
            if cancel_event.is_set():
                raise InterruptedError("研究任务已取消")
            if protect_host_memory:
                self._observe_prepare_memory(process.pid, resources)
            reader.join(timeout=5)
            if reader.is_alive():
                raise RuntimeError("研究进程退出后日志管道仍未关闭")
            if overflow.is_set():
                raise ValueError("研究日志超过配置限额")
            self._output_size(job_dir)
            resources["status"] = "completed" if process.returncode == 0 else "failed"
            return process.returncode
        except BaseException as exc:
            resources.update(status="failed", reason=str(exc))
            raise
        finally:
            if process.poll() is None or protect_host_memory:
                self._terminate(process, include_group_after_exit=protect_host_memory)
            reader.join(timeout=5)
            if protect_host_memory:
                resources["exit_code"] = process.poll()
                _json_write(resource_path, resources)
            with self._lock:
                self._processes.pop(proposal["job_id"], None)

    @staticmethod
    def _observe_prepare_memory(pgid: int | None, resources: dict) -> None:
        try:
            sample = _linux_memory_snapshot(pgid)
        except (OSError, ValueError, IndexError, KeyError) as exc:
            raise PreparationMemoryError(
                f"宿主补数内存监控无法读取可靠统计，已停止准备：{exc}"
            ) from exc
        resources["samples"] += 1
        resources["last_sample"] = sample
        resources["sampled_at"] = datetime.now(timezone.utc).isoformat()
        resources["status"] = "running" if pgid is not None else "preflight"
        for name in ("rss", "swap", "rss_plus_swap"):
            key = f"{name}_bytes"
            resources[f"peak_{key}"] = max(resources[f"peak_{key}"], sample[key])
        current_minimum = resources["minimum_observed_available_bytes"]
        resources["minimum_observed_available_bytes"] = (
            sample["available_bytes"]
            if current_minimum is None
            else min(current_minimum, sample["available_bytes"])
        )
        if sample["rss_plus_swap_bytes"] > resources["memory_limit_bytes"]:
            raise PreparationMemoryError(
                "宿主补数进程组内存超过限额，已停止准备："
                f"RSS+swap={sample['rss_plus_swap_bytes'] / 1048576:.1f} MiB，"
                f"上限={resources['memory_limit_bytes'] // 1048576} MiB"
            )
        if sample["available_bytes"] < resources["minimum_available_bytes"]:
            raise PreparationMemoryError(
                "宿主可用内存低于保留量，已停止准备："
                f"可用={sample['available_bytes'] / 1048576:.1f} MiB，"
                f"最低={resources['minimum_available_bytes'] // 1048576} MiB"
            )

    def safe_artifacts(self, proposal: dict, result: dict | None = None) -> list[str]:
        job_dir = self._job_dir(proposal["job_id"])
        self._output_size(job_dir)
        paths = [
            job_dir / "input/research.py",
            job_dir / "preview.json",
            job_dir / "execution.json",
            job_dir / "input/data_manifest.json",
            job_dir / "data_readiness.json",
            job_dir / "prepare_resources.json",
            job_dir / "prepare.log",
            job_dir / "run.log",
            job_dir / "custom.log",
        ]
        paths.extend(sorted((job_dir / "output").rglob("*")))
        return [
            str(_safe_path(path, job_dir, regular=True))
            for path in paths
            if path.is_file()
            and path.stat().st_size <= self.settings["max_output_mb"] * 1024 * 1024
        ]

    @staticmethod
    def _proposal_digest(proposal: dict) -> str:
        return hashlib.sha256(
            json.dumps(
                {
                    "inputs": proposal["input_hashes"],
                    "host_config": proposal["host_config_sha256"],
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()

    def recover_result(self, proposal: dict) -> dict:
        """Recover a sealed host receipt or preserve evidence as interrupted.

        The result receipt and hashes live outside all container write mounts.
        Neither branch executes data preparation or reruns the experiment.
        """
        self.cleanup_recovered(proposal)
        job_dir = self._job_dir(proposal["job_id"])
        errors = []
        try:
            artifacts = self.safe_artifacts(proposal)
            receipt = _safe_path(job_dir / "result.json", job_dir)
            if receipt.is_file():
                if receipt.stat().st_size > 512 * 1024:
                    raise ValueError("research result receipt is oversized")
                saved = json.loads(receipt.read_text(encoding="utf-8"))
                parsed = JobResult.parse_obj(saved)
                if parsed.job_id != proposal["job_id"]:
                    raise ValueError("research result belongs to a different job")
                if saved.get("proposal_sha256") != self._proposal_digest(proposal):
                    raise ValueError(
                        "research result is not bound to the approved inputs"
                    )
                recorded = saved.get("artifact_hashes", {})
                expected = {
                    str(Path(path).relative_to(job_dir)): _sha(
                        _safe_path(Path(path), job_dir, regular=True)
                    )
                    for path in parsed.artifacts
                }
                if recorded != expected or not set(parsed.artifacts).issubset(
                    artifacts
                ):
                    raise ValueError("research evidence changed after completion")
                return {
                    **parsed.dict(),
                    "artifact_hashes": recorded,
                    "proposal_sha256": saved["proposal_sha256"],
                }
        except (OSError, ValueError, TypeError) as exc:
            errors.append(str(exc))
            artifacts = []
        return JobResult(
            job_id=proposal["job_id"],
            status="interrupted",
            summary="服务重启前的研究状态不确定；已保留证据，不会自动重跑",
            artifacts=artifacts,
            errors=errors,
        ).dict()

    def run(
        self,
        proposal: dict,
        cancel_event: threading.Event,
        on_progress: Callable[[str], None],
        *,
        cached_only: bool = False,
    ) -> dict:
        job_id = proposal["job_id"]
        job_dir = self._job_dir(job_id)
        status, summary, exit_code = "failed", "研究任务未完成", None
        errors = []

        def progress(message: str) -> None:
            try:
                on_progress(message)
            except Exception:  # noqa: BLE001 - delivery does not control execution
                logger.warning("Research progress delivery failed for %s", job_id)

        with self._lock:
            self._events[job_id] = cancel_event
        try:
            self._check_frozen(proposal)
            if cancel_event.is_set():
                raise InterruptedError("研究任务已取消")
            ready = self.preflight()
            if not ready["ready"]:
                raise RuntimeError(ready["error"])
            _json_write(
                job_dir / "execution.json",
                {
                    "image_id": ready["image_id"],
                    "limits": self.settings,
                    "host_python": sys.version,
                    "cached_only": cached_only,
                    "started_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            progress("准备真实行情、公司行为、财报及基准数据")
            command = [
                sys.executable,
                "-B",
                "-m",
                "src.interactive.assistant.research_worker",
                "prepare",
                "--job-dir",
                str(job_dir),
                "--project-root",
                str(self.project_root),
                "--parent-pid",
                str(os.getpid()),
            ]
            if cached_only:
                command.append("--cached-only")
            exit_code = self._run_process(
                command,
                proposal,
                cancel_event,
                timeout=self.settings["prepare_timeout_seconds"],
                log_name="prepare.log",
                cwd=job_dir / "input/code",
                protect_host_memory=True,
            )
            if exit_code:
                raise RuntimeError(
                    "数据准备失败；详见 data_readiness.json 和 prepare.log"
                )
            manifest_path = _safe_path(
                job_dir / "input/data_manifest.json", job_dir, regular=True
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not manifest.get("ready") or not manifest.get("files"):
                raise RuntimeError("数据准备未返回可验证的就绪清单")
            for name, digest in manifest["files"].items():
                if (
                    _sha(
                        _safe_path(
                            job_dir / "input/data" / name,
                            job_dir / "input/data",
                            regular=True,
                        )
                    )
                    != digest
                ):
                    raise RuntimeError("冻结数据哈希校验失败")
            # The only additions allowed since approval are trusted data outputs.
            for name, digest in proposal["input_hashes"].items():
                if (
                    _sha(_safe_path(job_dir / "input" / name, job_dir, regular=True))
                    != digest
                ):
                    raise RuntimeError("批准的脚本或配置快照发生变化")
            self._make_inputs_readable(job_dir / "input")
            progress("数据就绪，正在离线容器内运行研究验证")
            run_deadline = time.monotonic() + self.settings["timeout_seconds"]
            exit_code = self._run_process(
                self.docker_command(
                    proposal, docker=ready["docker"], image_id=ready["image_id"]
                ),
                proposal,
                cancel_event,
                timeout=self.settings["timeout_seconds"] + 15,
                log_name="run.log",
            )
            if exit_code:
                raise RuntimeError(f"研究容器退出码 {exit_code}；详见 run.log")
            report = _safe_path(job_dir / "output/report.json", job_dir, regular=True)
            if report.stat().st_size > self.settings["max_output_mb"] * 1024 * 1024:
                raise ValueError("研究报告超过配置限额")
            evidence = json.loads(report.read_text(encoding="utf-8"))
            if (
                evidence.get("contract") != "feishu-research-shared-execution/1"
                or evidence.get("job_id") != job_id
                or not isinstance(evidence.get("metrics"), dict)
            ):
                raise ValueError("研究容器没有返回匹配任务的共享执行报告")
            if proposal["spec"]["script_id"] == "generated":
                # An experiment cannot replace trusted baseline evidence or
                # forge the metrics subsequently summarized by the host.
                baseline_dir = job_dir / "input/baseline"
                baseline_dir.mkdir()
                shutil.copyfile(report, baseline_dir / "report.json")
                self._make_inputs_readable(job_dir / "input")
                custom_dir = job_dir / "output/custom"
                custom_dir.mkdir()
                if os.name != "nt":
                    custom_dir.chmod(0o777)
                remaining = int(run_deadline - time.monotonic())
                if remaining <= 0:
                    raise TimeoutError("研究阶段已耗尽执行时限")
                progress("运行已批准的自定义 Python，基准证据只读保留")
                exit_code = self._run_process(
                    self.docker_command(
                        proposal,
                        docker=ready["docker"],
                        image_id=ready["image_id"],
                        custom=True,
                        timeout=remaining,
                    ),
                    proposal,
                    cancel_event,
                    timeout=remaining + 15,
                    log_name="custom.log",
                )
                if exit_code:
                    raise RuntimeError(
                        f"自定义 Python 退出码 {exit_code}；详见 custom.log"
                    )
            status = "completed"
            summary = "研究执行完成（范围见预览；正式策略验收仍需全量验证）"
            metrics = evidence.get("metrics", {})
            if isinstance(metrics, dict):
                summary += "\n" + json.dumps(metrics, ensure_ascii=False)[:2000]
        except InterruptedError as exc:
            status, summary = "cancelled", str(exc)
        except PreparationMemoryError as exc:
            summary = str(exc)
            errors.append(summary)
            _json_write(
                job_dir / "data_readiness.json",
                {
                    "ready": False,
                    "phase": "preparing",
                    "issues": [{"source": "host_memory_monitor", "reason": summary}],
                    "resource_report": "prepare_resources.json",
                },
            )
        except Exception as exc:  # noqa: BLE001 - persist a terminal job receipt
            summary = str(exc)[:2000]
            errors.append(summary)
        finally:
            preparation_cache = job_dir / "preparation_cache"
            if preparation_cache.exists():
                try:
                    _safe_path(preparation_cache, job_dir)
                    shutil.rmtree(preparation_cache)
                except (OSError, ValueError) as exc:
                    logger.warning(
                        "Could not remove private preparation cache for %s: %s",
                        job_id,
                        type(exc).__name__,
                    )
            self.cleanup_recovered(proposal)
            with self._lock:
                self._events.pop(job_id, None)
        try:
            artifacts = self.safe_artifacts(proposal)
        except (OSError, ValueError) as exc:
            status, artifacts = "failed", []
            errors.append(str(exc))
            summary = "研究产物路径或文件类型校验失败"
        result = JobResult(
            job_id=job_id,
            status=status,
            summary=summary,
            exit_code=exit_code,
            errors=errors,
            artifacts=artifacts,
        ).dict()
        result["artifact_hashes"] = {
            str(Path(path).relative_to(job_dir)): _sha(Path(path)) for path in artifacts
        }
        result["proposal_sha256"] = self._proposal_digest(proposal)
        _json_write(job_dir / "result.json", result)
        return result
