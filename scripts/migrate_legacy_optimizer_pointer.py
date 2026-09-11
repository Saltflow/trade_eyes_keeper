#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""将旧 schema v2 的三市场合并活动指针迁移为 v4 独立市场索引。

背景：架构升级为「每市场一个 run + v4 活动索引」后，加载器不再接受
schema_version 2 的 latest_strategy.yaml / 运行 manifest，导致：
  * 活动策略不可加载（日报/简报 0 策略告警）
  * 参考持仓绑定校验失败（load_strategy_run 返回 None）→ 三个资金池全部停单
本脚本把旧的已激活运行原地升级为 v4 格式，运行本身、参数、执行合同、参考
持仓绑定全部保留；不会重新评估 Gate，也不会修改参考持仓文件。迁移属于
显式运维动作，任何调度器都不会自动调用。

用法（在仓库根目录执行）：
    python3 scripts/migrate_legacy_optimizer_pointer.py --dry-run
    python3 scripts/migrate_legacy_optimizer_pointer.py --apply
"""

from __future__ import annotations

import copy
import shutil
import sys
from datetime import datetime
from pathlib import Path

import yaml

LEGACY_SCHEMA = 2
V4_SCHEMA = 4
MARKET_GROUPS = ("a_share", "hk", "us")


def _repo_root() -> Path:
    here = Path(__file__).resolve().parent
    if (here / "main.py").is_file() and (here / "src").is_dir():
        return here
    cwd = Path.cwd()
    if (cwd / "main.py").is_file() and (cwd / "src").is_dir():
        return cwd
    raise SystemExit("没有找到仓库根目录（需要 main.py + src/）")


def _log(message: str) -> None:
    print("[migrate] " + message, flush=True)


def _load_yaml(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        raise SystemExit("无法读取 %s: %s" % (path, exc))


def _write_yaml(path: Path, data: dict) -> None:
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _load_application_config(root: Path) -> dict:
    sys.path.insert(0, str(root))
    import main  # type: ignore

    return main.load_config()


def _legacy_market_config_hashes(
    root: Path, app_config: dict, artifact_values: dict[str, dict]
) -> dict[str, str]:
    """以「若按旧策略/求解器在今天重新求解」的同一契约计算 v4 合同哈希。
    沿用当前 walk-forward / execution / benchmark profile 与约束，保证每个
    市场唯一且与构件（artifact）自洽。"""
    from src.search.config import load_market_optimizer_config

    hashes: dict[str, str] = {}
    markets = (app_config.get("optimizer") or {}).get("markets") or {}
    for group in MARKET_GROUPS:
        current_spec = copy.deepcopy(markets.get(group) or {})
        values = artifact_values[group]
        legacy_spec = dict(current_spec)
        legacy_spec.update({
            "strategy": values["strategy_id"],
            "solver_id": values["solver_id"],
            "gate_profile": values["gate_profile"],
        })
        modified = copy.deepcopy(app_config)
        modified.setdefault("optimizer", {}).setdefault("markets", {})[group] = (
            legacy_spec
        )
        resolved = load_market_optimizer_config(
            group, application_config=modified
        )
        hashes[group] = str(resolved.config_hash)
        _log(
            "%s: 旧合同 v4 哈希 %s（strategy=%s solver=%s gate=%s）"
            % (
                group,
                hashes[group][:16],
                legacy_spec["strategy"],
                legacy_spec["solver_id"],
                legacy_spec["gate_profile"],
            )
        )
    return hashes


def _current_market_configs(root: Path, app_config: dict) -> dict:
    from src.search.config import load_market_optimizer_config

    resolved = {}
    for group in MARKET_GROUPS:
        resolved[group] = load_market_optimizer_config(
            group, application_config=app_config
        )
    return resolved


def _check_binding(
    root: Path,
    app_config: dict,
    market_configs: dict,
    run_id: str,
    artifacts: dict[str, dict],
) -> bool:
    """复现 run_brief_report 的绑定校验，报告每个条件。"""
    from src.search.artifacts import load_strategy_run
    from src.search.contracts import stable_hash
    from src.core.ref_portfolio import reference_execution_contract

    ok = True
    for group in MARKET_GROUPS:
        pf_file = "ref_portfolio_a.yaml" if group == "a_share" else "ref_portfolio_%s.yaml" % group
        pf_path = root / "data" / pf_file
        if not pf_path.is_file():
            _log("%s: 无参考持仓文件，跳过" % group)
            continue
        pf = _load_yaml(pf_path).get("ref_portfolio", {})
        pinned = load_strategy_run(run_id, groups=(group,))
        if pinned is None:
            _log("%s: load_strategy_run 仍返回 None → 绑定失败" % group)
            ok = False
            continue
        strategy = pinned.strategy_for(group)
        params = pinned.params_by_group.get(group)
        if strategy is None or params is None:
            _log("%s: 策略或参数缺失" % group)
            ok = False
            continue
        h1 = stable_hash({"strategy_id": strategy.name, "values": params.values})
        h2 = stable_hash(
            reference_execution_contract(
                params.execution_snapshot,
                market_configs[group].execution,
                group,
            )
        )
        p_ok = h1 == pf.get("params_hash", "")
        e_ok = h2 == pf.get("execution_hash", "")
        s_ok = strategy.name == pf.get("strategy_id")
        _log(
            "%s: 绑定条件 strategy=%s params_hash=%s exec_hash=%s"
            % (group, s_ok, p_ok, e_ok)
        )
        ok = ok and p_ok and e_ok and s_ok
    return ok


def _collect_state(root: Path):
    optimizer_root = root / "data" / "optimizer"
    leader = optimizer_root / "latest_strategy.yaml"
    if not leader.is_file():
        raise SystemExit("未找到活动指针 %s" % leader)
    pointer = _load_yaml(leader)

    if int(pointer.get("schema_version", 0) or 0) == V4_SCHEMA:
        return ("already_v4", pointer, optimizer_root, leader, None, None, {})
    if int(pointer.get("schema_version", 0) or 0) != LEGACY_SCHEMA:
        raise SystemExit(
            "活动指针 schema_version=%s，不是可迁移的 v2 格式"
            % pointer.get("schema_version")
        )

    run_id = str(pointer.get("run_id", "") or "").strip()
    if not run_id or Path(run_id).name != run_id:
        raise SystemExit("活动指针缺少合法 run_id: %r" % (pointer.get("run_id"),))
    run_dir = optimizer_root / "runs" / run_id
    run_manifest_path = run_dir / "manifest.yaml"
    if not run_manifest_path.is_file():
        raise SystemExit("绑定运行不存在: %s" % run_dir)
    run_manifest = _load_yaml(run_manifest_path)
    if int(run_manifest.get("schema_version", 0) or 0) != LEGACY_SCHEMA:
        raise SystemExit(
            "运行 manifest schema_version=%s，不是 v2"
            % run_manifest.get("schema_version")
        )

    entries = pointer.get("groups") or {}
    artifacts: dict[str, dict] = {}
    artifact_paths: dict[str, Path] = {}
    for group in MARKET_GROUPS:
        entry = entries.get(group) if isinstance(entries, dict) else None
        artifact_rel = entry.get("artifact") if isinstance(entry, dict) else None
        if not isinstance(artifact_rel, str) or not artifact_rel.strip():
            raise SystemExit("v2 指针缺少 %s 的 artifact 引用" % group)
        path = (optimizer_root / artifact_rel).resolve()
        try:
            path.relative_to(optimizer_root.resolve())
        except ValueError:
            raise SystemExit("非法 artifact 路径: %s" % artifact_rel)
        if not path.is_file():
            raise SystemExit("artifact 不存在: %s" % path)
        data = _load_yaml(path)
        if int(data.get("schema_version", 0) or 0) != 2:
            raise SystemExit(
                "%s artifact schema_version=%s"
                % (group, data.get("schema_version"))
            )
        for field in ("strategy_id", "solver_id", "gate_profile", "execution"):
            if not data.get(field):
                raise SystemExit("%s artifact 缺少 %s" % (group, field))
        artifacts[group] = data
        artifact_paths[group] = path

    return (
        "legacy_v2",
        pointer,
        optimizer_root,
        leader,
        (run_id, run_dir, run_manifest_path, run_manifest),
        artifact_paths,
        artifacts,
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="实际写入（默认仅校验/干跑）"
    )
    args = parser.parse_args(argv)
    apply = bool(args.apply)

    root = _repo_root()
    (
        status,
        pointer,
        optimizer_root,
        leader,
        run_info,
        artifact_paths,
        artifacts,
    ) = _collect_state(root)

    if status == "already_v4":
        _log("活动指针已是 v4；无需迁移")
        return 0

    run_id, run_dir, run_manifest_path, run_manifest = run_info
    _log("运行: %s" % run_id)
    _log("活动指针 schema_version=2 → 迁移到 v4（三市场同一 run，独立条目）")

    app_config = _load_application_config(root)
    try:
        market_configs = _current_market_configs(root, app_config)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit("当前市场配置无法解析，拒绝迁移: %s" % exc)
    config_hashes = _legacy_market_config_hashes(root, app_config, artifacts)

    for group in MARKET_GROUPS:
        data = artifacts[group]
        _log(
            "%s: artifact %s → strategy=%s solver=%s gate=%s hash=%s"
            % (
                group,
                artifact_paths[group].name,
                data["strategy_id"],
                data["solver_id"],
                data["gate_profile"],
                config_hashes[group][:16],
            )
        )

    entries = {}
    for group in MARKET_GROUPS:
        entries[group] = {
            "group": group,
            "run_id": run_id,
            "artifact": "runs/%s/%s" % (run_id, artifact_paths[group].name),
            "strategy": artifacts[group]["strategy_id"],
            "solver_id": artifacts[group]["solver_id"],
            "gate_profile": artifacts[group]["gate_profile"],
            "config_hash": config_hashes[group],
        }

    new_run_manifest = dict(run_manifest)
    new_run_manifest.update({
        "schema_version": V4_SCHEMA,
        "run_id": run_id,
        "groups": entries,
    })
    new_pointer = dict(pointer)
    new_pointer.update({
        "schema_version": V4_SCHEMA,
        "run_id": run_id,
        "timestamp": datetime.now().isoformat(),
        "groups": entries,
    })

    if not apply:
        _log("干跑模式：未写入任何文件。检查通过：")
        _log("  * 活动指针 → v4（%d 个市场条目）" % len(entries))
        _log("  * 运行 manifest → v4（%s）" % run_id)
        _log("  * 三个 artifact 注入 market_config_hash（与条目 config_hash 一致）")
        return 0

    # ── 备份 ──
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    backup = optimizer_root / "migrations" / ("%s_legacy_v2_to_v4" % stamp)
    backup.mkdir(parents=True, exist_ok=True)
    shutil.copy2(leader, backup / "latest_strategy.yaml.bak")
    shutil.copy2(run_manifest_path, backup / "run_manifest.yaml.bak")
    for group in MARKET_GROUPS:
        shutil.copy2(
            artifact_paths[group],
            backup / ("%s_best_params.yaml.bak" % group),
        )
    _log("备份: %s" % backup)

    # ── 写入 ──
    for group in MARKET_GROUPS:
        data = copy.deepcopy(artifacts[group])
        data["market_config_hash"] = config_hashes[group]
        _write_yaml(artifact_paths[group], data)
    _write_yaml(run_manifest_path, new_run_manifest)
    _write_yaml(leader, new_pointer)
    _log("已写入: 3 个 artifact + 运行 manifest + 活动指针")

    # ── 迁移后验证 ──
    from src.search.artifacts import load_latest_strategy_run

    active = load_latest_strategy_run(groups=MARKET_GROUPS)
    if active is None:
        _log("!! 迁移后 load_latest_strategy_run 仍为 None")
        return 1
    _log("活动策略加载成功: run_id=%s strategy=%s" % (active.run_id, active.strategy_name))
    binding_ok = _check_binding(root, app_config, market_configs, run_id, artifacts)
    if not binding_ok:
        _log("!! 参考持仓绑定校验仍有失败项")
        return 1
    _log("参考持仓绑定校验全部通过（strategy / params_hash / exec_hash 一致）")
    _log("迁移完成。下一次简报/日报将按 v4 活动策略继续运行，参考持仓恢复交易。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
