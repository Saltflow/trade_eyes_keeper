"""Read-only historical strategy artifacts and exact active-run resolution.

The former storage path and metadata contracts remain for manual backtests,
reference holdings and research. This module never publishes or activates a run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..strategy import Params, get_strategy

logger = logging.getLogger(__name__)
OPTIMIZER_ROOT = Path("data/optimizer")
RUNS_DIRNAME = "runs"
LATEST_MANIFEST = "latest_strategy.yaml"
ACTIVE_SCHEMA_VERSION = 4
# Pre-v4 pointers name one artifact per market and declare no solver, Gate
# Profile, or market-config hash.  They stay readable so an installation whose
# last search predates that contract is not silently left without a strategy.
LEGACY_SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = (LEGACY_SCHEMA_VERSION, ACTIVE_SCHEMA_VERSION)
MARKET_GROUPS = ("a_share", "hk", "us")


@dataclass
class ActiveStrategyRun:
    """A frozen historical strategy run for explicit backtests and reference reads."""

    strategy_name: str
    timestamp: str
    params_by_group: dict[str, Params]
    run_id: str = ""
    validation_by_group: dict[str, dict[str, str]] = field(default_factory=dict)
    selection_by_group: dict[str, dict[str, object]] = field(default_factory=dict)
    strategy_by_group: dict[str, str] = field(default_factory=dict)
    solver_by_group: dict[str, str] = field(default_factory=dict)
    config_hash_by_group: dict[str, str] = field(default_factory=dict)
    run_ids_by_group: dict[str, str] = field(default_factory=dict)
    execution_compatibility_by_group: dict[str, dict[str, object]] = field(
        default_factory=dict
    )

    @property
    def strategy(self):
        """Return a strategy only when this view contains one market."""
        if len(self.strategy_by_group) != 1:
            return None
        return get_strategy(next(iter(self.strategy_by_group.values())))

    def strategy_for(self, group: str):
        """Return the strategy pinned to one market group."""
        name = self.strategy_by_group.get(group)
        if not name:
            return None
        return get_strategy(name)

    def run_id_for(self, group: str) -> str:
        return self.run_ids_by_group.get(group, "")


def _root(root: Path | str | None = None) -> Path:
    return Path(root) if root is not None else OPTIMIZER_ROOT


def _parse_params(data: dict, strategy_name: str) -> Params | None:
    raw_params = data.get("params")
    if data.get("strategy_id") != strategy_name or not isinstance(raw_params, dict):
        return None
    try:
        values = {
            key: int(value)
            for key, value in raw_params.items()
            if not str(key).startswith("_")
        }
    except (TypeError, ValueError):
        return None
    execution = data.get("execution")
    if not isinstance(execution, dict):
        return None
    return Params(
        values=values,
        _engine=strategy_name,
        execution_snapshot={str(k): v for k, v in execution.items()},
    )


def _selection_diagnostics(data: dict) -> dict[str, object]:
    """Expose immutable search diagnostics to daily/report consumers."""
    search = data.get("search", {})
    search = search if isinstance(search, dict) else {}
    sensitivity = data.get("sensitivity", {})
    sensitivity = sensitivity if isinstance(sensitivity, dict) else {}
    windows: list[dict] = []
    window_sources = [
        ("ranking", "ranking_windows"),
        (
            "purged",
            "purged_windows"
            if data.get("purged_windows") is not None
            else "isolated_windows",
        ),
        ("holdout", "holdout_windows"),
    ]
    for role, key in window_sources:
        rows = data.get(key, [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            item = dict(row)
            item.setdefault("role", role)
            windows.append(item)
    # A new artifact has explicit global indexes. Legacy artifacts still get a
    # deterministic order for rendering, while retaining all original fields.
    windows.sort(key=lambda item: int(item.get("global_index", 0) or 0))
    for index, item in enumerate(windows, 1):
        item.setdefault("global_index", index)
    result = {
        "wf_score": data.get("wf_score"),
        "ranking_diagnostics": (
            dict(search.get("ranking_diagnostics", {}))
            if isinstance(search.get("ranking_diagnostics"), dict)
            else {}
        ),
        "sensitivity": dict(sensitivity),
        "selection_score": search.get("selection_score"),
    }
    if windows or isinstance(data.get("holdout_summary"), dict):
        result.update({
            "holdout_summary": (
                dict(data.get("holdout_summary", {}))
                if isinstance(data.get("holdout_summary"), dict)
                else {}
            ),
            "window_counts": {
                "total": int(
                    search.get("total_window_count", len(windows)) or 0
                ),
                "ranking": int(search.get("ranking_window_count", 0) or 0),
                "purged": int(
                    search.get("purged_overlap_window_count", 0) or 0
                ),
                "holdout": int(search.get("validation_window_count", 0) or 0),
            },
            "windows": windows,
        })
    return result


def _load_yaml(path: Path) -> dict | None:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("Cannot read optimizer artifact %s: %s", path, exc)
        return None
    return value if isinstance(value, dict) else None


def _manifest_schema_version(manifest: dict) -> int:
    """Return one pointer's declared schema version, never raising on junk."""
    try:
        return int(manifest.get("schema_version", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _load_active_manifest(
    root: Path,
    run_id: str | None = None,
) -> tuple[Path, dict] | None:
    """Return an activated pointer that this build knows how to read.

    A v4 index is the current contract.  A legacy pointer is still accepted so
    its markets resolve as legacy entries instead of disappearing.
    """
    if run_id is not None:
        path = root / RUNS_DIRNAME / run_id / "manifest.yaml"
        manifest = _load_yaml(path)
        if (
            not manifest
            or not manifest.get("activated")
            or str(manifest.get("run_id", "")) != run_id
            or _manifest_schema_version(manifest) not in SUPPORTED_SCHEMA_VERSIONS
        ):
            return None
        return path, manifest
    path = root / LATEST_MANIFEST
    manifest = _load_yaml(path)
    if (
        not manifest
        or not manifest.get("activated")
        or _manifest_schema_version(manifest) not in SUPPORTED_SCHEMA_VERSIONS
    ):
        return None
    return path, manifest


def _artifact_path(base: Path, artifact: str) -> Path | None:
    """Resolve a manifest artifact without allowing paths outside its root."""
    try:
        root = base.resolve()
        path = (root / artifact).resolve()
        path.relative_to(root)
    except (OSError, ValueError):
        logger.warning("Invalid optimizer artifact path: %s", artifact)
        return None
    return path


def _manifest_entry(manifest: dict, group: str) -> dict | None:
    """Return a strict v4 market entry, never consulting global fields."""
    if int(manifest.get("schema_version", 0) or 0) != ACTIVE_SCHEMA_VERSION:
        return None
    entries = manifest.get("groups")
    if not isinstance(entries, dict):
        return None
    entry = entries.get(group)
    if not isinstance(entry, dict):
        return None
    required = (
        "run_id",
        "artifact",
        "strategy",
        "solver_id",
        "gate_profile",
        "config_hash",
    )
    if any(not str(entry.get(key, "")).strip() for key in required):
        return None
    if str(entry.get("group", group)) != group:
        return None
    if Path(str(entry["run_id"])).name != str(entry["run_id"]):
        return None
    artifact = Path(str(entry["artifact"]))
    expected_prefix = Path(RUNS_DIRNAME) / str(entry["run_id"])
    if len(artifact.parts) < 3 or Path(*artifact.parts[:2]) != expected_prefix:
        return None
    strategy = str(entry["strategy"])
    if get_strategy(strategy) is None or strategy == "mixed":
        return None
    return entry


def _artifact_matches_entry(data: dict, entry: dict, group: str) -> bool:
    """Require the immutable artifact to repeat the active market contract."""
    if not isinstance(data, dict) or int(data.get("schema_version", 0) or 0) != 2:
        return False
    if str(data.get("group", "")) != group:
        return False
    if str(data.get("strategy_id", "")) != str(entry.get("strategy", "")):
        return False
    if str(data.get("solver_id", "")) != str(entry.get("solver_id", "")):
        return False
    if str(data.get("gate_profile", "")) != str(entry.get("gate_profile", "")):
        return False
    if not isinstance(data.get("execution"), dict):
        return False
    return str(data.get("market_config_hash", "")) == str(
        entry.get("config_hash", "")
    )


def _legacy_manifest_entry(manifest: dict, group: str) -> dict | None:
    """Return a market entry that declares no market contract.

    Legacy pointers name one artifact per market under a run-wide strategy, and
    legacy entries carried into a v4 index keep that shape.  Such an entry is
    still tied to an existing run and artifact, so it can be read and carried
    forward without inventing a solver, Gate Profile, or config hash.
    """
    entries = manifest.get("groups")
    if not isinstance(entries, dict):
        return None
    entry = entries.get(group)
    if not isinstance(entry, dict):
        return None
    artifact = str(entry.get("artifact", "")).strip()
    strategy = str(entry.get("strategy") or manifest.get("strategy") or "").strip()
    run_id = str(entry.get("run_id") or manifest.get("run_id") or "").strip()
    if not artifact or not strategy or not run_id:
        return None
    if str(entry.get("group", group)) != group:
        return None
    if Path(run_id).name != run_id:
        return None
    if get_strategy(strategy) is None or strategy == "mixed":
        return None
    parts = Path(artifact).parts
    if len(parts) < 3 or Path(*parts[:2]) != Path(RUNS_DIRNAME) / run_id:
        return None
    return {
        "group": group,
        "run_id": run_id,
        "artifact": artifact,
        "strategy": strategy,
        "solver_id": str(entry.get("solver_id", "")),
        "gate_profile": str(entry.get("gate_profile", "")),
        "config_hash": str(entry.get("config_hash", "")),
        "legacy": True,
    }


def _artifact_matches_legacy_entry(data: dict, entry: dict, group: str) -> bool:
    """Require a legacy artifact to agree on the market and the strategy only."""
    if not isinstance(data, dict) or int(data.get("schema_version", 0) or 0) != 2:
        return False
    if str(data.get("group", "")) != group:
        return False
    if str(data.get("strategy_id", "")) != str(entry.get("strategy", "")):
        return False
    return isinstance(data.get("execution"), dict)


def execution_contract_compatibility(data: dict, group: str) -> dict[str, object]:
    """Compare recorded cash semantics with the runtime without rewriting history.

    The versioned change introduces HK declared-currency conversion. Existing
    A-share equity/fund providers emit CNY cash and US providers emit USD cash;
    their native-currency calculations are unchanged by this HK-only migration.
    This explicit legacy policy does not certify any other contract or stamp a
    new execution version on historical artifacts.
    """
    from ..data.corporate_action_currency import CORPORATE_ACTION_CURRENCY_CONTRACT

    contracts = data.get("execution_contracts", {})
    malformed = not isinstance(contracts, dict)
    recorded = contracts.get("currency_cash_version") if not malformed else None
    explicit = malformed or "currency_cash_version" in contracts
    result: dict[str, object] = {
        "status": "incompatible_execution_contract",
        "compatible": False,
        "recorded_currency_cash_version": recorded,
        "runtime_currency_cash_version": CORPORATE_ACTION_CURRENCY_CONTRACT,
        "requires_reoptimization": True,
    }
    if explicit and recorded == CORPORATE_ACTION_CURRENCY_CONTRACT:
        result.update(
            status="compatible",
            compatible=True,
            requires_reoptimization=False,
            reason="",
        )
    elif group in {"a_share", "us"} and (
        not explicit or recorded == "declared-cash+causal-hkma-valuation/1"
    ):
        result.update(
            status="legacy_native_currency_compatible",
            compatible=True,
            requires_reoptimization=False,
            reason="旧产物保留原执行版本；A股CNY/美股USD同币种路径未变",
            compatibility_policy="hk_conversion_only_native_cash_unchanged/1",
        )
    else:
        result["reason"] = (
            "港股旧产物缺少现金币种转换合同证据，需要按当前合同重新优化"
            if not explicit and group == "hk"
            else "产物现金币种执行合同与当前运行时不兼容，需要重新优化"
        )
    return result


def load_latest_strategy_run(
    groups: tuple[str, ...] = MARKET_GROUPS,
    root: Path | str | None = None,
    *,
    _manifest_run_id: str | None = None,
    compatibility_diagnostics: dict | None = None,
) -> ActiveStrategyRun | None:
    """Load explicitly activated v4 market entries only.

    A requested market missing from the active pointer is an unconfigured
    market, not permission to use another market or an old artifact.
    """
    base = _root(root)
    found = _load_active_manifest(base, _manifest_run_id)
    if not found:
        return None
    _manifest_path, manifest = found
    requested = tuple(dict.fromkeys(groups))
    if not requested or any(group not in MARKET_GROUPS for group in requested):
        return None
    strategy_by_group: dict[str, str] = {}
    solver_by_group: dict[str, str] = {}
    config_hash_by_group: dict[str, str] = {}
    run_ids_by_group: dict[str, str] = {}
    params_by_group: dict[str, Params] = {}
    validation_by_group: dict[str, dict[str, str]] = {}
    selection_by_group: dict[str, dict[str, object]] = {}
    execution_compatibility_by_group: dict[str, dict[str, object]] = {}
    for group in requested:
        entry = _manifest_entry(manifest, group)
        legacy_entry = entry is None
        if legacy_entry:
            entry = _legacy_manifest_entry(manifest, group)
        if entry is None:
            logger.info("No active optimizer artifact for market %s", group)
            return None
        path = _artifact_path(base, str(entry["artifact"]))
        if path is None:
            return None
        data = _load_yaml(path)
        if legacy_entry:
            matched = _artifact_matches_legacy_entry(data or {}, entry, group)
        else:
            matched = _artifact_matches_entry(data or {}, entry, group)
        if not matched:
            logger.warning(
                "Ignoring artifact whose market contract does not match %s",
                group,
            )
            return None
        compatibility = execution_contract_compatibility(data or {}, group)
        execution_compatibility_by_group[group] = compatibility
        if compatibility_diagnostics is not None:
            compatibility_diagnostics[group] = dict(compatibility)
        if not compatibility["compatible"]:
            logger.warning("%s: %s", group, compatibility["reason"])
            return None
        params = _parse_params(data or {}, str(entry["strategy"]))
        if params is None:
            return None
        strategy_by_group[group] = str(entry["strategy"])
        solver_by_group[group] = str(entry.get("solver_id", ""))
        config_hash_by_group[group] = str(entry.get("config_hash", ""))
        run_ids_by_group[group] = str(entry["run_id"])
        params_by_group[group] = params
        period = (data or {}).get("validation_period", {})
        if isinstance(period, dict):
            validation_by_group[group] = {
                key: str(value)
                for key, value in period.items()
                if key in {"start", "end"} and value
            }
        selection_by_group[group] = _selection_diagnostics(data or {})
    unique_strategies = set(strategy_by_group.values())
    strategy_name = next(iter(unique_strategies)) if len(unique_strategies) == 1 else ""
    unique_run_ids = set(run_ids_by_group.values())
    run_id = next(iter(unique_run_ids)) if len(unique_run_ids) == 1 else ""
    return ActiveStrategyRun(
        strategy_name=strategy_name,
        timestamp=str(manifest.get("timestamp", "")),
        params_by_group=params_by_group,
        run_id=run_id,
        validation_by_group=validation_by_group,
        selection_by_group=selection_by_group,
        strategy_by_group=strategy_by_group,
        solver_by_group=solver_by_group,
        config_hash_by_group=config_hash_by_group,
        run_ids_by_group=run_ids_by_group,
        execution_compatibility_by_group=execution_compatibility_by_group,
    )
def load_strategy_run(
    run_id: str,
    groups: tuple[str, ...] = MARKET_GROUPS,
    root: Path | str | None = None,
) -> ActiveStrategyRun | None:
    """Load one exact activated market run without substituting data."""
    if not run_id or Path(run_id).name != run_id:
        return None
    return load_latest_strategy_run(
        groups,
        root,
        _manifest_run_id=run_id,
    )
