"""Versioned optimizer artifacts and the active-strategy resolver.

An optimizer run is only made active after every configured market has produced
an artifact.  Daily reports, brief reports and interactive backtests all use
this module, so they cannot accidentally combine the strategy from one run
with the parameters from another.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from .search_interface import Params
from .strategies import get_strategy


logger = logging.getLogger(__name__)
OPTIMIZER_ROOT = Path("data/optimizer")
RUNS_DIRNAME = "runs"
LATEST_MANIFEST = "latest_strategy.yaml"


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


@dataclass
class ActiveStrategyRun:
    """A complete timestamped optimizer run usable by alerts and backtests."""

    strategy_name: str
    timestamp: str
    params_by_group: dict[str, Params]
    run_id: str = ""
    validation_by_group: dict[str, dict[str, str]] = field(default_factory=dict)
    selection_by_group: dict[str, dict[str, object]] = field(default_factory=dict)

    @property
    def strategy(self):
        return get_strategy(self.strategy_name)


def _root(root: Path | str | None = None) -> Path:
    return Path(root) if root is not None else OPTIMIZER_ROOT


def _parse_params(data: dict, strategy_name: str) -> Params | None:
    raw_params = data.get("params")
    artifact_strategy = data.get("strategy_id") or data.get("engine")
    if artifact_strategy != strategy_name or not isinstance(raw_params, dict):
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
        execution = _migrate_legacy_execution(values, strategy_name)
    return Params(
        values=values,
        _engine=strategy_name,
        execution_snapshot={str(k): v for k, v in execution.items()},
    )


def _migrate_legacy_execution(values: dict[str, int], strategy_name: str) -> dict:
    """Translate an old percentage/rule-limit artifact once at load time."""
    from .config import get_constraints

    constraints = get_constraints()
    tiers = constraints.discrete_search
    buy_levels = [float(value) for value in tiers.buy_limit_levels] or [10000.0]
    sell_levels = [float(value) for value in tiers.sell_limit_levels] or [10000.0]
    capital = float(constraints.execution.initial_capital)

    def nearest(levels, amount):
        return min(levels, key=lambda value: (abs(value - amount), value))

    # Percentile artifacts had one discrete fraction.  Builder had one per
    # rule; its legacy executor averaged active fractions.  Simplified used
    # the largest selected rule limit in practice.  Preserve those historic
    # effective values only until the next native cash-tier search.
    fractions = [0.05, 0.15, 0.25, 0.35, 0.45]
    if "position_frac" in values:
        fraction = fractions[int(values["position_frac"]) % len(fractions)]
        buy_amount = sell_amount = capital * fraction
    elif any(key.endswith("_limit") for key in values):
        buy_amount = max(
            [buy_levels[int(value) % len(buy_levels)] for key, value in values.items()
             if key.startswith("buy_") and key.endswith("_limit")]
            or [buy_levels[0]]
        )
        sell_amount = max(
            [sell_levels[int(value) % len(sell_levels)] for key, value in values.items()
             if key.startswith("sell_") and key.endswith("_limit")]
            or [sell_levels[0]]
        )
    else:
        buy_fracs = [
            float(value) for key, value in values.items()
            if key.startswith("buy_") and key.endswith("_frac")
        ]
        sell_fracs = [
            float(value) for key, value in values.items()
            if key.startswith("sell_") and key.endswith("_frac")
        ]
        legacy_levels = [0.10, 0.15, 0.20, 0.25, 0.30, 0.40]
        buy_amount = capital * (
            sum(legacy_levels[int(v) % len(legacy_levels)] for v in buy_fracs)
            / max(len(buy_fracs), 1)
        )
        sell_amount = capital * (
            sum(legacy_levels[int(v) % len(legacy_levels)] for v in sell_fracs)
            / max(len(sell_fracs), 1)
        )
    return {
        "model": "cash_cap",
        "buy_cash_limit": nearest(buy_levels, buy_amount),
        "sell_cash_limit": nearest(sell_levels, sell_amount),
        "migration": "legacy_execution_mapped",
        "source_engine": strategy_name,
    }


def _load_yaml(path: Path) -> dict | None:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("Cannot read optimizer artifact %s: %s", path, exc)
        return None
    return value if isinstance(value, dict) else None


def _manifest_candidates(root: Path) -> list[tuple[str, Path, dict]]:
    paths = [root / LATEST_MANIFEST]
    runs_dir = root / RUNS_DIRNAME
    if runs_dir.exists():
        paths.extend(runs_dir.glob("*/manifest.yaml"))

    candidates = []
    seen: set[Path] = set()
    for path in paths:
        try:
            path = path.resolve()
        except OSError:
            continue
        if path in seen or not path.exists():
            continue
        seen.add(path)
        manifest = _load_yaml(path)
        if not manifest or not manifest.get("activated"):
            continue
        timestamp = str(
            manifest.get("activated_at") or manifest.get("timestamp", "")
        )
        if timestamp:
            candidates.append((timestamp, path, manifest))
    return candidates


def _load_active_manifest(root: Path) -> tuple[Path, dict] | None:
    candidates = _manifest_candidates(root)
    if not candidates:
        return None
    _, path, manifest = max(candidates, key=lambda item: item[0])
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


def load_latest_strategy_run(
    groups: tuple[str, ...] = ("a_share", "hk", "us"),
    root: Path | str | None = None,
) -> ActiveStrategyRun | None:
    """Load the newest complete optimizer run, falling back to legacy files.

    Versioned manifests are preferred and sorted by their persisted timestamp,
    rather than trusting a configuration value or file-system modification
    times.  The fallback keeps existing installations functional until their
    next complete optimization run publishes a manifest.
    """
    base = _root(root)
    found = _load_active_manifest(base)
    if found:
        _manifest_path, manifest = found
        strategy_name = str(manifest.get("strategy", ""))
        if get_strategy(strategy_name) is None:
            logger.warning("Newest optimizer run uses unregistered strategy %s", strategy_name)
            return None
        params_by_group: dict[str, Params] = {}
        validation_by_group: dict[str, dict[str, str]] = {}
        selection_by_group: dict[str, dict[str, object]] = {}
        entries = manifest.get("groups", {})
        if not isinstance(entries, dict):
            return None
        for group in groups:
            entry = entries.get(group, {})
            if not isinstance(entry, dict):
                return None
            artifact = entry.get("artifact")
            if not artifact:
                return None
            # Artifacts are always relative to the optimizer root, so the
            # global latest pointer and its run-local manifest resolve to the
            # same immutable file.
            path = _artifact_path(base, str(artifact))
            if path is None:
                return None
            data = _load_yaml(path)
            params = _parse_params(data or {}, strategy_name)
            if params is None:
                logger.warning("Newest optimizer run has invalid %s artifact", group)
                return None
            params_by_group[group] = params
            period = (data or {}).get("validation_period", {})
            if isinstance(period, dict):
                validation_by_group[group] = {
                    key: str(value)
                    for key, value in period.items()
                    if key in {"start", "end"} and value
                }
            search = (data or {}).get("search", {})
            sensitivity = (data or {}).get("sensitivity", {})
            selection_by_group[group] = {
                "wf_score": (data or {}).get("wf_score"),
                "ranking_diagnostics": dict(search)
                .get("ranking_diagnostics", {})
                if isinstance(search, dict)
                and isinstance(search.get("ranking_diagnostics"), dict)
                else {},
                "sensitivity": dict(sensitivity) if isinstance(sensitivity, dict) else {},
                "selection_score": (
                    search.get("selection_score") if isinstance(search, dict) else None
                ),
            }
        return ActiveStrategyRun(
            strategy_name=strategy_name,
            timestamp=str(manifest["timestamp"]),
            params_by_group=params_by_group,
            run_id=str(manifest.get("run_id", "")),
            validation_by_group=validation_by_group,
            selection_by_group=selection_by_group,
        )

    # Compatibility for artifacts written before run manifests existed.  Pick
    # the newest timestamp, then only accept a coherent strategy across every
    # requested market.
    legacy: list[tuple[str, str, dict]] = []
    for group in groups:
        data = _load_yaml(base / f"{group}_best_params.yaml")
        strategy_id = (data or {}).get("strategy_id") or (data or {}).get("engine")
        if data and data.get("timestamp") and strategy_id:
            legacy.append((str(data["timestamp"]), str(strategy_id), data))
    if not legacy:
        return None
    _, strategy_name, _ = max(legacy, key=lambda item: item[0])
    if get_strategy(strategy_name) is None:
        return None
    params_by_group = {}
    validation_by_group: dict[str, dict[str, str]] = {}
    selection_by_group: dict[str, dict[str, object]] = {}
    for group in groups:
        data = _load_yaml(base / f"{group}_best_params.yaml") or {}
        params = _parse_params(data, strategy_name)
        if params is None:
            return None
        params_by_group[group] = params
        period = data.get("validation_period", {})
        if isinstance(period, dict):
            validation_by_group[group] = {
                key: str(value)
                for key, value in period.items()
                if key in {"start", "end"} and value
            }
        search = data.get("search", {})
        sensitivity = data.get("sensitivity", {})
        selection_by_group[group] = {
            "wf_score": data.get("wf_score"),
            "ranking_diagnostics": dict(search).get("ranking_diagnostics", {})
            if isinstance(search, dict)
            and isinstance(search.get("ranking_diagnostics"), dict)
            else {},
            "sensitivity": dict(sensitivity) if isinstance(sensitivity, dict) else {},
            "selection_score": search.get("selection_score")
            if isinstance(search, dict)
            else None,
        }
    timestamp = max(item[0] for item in legacy)
    return ActiveStrategyRun(
        strategy_name,
        timestamp,
        params_by_group,
        "legacy",
        validation_by_group,
        selection_by_group,
    )


def publish_complete_run(
    run_id: str,
    strategy_name: str,
    timestamp: str,
    groups: dict[str, OptimizerGroupSummary],
    required_groups: tuple[str, ...] = ("a_share", "hk", "us"),
    all_groups: tuple[str, ...] = ("a_share", "hk", "us"),
    root: Path | str | None = None,
    activate: bool = True,
) -> bool:
    """Persist a complete run and optionally make it active atomically.

    A default A-share-only optimization must not make the active resolver lose
    the most recently validated HK/US parameters.  Partial publication is
    therefore allowed only when a compatible active manifest supplies every
    untouched market artifact.
    """
    base = _root(root)
    run_dir = base / RUNS_DIRNAME / run_id
    entries: dict[str, dict[str, str]] = {}
    artifacts_eligible = True
    for group in required_groups:
        summary = groups.get(group)
        if summary is None or summary.status != "completed" or not summary.artifact:
            return False
        artifact_path = run_dir / summary.artifact
        data = _load_yaml(artifact_path)
        if _parse_params(data or {}, strategy_name) is None:
            return False
        activation = (data or {}).get("activation", {})
        if isinstance(activation, dict) and "eligible" in activation:
            artifacts_eligible &= bool(activation.get("eligible"))
        entries[group] = {
            "artifact": (Path(RUNS_DIRNAME) / run_id / summary.artifact).as_posix()
        }

    if set(required_groups) != set(all_groups):
        found = _load_active_manifest(base)
        if found is None:
            logger.warning("Cannot partially publish without an active full strategy")
            return False
        _, previous = found
        if str(previous.get("strategy", "")) != strategy_name:
            logger.warning("Cannot merge partial run across strategy engines")
            return False
        previous_entries = previous.get("groups", {})
        if not isinstance(previous_entries, dict):
            return False
        for group in all_groups:
            if group in entries:
                continue
            previous_entry = previous_entries.get(group)
            if not isinstance(previous_entry, dict):
                return False
            artifact = previous_entry.get("artifact")
            if not artifact:
                return False
            artifact_path = _artifact_path(base, str(artifact))
            if artifact_path is None:
                return False
            data = _load_yaml(artifact_path)
            if _parse_params(data or {}, strategy_name) is None:
                return False
            entries[group] = {"artifact": str(artifact)}

    manifest = {
        "schema_version": 2,
        "run_id": run_id,
        "strategy": strategy_name,
        "timestamp": timestamp,
        "activated": bool(activate),
        "candidate": not bool(activate),
        "activation_eligible": bool(artifacts_eligible),
        "groups": entries,
    }
    if activate:
        manifest["activated_at"] = datetime.now().isoformat()
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = run_dir / "manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8")

    if not activate:
        return True

    # ``replace`` makes the global pointer atomic on the same filesystem.
    base.mkdir(parents=True, exist_ok=True)
    tmp_path = base / f".{LATEST_MANIFEST}.tmp"
    tmp_path.write_text(yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8")
    tmp_path.replace(base / LATEST_MANIFEST)

    # Keep the historical filenames for external scripts; alerts never read
    # them once a manifest is available.
    for group, entry in entries.items():
        shutil.copy2(base / entry["artifact"], base / f"{group}_best_params.yaml")
    return True


def activate_run(
    run_id: str,
    groups: tuple[str, ...] = ("a_share", "hk", "us"),
    root: Path | str | None = None,
) -> bool:
    """Atomically activate one complete, registered, holdout-passed candidate."""
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
    strategy_name = str(manifest.get("strategy", ""))
    if get_strategy(strategy_name) is None:
        logger.warning("Candidate uses unregistered strategy %s", strategy_name)
        return False
    if not manifest.get("activation_eligible"):
        logger.warning("Candidate %s did not pass activation gates", run_id)
        return False
    entries = manifest.get("groups", {})
    if not isinstance(entries, dict) or set(groups) - set(entries):
        logger.warning("Candidate %s is not complete for %s", run_id, groups)
        return False
    for group in groups:
        entry = entries.get(group, {})
        artifact = entry.get("artifact") if isinstance(entry, dict) else None
        path = _artifact_path(base, str(artifact)) if artifact else None
        data = _load_yaml(path) if path is not None else None
        if _parse_params(data or {}, strategy_name) is None:
            return False
        activation = (data or {}).get("activation", {})
        holdout = (data or {}).get("holdout_windows", [])
        if (
            not isinstance(activation, dict)
            or not activation.get("eligible")
            or not activation.get("holdout_passed")
            or not isinstance(holdout, list)
            or len(holdout) != 1
            or float(holdout[0].get("excess_return", 0.0)) <= 0.0
        ):
            logger.warning("%s candidate artifact failed holdout checks", group)
            return False

    activated = dict(manifest)
    activated["activated"] = True
    activated["candidate"] = False
    activated["activated_at"] = datetime.now().isoformat()
    base.mkdir(parents=True, exist_ok=True)
    tmp_path = base / f".{LATEST_MANIFEST}.tmp"
    tmp_path.write_text(
        yaml.safe_dump(activated, allow_unicode=True), encoding="utf-8"
    )
    # The active pointer is the commit point.  Until this same-filesystem
    # replace succeeds, every reader continues using the previous run.
    tmp_path.replace(base / LATEST_MANIFEST)
    try:
        manifest_path.write_text(
            yaml.safe_dump(activated, allow_unicode=True), encoding="utf-8"
        )
        for group in groups:
            artifact = activated["groups"][group]["artifact"]
            shutil.copy2(base / artifact, base / f"{group}_best_params.yaml")
    except OSError:
        # The pointer replacement above is the authoritative atomic commit.
        # Legacy file synchronization is best-effort and must not turn a
        # completed activation into a misleading failure response.
        logger.exception(
            "Candidate %s activated, but legacy artifact sync failed", run_id
        )
    return True


def persist_group_summary(
    run_id: str,
    summary: OptimizerGroupSummary,
    root: Path | str | None = None,
) -> None:
    """Store notification metadata beside an immutable parameter artifact.

    The active-parameter resolver intentionally only consumes ``engine`` and
    ``params``.  Search and validation metadata lives in the same versioned
    artifact so a later resend can reproduce the optimizer report without
    guessing from the final GA population size.
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


def new_run_id(strategy_name: str, now: datetime | None = None) -> str:
    now = now or datetime.now()
    return f"{now.strftime('%Y%m%dT%H%M%S%f')}_{strategy_name}"
