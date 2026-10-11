"""A failed market must recover when a later market repairs shared evidence."""

import json
from types import SimpleNamespace

import pandas as pd
import pytest

import main
from src.data.market_history import PointInTimeMarketStore, PriceHistoryBundle
from src.search.artifacts import OptimizerGroupSummary, OptimizerRunSummary


def save_bundle(root, code, end="2026-10-09"):
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-10-08", end]),
            "volume": [100, 100],
            "tradable": [True, True],
        }
    )
    for prefix in ("raw", "qfq"):
        for field in ("open", "high", "low", "close"):
            frame[f"{prefix}_{field}"] = [100.0, 100.0]
    frame["qfq_factor"] = 1.0
    PointInTimeMarketStore(root).write(
        PriceHistoryBundle(
            code=code,
            prices=frame,
            actions=[],
            currency="USD" if code == "VOO" else "HKD",
            source="fixture_complete_actions",
        )
    )


def scenario(tmp_path, monkeypatch, repaired=True, recovery_raises=False):
    monkeypatch.chdir(tmp_path)
    cfg = {"point_in_time_data": {"output_dir": str(tmp_path / "pit")}}
    calls, notices = [], []
    configs = {g: SimpleNamespace() for g in ["hk", "us"]}
    monkeypatch.setattr(main, "get_market_optimizer_configs", lambda *a, **kw: configs)
    monkeypatch.setattr(main, "_notify_optimizer_run", lambda cfg, r: notices.append(r))
    save_bundle(tmp_path / "pit", "00001")

    def run(cfg, group, market_config, *, report_sink):
        calls.append(group)
        retry = group == "hk" and calls.count("hk") > 1
        if retry and recovery_raises:
            raise RuntimeError("recovery computation failed")
        status = "no_data" if group == "hk" and not retry else "completed"
        rid = f"{group}_{calls.count(group)}"
        item = OptimizerGroupSummary(group=group, status=status)
        if status == "no_data":
            issue = {
                "code": "VOO",
                "category": "fetch_or_validation_failed",
                "reason": "missing metadata",
            }
            item.ranking_diagnostics["data_readiness_issues"] = [issue]
            path = Path("data/optimizer/runs") / rid
            path.mkdir(parents=True, exist_ok=True)
            receipt = {
                "purpose": "optimizer",
                "requested_start": "2026-10-08",
                "requested_end": "2026-10-11",
                "ready_codes": ["00001"],
                "issues": [issue],
                "market_cutoffs": {
                    c: {"effective_end": "2026-10-09"} for c in ["00001", "VOO"]
                },
            }
            (path / "data_readiness.json").write_text(json.dumps(receipt))
        if group == "us" and repaired:
            save_bundle(tmp_path / "pit", "VOO")
        report_sink.append(
            OptimizerRunSummary(
                "test",
                "test",
                "2026-10-11T02:00:00",
                1,
                {group: item},
                run_id=rid,
                status="failed" if status == "no_data" else "completed",
                failure_reason="missing metadata" if status == "no_data" else "",
                run_ids_by_group={group: rid},
            )
        )
        return {group: 1} if status == "completed" else {}

    monkeypatch.setattr(main, "_run_optimization_group", run)
    return cfg, calls, notices


from pathlib import Path


def test_shared_source_recovers_failed_market_without_rerunning_completed_market(
    tmp_path, monkeypatch
):
    cfg, calls, notices = scenario(tmp_path, monkeypatch)
    assert main.run_optimization(cfg, target_groups=("hk", "us")) == {"hk": 1, "us": 1}
    assert calls == ["hk", "us", "hk"]
    assert notices[-1].status == "completed"
    assert notices[-1].failure_reason == ""
    assert notices[-1].run_ids_by_group["hk"] == "hk_2"
    assert Path("data/optimizer/runs/hk_1/data_readiness.json").exists()
    attempt = json.loads(
        Path("data/optimizer/runs/hk_1/shared_data_recovery.json").read_text()
    )
    assert attempt["status"] == "completed"


def test_unrepaired_cache_does_not_trigger_network_or_rerun(tmp_path, monkeypatch):
    cfg, calls, notices = scenario(tmp_path, monkeypatch, repaired=False)
    assert main.run_optimization(cfg, target_groups=("hk", "us")) == {"us": 1}
    assert calls == ["hk", "us"]
    assert notices[-1].status == "partial"


def test_recovery_exception_is_visible_and_bounded(tmp_path, monkeypatch):
    cfg, calls, notices = scenario(tmp_path, monkeypatch, recovery_raises=True)
    assert main.run_optimization(cfg, target_groups=("hk", "us")) == {"us": 1}
    assert calls == ["hk", "us", "hk"]
    assert "recovery computation failed" in notices[-1].failure_reason
    attempt = json.loads(
        Path("data/optimizer/runs/hk_1/shared_data_recovery.json").read_text()
    )
    assert attempt["status"] == "failed"


def test_restart_does_not_reset_attempt_budget(tmp_path, monkeypatch):
    cfg, calls, _notices = scenario(tmp_path, monkeypatch)
    main.run_optimization(cfg, target_groups=("hk", "us"))
    calls.clear()
    main.run_optimization(cfg, target_groups=("hk", "us"))
    assert calls == ["hk", "us"]


def test_probe_rejects_other_missing_symbol_and_malformed_receipt(tmp_path):
    from src.search.shared_data_recovery import cached_recovery_evidence

    cfg = {"point_in_time_data": {"output_dir": str(tmp_path)}}
    save_bundle(tmp_path, "VOO")
    receipt = {
        "issues": [{"code": "VOO", "category": "fetch_or_validation_failed"}],
        "ready_codes": ["00001"],
        "market_cutoffs": {
            "VOO": {"effective_end": "2026-10-09"},
            "00001": {"effective_end": "2026-10-09"},
        },
        "requested_start": "2026-10-08",
        "requested_end": "2026-10-11",
    }
    with pytest.raises(TypeError):
        cached_recovery_evidence(cfg, receipt)
    receipt["market_cutoffs"].pop("00001")
    with pytest.raises(ValueError, match="full requested universe"):
        cached_recovery_evidence(cfg, receipt)


def test_probe_does_not_reclassify_fundamental_failure_as_market_recovery(tmp_path):
    from src.search.shared_data_recovery import cached_recovery_evidence

    with pytest.raises(ValueError, match="only market data"):
        cached_recovery_evidence(
            {},
            {
                "issues": [
                    {
                        "code": "VOO",
                        "category": "fetch_or_validation_failed",
                        "source": "fundamental",
                    }
                ]
            },
        )
