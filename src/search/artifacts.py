"""Versioned optimizer artifacts and the active-strategy resolver.

Optimizer artifacts are immutable and the active manifest is the single source
of truth. Every optimizer run belongs to exactly one market. The active
manifest is only an index of independently activated market artifacts; it
never supplies a global strategy or fallback.
"""

from __future__ import annotations

import logging
import math
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

import yaml

from ..strategy import Params
from ..strategy import get_strategy


logger = logging.getLogger(__name__)
OPTIMIZER_ROOT = Path("data/optimizer")
RUNS_DIRNAME = "runs"
LATEST_MANIFEST = "latest_strategy.yaml"
ACTIVE_SCHEMA_VERSION = 4
MARKET_GROUPS = ("a_share", "hk", "us")


def as_yaml_primitives(value):
    """Recursively convert NumPy/Pandas scalar values before YAML persistence."""
    if isinstance(value, dict):
        return {str(key): as_yaml_primitives(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [as_yaml_primitives(item) for item in value]
    if type(value) in (str, int, float, bool) or value is None:
        return value
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return as_yaml_primitives(item())
        except (TypeError, ValueError):
            pass
    return str(value)


@dataclass
class OptimizerGroupSummary:
    """One market's result in an optimizer notification."""

    group: str
    # ``candidate_count`` is retained for compatibility with previous report
    # objects.  It is the final retained population, not the total search
    # budget; the two values must never be presented as the same metric.
    candidate_count: int = 0
    evaluated_count: int = 0
    survivor_count: int = 0
    wf_score: float | None = None
    params: dict[str, object] = field(default_factory=dict)
    execution: dict[str, object] = field(default_factory=dict)
    ranking_window_count: int = 0
    validation_window_count: int = 0
    purged_window_count: int = 0
    ranking_diagnostics: dict[str, object] = field(default_factory=dict)
    sensitivity: dict[str, object] = field(default_factory=dict)
    validation: dict[str, object] = field(default_factory=dict)
    activation: dict[str, object] = field(default_factory=dict)
    status: str = "not_run"
    artifact: str | None = None
    strategy_name: str = ""
    strategy_label: str = ""
    solver_id: str = ""
    gate_profile: str = ""
    walk_forward_profile: str = ""
    execution_profile: str = ""
    benchmark_profile: str = ""
    market_config_hash: str = ""
    run_id: str = ""


@dataclass
class OptimizerRunSummary:
    """Channel-neutral summary sent after every optimizer invocation."""

    strategy_name: str
    strategy_label: str
    timestamp: str
    elapsed_seconds: float
    groups: dict[str, OptimizerGroupSummary]
    activated: bool = False
    run_id: str = ""
    candidate: bool = False
    status: str = "completed"
    failure_reason: str = ""
    strategy_by_group: dict[str, str] = field(default_factory=dict)
    run_ids_by_group: dict[str, str] = field(default_factory=dict)


@dataclass
class ActiveStrategyRun:
    """A complete timestamped optimizer run usable by alerts and backtests."""

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


@dataclass(frozen=True)
class RunRetentionResult:
    """Summary of one safe optimizer-run lifecycle cleanup."""

    kept_complete: tuple[str, ...] = ()
    protected: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    reclaimed_bytes: int = 0


def _root(root: Path | str | None = None) -> Path:
    return Path(root) if root is not None else OPTIMIZER_ROOT


def _manifest_artifact_run_ids(base: Path, manifest: dict | None) -> set[str]:
    """Return run directories referenced by a manifest's immutable artifacts."""
    if not isinstance(manifest, dict):
        return set()
    referenced: set[str] = set()
    run_id = manifest.get("run_id")
    if isinstance(run_id, str) and run_id and Path(run_id).name == run_id:
        referenced.add(run_id)
    groups = manifest.get("groups", {})
    if not isinstance(groups, dict):
        return referenced
    runs_root = (base / RUNS_DIRNAME).resolve()
    for entry in groups.values():
        artifact = entry.get("artifact") if isinstance(entry, dict) else None
        if not isinstance(artifact, str) or not artifact:
            continue
        try:
            relative = (base / artifact).resolve().relative_to(runs_root)
        except (OSError, ValueError):
            continue
        if len(relative.parts) >= 2:
            referenced.add(relative.parts[0])
    return referenced


def _complete_run_manifest(base: Path, run_dir: Path) -> dict | None:
    """Return a run manifest only when every declared artifact still exists."""
    manifest_path = run_dir / "manifest.yaml"
    if not manifest_path.is_file():
        return None
    manifest = _load_yaml(manifest_path)
    if not manifest or str(manifest.get("run_id", "")) != run_dir.name:
        return None
    if int(manifest.get("schema_version", 0) or 0) != ACTIVE_SCHEMA_VERSION:
        return None
    groups = manifest.get("groups")
    if not isinstance(groups, dict) or not groups:
        return None
    for group in groups:
        entry = _manifest_entry(manifest, group)
        if entry is None:
            return None
        artifact = entry.get("artifact") if isinstance(entry, dict) else None
        if not isinstance(artifact, str) or not artifact:
            return None
        path = _artifact_path(base, artifact)
        if path is None or not path.is_file():
            return None
        if not _artifact_matches_entry(_load_yaml(path) or {}, entry, group):
            return None
    return manifest


def _directory_bytes(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def prune_optimizer_runs(
    *,
    keep_completed: int = 3,
    protected_run_ids: Iterable[str] = (),
    root: Path | str | None = None,
) -> RunRetentionResult:
    """Keep recent complete searches and remove stale or partial run directories.

    The newest configured number of valid manifests are retained. The active
    pointer and every run directory referenced by it are protected in addition
    to that limit, so a historical active strategy remains usable until a new
    candidate is explicitly activated. Callers may also protect the ID of an
    in-flight run. Only direct, non-symlink children of the runs directory are
    removed.
    """
    if keep_completed < 1:
        raise ValueError("keep_completed must be at least 1")

    base = _root(root).resolve()
    runs_root = (base / RUNS_DIRNAME).resolve()
    try:
        runs_root.relative_to(base)
    except ValueError as exc:
        raise ValueError("optimizer runs directory must stay under its root") from exc
    if not runs_root.exists():
        return RunRetentionResult()

    protected = {
        run_id
        for raw in protected_run_ids
        if (run_id := str(raw).strip()) and Path(run_id).name == run_id
    }
    latest_path = base / LATEST_MANIFEST
    active_manifest = _load_yaml(latest_path) if latest_path.is_file() else None
    if latest_path.exists() and active_manifest is None:
        logger.error(
            "Optimizer active pointer is unreadable; skipping run retention cleanup"
        )
        return RunRetentionResult()
    protected.update(_manifest_artifact_run_ids(base, active_manifest))

    completed: list[tuple[str, float, str, Path]] = []
    directories: list[Path] = []
    for item in runs_root.iterdir():
        if not item.is_dir() or item.is_symlink():
            continue
        try:
            item.resolve().relative_to(runs_root)
            modified = item.stat().st_mtime
        except (OSError, ValueError):
            continue
        directories.append(item)
        manifest = _complete_run_manifest(base, item)
        if manifest is not None:
            completed.append(
                (str(manifest.get("timestamp", "")), modified, item.name, item)
            )

    completed.sort(key=lambda value: (value[0], value[1], value[2]), reverse=True)
    recent = {item.name for *_metadata, item in completed[:keep_completed]}
    keep = recent | protected
    removed: list[str] = []
    failed: list[str] = []
    reclaimed = 0
    for item in sorted(directories, key=lambda path: path.name):
        if item.name in keep:
            continue
        item_bytes = _directory_bytes(item)
        try:
            shutil.rmtree(item)
        except OSError as exc:
            logger.warning("Cannot prune optimizer run %s: %s", item, exc)
            failed.append(item.name)
            continue
        reclaimed += item_bytes
        removed.append(item.name)

    result = RunRetentionResult(
        kept_complete=tuple(
            item.name for *_metadata, item in completed[:keep_completed]
        ),
        protected=tuple(sorted(protected)),
        removed=tuple(removed),
        failed=tuple(failed),
        reclaimed_bytes=reclaimed,
    )
    if removed:
        logger.info(
            "Pruned %d optimizer run directories and reclaimed %.2f GiB; "
            "kept recent=%s protected=%s",
            len(removed),
            reclaimed / (1024 ** 3),
            result.kept_complete,
            result.protected,
        )
    return result


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


def _load_active_manifest(
    root: Path,
    run_id: str | None = None,
) -> tuple[Path, dict] | None:
    if run_id is not None:
        path = root / RUNS_DIRNAME / run_id / "manifest.yaml"
        manifest = _load_yaml(path)
        if (
            not manifest
            or not manifest.get("activated")
            or str(manifest.get("run_id", "")) != run_id
            or int(manifest.get("schema_version", 0) or 0) != ACTIVE_SCHEMA_VERSION
        ):
            return None
        return path, manifest
    path = root / LATEST_MANIFEST
    manifest = _load_yaml(path)
    if (
        not manifest
        or not manifest.get("activated")
        or int(manifest.get("schema_version", 0) or 0) != ACTIVE_SCHEMA_VERSION
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


def load_latest_strategy_run(
    groups: tuple[str, ...] = MARKET_GROUPS,
    root: Path | str | None = None,
    *,
    _manifest_run_id: str | None = None,
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
    if int(manifest.get("schema_version", 0) or 0) != ACTIVE_SCHEMA_VERSION:
        logger.warning("Ignoring unsupported optimizer manifest schema")
        return None
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
    for group in requested:
        entry = _manifest_entry(manifest, group)
        if entry is None:
            logger.info("No active optimizer artifact for market %s", group)
            return None
        path = _artifact_path(base, str(entry["artifact"]))
        if path is None:
            return None
        data = _load_yaml(path)
        if not _artifact_matches_entry(data or {}, entry, group):
            logger.warning(
                "Ignoring artifact whose market contract does not match %s",
                group,
            )
            return None
        params = _parse_params(data or {}, str(entry["strategy"]))
        if params is None:
            return None
        strategy_by_group[group] = str(entry["strategy"])
        solver_by_group[group] = str(entry["solver_id"])
        config_hash_by_group[group] = str(entry["config_hash"])
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


def publish_complete_run(
    run_id: str,
    strategy_name: str,
    timestamp: str,
    groups: dict[str, OptimizerGroupSummary],
    required_groups: tuple[str, ...] = ("a_share",),
    all_groups: tuple[str, ...] = MARKET_GROUPS,
    root: Path | str | None = None,
    activate: bool = True,
    strategy_by_group: dict[str, str] | None = None,
) -> bool:
    """Persist exactly one market candidate and optionally activate it."""
    required_groups = tuple(dict.fromkeys(required_groups))
    if len(required_groups) != 1 or required_groups[0] not in MARKET_GROUPS:
        logger.warning("Every optimizer run must contain exactly one market")
        return False
    group = required_groups[0]
    base = _root(root)
    run_dir = base / RUNS_DIRNAME / run_id
    summary = groups.get(group)
    if summary is None or summary.status != "completed" or not summary.artifact:
        return False
    group_strategy = (strategy_by_group or {}).get(group) or summary.strategy_name
    if not group_strategy or group_strategy == "mixed" or get_strategy(group_strategy) is None:
        return False
    artifact_path = run_dir / summary.artifact
    data = _load_yaml(artifact_path)
    if _parse_params(data or {}, group_strategy) is None:
        return False
    search = (data or {}).get("search", {})
    if not isinstance(search, dict):
        search = {}
    solver_id = str(
        summary.solver_id or search.get("solver_id") or (data or {}).get("solver_id", "")
    ).strip()
    config_hash = str(
        summary.market_config_hash
        or (data or {}).get("market_config_hash", "")
        or ((data or {}).get("contracts", {}) or {}).get("market_config_hash", "")
    ).strip()
    if not solver_id or not config_hash:
        logger.warning("%s candidate has no complete market contract", group)
        return False
    gate_profile = str(
        summary.gate_profile or search.get("gate_profile", "")
    ).strip()
    if not gate_profile:
        logger.warning("%s candidate has no Gate Profile", group)
        return False
    if not _artifact_matches_entry(
        data or {},
        {
            "strategy": group_strategy,
            "solver_id": solver_id,
            "gate_profile": gate_profile,
            "config_hash": config_hash,
        },
        group,
    ):
        logger.warning("%s candidate artifact metadata is incomplete", group)
        return False
    entry = {
        "group": group,
        "run_id": run_id,
        "artifact": (Path(RUNS_DIRNAME) / run_id / summary.artifact).as_posix(),
        "strategy": group_strategy,
        "solver_id": solver_id,
        "gate_profile": gate_profile,
        "config_hash": config_hash,
    }
    manifest = {
        "schema_version": ACTIVE_SCHEMA_VERSION,
        "run_id": run_id,
        "market_group": group,
        "timestamp": timestamp,
        "activated": False,
        "candidate": True,
        "groups": {group: entry},
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = run_dir / "manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    if activate:
        return activate_run(run_id, group=group, root=base)
    return True


def activate_run(
    run_id: str,
    group: str | None = None,
    root: Path | str | None = None,
    *,
    groups: tuple[str, ...] | None = None,
) -> bool:
    """Atomically activate one holdout-passed market candidate."""
    if group is None and groups is not None and len(groups) == 1:
        group = groups[0]
    if group not in MARKET_GROUPS:
        logger.warning("Activation requires exactly one market group")
        return False
    base = _root(root)
    if not run_id or Path(run_id).name != run_id:
        logger.warning("Invalid optimizer run id: %s", run_id)
        return False
    run_dir = base / RUNS_DIRNAME / run_id
    manifest_path = run_dir / "manifest.yaml"
    manifest = _load_yaml(manifest_path)
    if not manifest:
        logger.warning("Optimizer candidate does not exist: %s", run_id)
        return False
    if int(manifest.get("schema_version", 0) or 0) != ACTIVE_SCHEMA_VERSION:
        logger.warning("Candidate %s uses an unsupported manifest schema", run_id)
        return False
    if str(manifest.get("market_group", "")) != group:
        logger.warning("Candidate %s does not belong to %s", run_id, group)
        return False
    entry = (manifest.get("groups", {}) or {}).get(group, {})
    if not isinstance(entry, dict) or _manifest_entry(manifest, group) is None:
        return False
    artifact = entry.get("artifact")
    path = _artifact_path(base, str(artifact)) if artifact else None
    data = _load_yaml(path) if path is not None else None
    group_strategy = str(entry["strategy"])
    if not _artifact_matches_entry(data or {}, entry, group):
        logger.warning("%s candidate artifact does not match its market entry", group)
        return False
    if _parse_params(data or {}, group_strategy) is None:
        return False
    activation = (data or {}).get("activation", {})
    holdout = (data or {}).get("holdout_windows", [])
    holdout_excesses: list[float] = []
    if isinstance(holdout, list):
        for window in holdout:
            if not isinstance(window, dict):
                holdout_excesses = []
                break
            raw_excess = window.get(
                "majority_benchmark_excess", window.get("excess_return")
            )
            try:
                excess = float(raw_excess)
            except (TypeError, ValueError):
                holdout_excesses = []
                break
            if not math.isfinite(excess):
                holdout_excesses = []
                break
            holdout_excesses.append(excess)
    if (
        not isinstance(activation, dict)
        or not activation.get("eligible")
        or not activation.get("holdout_passed")
        or not holdout_excesses
        or not all(excess > 0.0 for excess in holdout_excesses)
    ):
        logger.warning(
            "%s candidate artifact failed holdout checks (%d windows)",
            group,
            len(holdout_excesses),
        )
        return False

    current = _load_active_manifest(base)
    if current is not None:
        _, current_manifest = current
        if int(current_manifest.get("schema_version", 0) or 0) != ACTIVE_SCHEMA_VERSION:
            logger.warning("Existing active manifest is not a v4 market index")
            return False
        current_entries = current_manifest.get("groups", {})
        if not isinstance(current_entries, dict):
            return False
        activated_entries = {
            key: dict(value)
            for key, value in current_entries.items()
            if isinstance(value, dict)
        }
    else:
        activated_entries = {}
    activated_entry = dict(entry)
    activated_entry["activated_at"] = datetime.now().isoformat()
    activated_entries[group] = activated_entry
    activated = {
        "schema_version": ACTIVE_SCHEMA_VERSION,
        "timestamp": datetime.now().isoformat(),
        "activated_at": datetime.now().isoformat(),
        "activated": True,
        "candidate": False,
        "last_activated_group": group,
        "last_activated_run_id": run_id,
        "groups": activated_entries,
    }
    base.mkdir(parents=True, exist_ok=True)
    tmp_path = base / f".{LATEST_MANIFEST}.tmp"
    tmp_path.write_text(
        yaml.safe_dump(activated, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    tmp_path.replace(base / LATEST_MANIFEST)
    manifest["activated"] = True
    manifest["candidate"] = False
    manifest["activated_at"] = activated["activated_at"]
    manifest_path.write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return True


def persist_group_summary(
    run_id: str,
    summary: OptimizerGroupSummary,
    root: Path | str | None = None,
) -> None:
    """Store notification metadata beside an immutable parameter artifact.

    The active-parameter resolver consumes the immutable market contract and
    ``params``.  Search and validation metadata lives in the same versioned
    artifact so a later resend can reproduce the optimizer report without
    guessing from the final Solver population size.
    """
    if not summary.artifact:
        return
    path = _root(root) / RUNS_DIRNAME / run_id / summary.artifact
    data = _load_yaml(path)
    if data is None:
        return
    search = data.get("search", {})
    if not isinstance(search, dict):
        search = {}
    search.update({
        "evaluated_count": int(summary.evaluated_count),
        "survivor_count": int(summary.survivor_count or summary.candidate_count),
        "ranking_window_count": int(summary.ranking_window_count),
        "validation_window_count": int(summary.validation_window_count),
        "purged_overlap_window_count": int(summary.purged_window_count),
    })
    if summary.ranking_diagnostics:
        search["ranking_diagnostics"] = summary.ranking_diagnostics
    data["search"] = search
    if summary.sensitivity:
        data["sensitivity"] = summary.sensitivity
    if summary.validation:
        data["validation"] = summary.validation
    path.write_text(
        yaml.safe_dump(as_yaml_primitives(data), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def new_run_id(
    strategy_name: str,
    now: datetime | None = None,
    group: str | None = None,
) -> str:
    now = now or datetime.now()
    suffix = f"_{group}" if group else ""
    return f"{now.strftime('%Y%m%dT%H%M%S%f')}_{strategy_name}{suffix}"
