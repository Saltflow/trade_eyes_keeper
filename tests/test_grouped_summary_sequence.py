"""Offline checks of shared summary budget and source-part handling."""

import json
import sys
from decimal import Decimal
from types import SimpleNamespace

import pytest

from scripts import run_grouped_summary_sequence as sequence


def test_unknown_usage_is_fully_reserved():
    report = {"known_usage_cost_usd": "0.4", "unknown_usage_reservation_usd": "0.7"}
    assert sequence.accounted(report) == Decimal("1.1")


def test_merge_requires_both_existing_nonempty_parts(tmp_path):
    first, second = tmp_path / "one.md", tmp_path / "two.md"
    first.write_text("first source", encoding="utf8")
    job = {
        "source": str(tmp_path / "merged"),
        "company": "X",
        "parts": [str(first), str(second)],
        "part_years": ["2019–2022", "2023–2025"],
    }
    with pytest.raises(ValueError, match="required_part_summary_missing"):
        sequence.prepare_merge(job)
    # A failed merge leaves its directory for review and is never overwritten.
    assert not (tmp_path / "merged" / "X" / "模型正文.md").exists()


def test_shared_budget_blocks_a_request_before_send(tmp_path, monkeypatch):
    initial = tmp_path / "initial"
    initial.mkdir()
    (initial / "执行结果.json").write_text(
        json.dumps(
            {
                "known_usage_cost_usd": "1.0",
                "unknown_usage_reservation_usd": "0.5",
                "requests_sent": 1,
                "receipts": [{"status": "complete"}],
            }
        ),
        encoding="utf8",
    )
    output = tmp_path / "second"
    job = {
        "id": "next",
        "kind": "summary",
        "source": "source",
        "metadata": "metadata",
        "company": "X",
        "output": str(output),
    }
    queue = {
        "budget_cny": "20",
        "budget_usd_tariff_comparable": "3",
        "initial_summary": str(initial),
        "initial_pid": 0,
        "pricing_config": "pricing.json",
        "pricing_checked_utc_date": sequence.datetime.now(sequence.timezone.utc)
        .date()
        .isoformat(),
        "items": [job],
    }
    queue_path = tmp_path / "queue.json"
    queue_path.write_text(json.dumps(queue), encoding="utf8")
    monkeypatch.setattr(sys, "argv", ["sequence", "--queue", str(queue_path)])
    monkeypatch.setattr(sequence.psutil, "pid_exists", lambda _: False)

    def prepare(*args, **kwargs):
        output.mkdir()
        (output / "plan.json").write_text(
            '{"reserved_usd_peak":"1.6"}', encoding="utf8"
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(sequence.subprocess, "run", prepare)
    monkeypatch.setattr(
        sequence.subprocess, "Popen", lambda *a, **k: pytest.fail("must not send")
    )
    assert sequence.main() == 2
    status = sequence.load(tmp_path / "sequence_status.json")
    assert status["state"] == "blocked"
    assert status["error"] == "cross_group_budget_guard_failed"
