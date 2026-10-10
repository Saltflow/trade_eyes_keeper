"""Public-source budgets/restart semantics; no production/model network tests."""

import copy
from types import SimpleNamespace

import pytest

from src.fundamental_quant.longitudinal_acquisition import (
    PublicAcquisition,
    acquisition_tasks,
)


def task(year=2022):
    return {
        "key": f"a_share:600039:annual:{year}-12-31",
        "issuer_id": "a_share:600039",
        "company_name": "四川路桥",
        "report_type": "annual",
        "year": year,
        "period_end": f"{year}-12-31",
    }


def test_restart_success_does_not_repeat_source_operation_or_reset_limits(tmp_path):
    calls = []
    source = SimpleNamespace(
        collect=lambda *a: calls.append(a) or {"status": "success"}
    )
    now = [100.0]
    first = PublicAcquisition(
        tmp_path, "test", "2026-10-10", 2, 60, clock=lambda: now[0]
    )
    first.run([task()], source)
    second = PublicAcquisition(
        tmp_path, "test", "2026-10-10", 2, 60, clock=lambda: now[0]
    )
    second.run([task()], source)
    assert len(calls) == 1
    assert second.state["reserved_document_requests"] == 1
    assert second.state["expires_at"] == 160
    now[0] = 161
    second.run([task(2021)], source)
    assert len(calls) == 1
    assert second.state["status"] == "deadline_reached"
    with pytest.raises(ValueError, match="immutable"):
        PublicAcquisition(tmp_path, "test", "2026-10-10", 3, 60)


def test_retry_metadata_survives_restart_and_wait(tmp_path):
    calls = []
    now = [100.0]
    response = {
        "status": "unavailable",
        "errors": [{"code": "document_source_network_error"}],
        "retryable": True,
        "retry_at": 120,
        "next_action": "wait_until_retry_at",
    }
    source = SimpleNamespace(
        collect=lambda *a: calls.append(a) or copy.deepcopy(response)
    )
    first = PublicAcquisition(
        tmp_path, "retry", "2026-10-10", 2, 60, clock=lambda: now[0]
    )
    first.run([task()], source)
    second = PublicAcquisition(
        tmp_path, "retry", "2026-10-10", 2, 60, clock=lambda: now[0]
    )
    second.run([task()], source)
    assert len(calls) == 1
    assert second.state["attempts"][0]["result"] == response
    now[0] = 121
    second.run([task()], source)
    assert len(calls) == 2
    assert second.state["reserved_document_requests"] == 2


def test_tasks_derive_company_period_and_interim_not_fixed_years():
    coverage = {
        "rows": [
            {
                "status": "missing_original",
                "issuer_id": "a_share:000858",
                "company_name": "五粮液",
                "period_end": "2024-06-30",
                "kind": "interim",
            }
        ]
    }
    tasks = acquisition_tasks([coverage, coverage])
    assert len(tasks) == 1
    assert tasks[0]["year"] == 2024
    assert tasks[0]["report_type"] == "half_year"


def test_new_sources_preserve_existing_checkpoint_and_write_revision(
    tmp_path, monkeypatch
):
    import scripts.run_longitudinal_research as runner

    group = {"group_id": "g", "companies": [], "industry": "test", "history_years": 7}
    config = {"groups": [group]}
    folder = tmp_path / "g"
    folder.mkdir()
    checkpoint = folder / "checkpoint.json"
    checkpoint.write_text('{"status":"researching"}')
    calls = []
    monkeypatch.setattr(runner, "discover_documents", lambda *a: [])
    monkeypatch.setattr(
        runner, "_preparation_inputs", lambda *a: ({"new_source": True}, {})
    )
    monkeypatch.setattr(runner, "prepare", lambda root, *a: calls.append(root) or [])
    monkeypatch.setattr(runner, "render_index", lambda *a: None)
    result = runner.acquire(
        tmp_path,
        config,
        "2026-10-10",
        run_id="test",
        source=SimpleNamespace(collect=lambda *a: pytest.fail("no missing tasks")),
    )
    assert result["research_migration_required"] is True
    assert calls[0].is_relative_to(tmp_path / "revisions")
    assert checkpoint.read_text() == '{"status":"researching"}'
    assert not (calls[0] / "g" / "checkpoint.json").exists()


def test_interrupted_reservation_waits_lease_then_reuses_same_budget(tmp_path):
    from src.fundamental_quant.session_sources import SOURCE_REQUEST_LEASE

    now = [100.0]
    run = PublicAcquisition(
        tmp_path, "interrupt", "2026-10-10", 2, 3600, clock=lambda: now[0]
    )
    run.state["reserved_document_requests"] = 1
    run.state["attempts"] = [{"task": task(), "started_at": 100.0, "result": None}]
    run.save()
    calls = []
    source = SimpleNamespace(
        collect=lambda *a: calls.append(a) or {"status": "success"}
    )
    restarted = PublicAcquisition(
        tmp_path, "interrupt", "2026-10-10", 2, 3600, clock=lambda: now[0]
    )
    restarted.run([task()], source)
    assert calls == []
    assert restarted.state["status"] == "waiting_interrupted_source_lease"
    now[0] += SOURCE_REQUEST_LEASE + 1
    restarted.run([task()], source)
    assert len(calls) == 1
    assert restarted.state["reserved_document_requests"] == 2
    assert restarted.state["expires_at"] == 3700
