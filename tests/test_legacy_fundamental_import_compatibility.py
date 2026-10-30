"""Preserved legacy APIs import offline without reviving supported execution."""

import ast
import copy
import importlib
import socket
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src/fundamental_quant"


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Unexpected network in legacy import compatibility acceptance")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def test_preserved_package_definitions_and_manual_cli_import_without_running():
    # Importing definitions does not construct a service, invoke a model or run CLI.
    names = sorted(path.stem for path in PACKAGE.glob("*.py"))
    for name in names:
        importlib.import_module("src.fundamental_quant." + name)
    manual = importlib.import_module("scripts.run_longitudinal_research")
    assert callable(manual.main)
    assert callable(manual.prepare_comparison)


def test_preserved_consumer_imports_resolve_every_legacy_private_helper():
    resolved = set()
    for path in PACKAGE.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ImportFrom) or node.level != 1:
                continue
            if node.module not in {"legacy_reporting", "legacy_service"}:
                continue
            target = importlib.import_module("src.fundamental_quant." + node.module)
            for member in node.names:
                assert hasattr(target, member.name), (path.name, member.name)
                resolved.add(member.name)
    assert {
        "_atomic",
        "_document",
        "_escape",
        "_json",
        "_section",
        "_candidate",
    } <= resolved
    assert {"ResearchBlocked", "evidence_hash"} <= resolved


def test_legacy_helper_exports_do_not_expose_an_executable_service():
    from src.fundamental_quant import legacy_service

    assert not hasattr(legacy_service, "FundamentalQuantService")
    blocked = legacy_service.ResearchBlocked([{"code": "frozen_evidence"}])
    assert blocked.errors == [{"code": "frozen_evidence"}]
    assert str(blocked) == "frozen_evidence"


def test_evidence_hash_preserves_quote_exclusion_and_original_input():
    from src.fundamental_quant.legacy_service import evidence_hash

    packet = {
        "company": {"id": "sample", "market_cap": 10, "quote_as_of": "2026-01-01"},
        "sources": [{"id": "q", "kind": "quote"}, {"id": "f", "kind": "report"}],
        "facts": [{"source_id": "q", "value": 10}, {"source_id": "f", "value": 2}],
        "peer_candidates": [],
        "peer_packets": [],
    }
    original = copy.deepcopy(packet)
    changed = copy.deepcopy(packet)
    changed["company"].update(market_cap=30, quote_as_of="2026-02-01")
    changed["facts"][0]["value"] = 30
    assert evidence_hash(packet) != evidence_hash(changed)
    assert evidence_hash(packet, exclude_prices=True) == evidence_hash(
        changed, exclude_prices=True
    )
    changed["facts"][1]["value"] = 3
    assert evidence_hash(packet, exclude_prices=True) != evidence_hash(
        changed, exclude_prices=True
    )
    assert packet == original


def test_original_reporting_private_atomic_writer_remains_offline(tmp_path):
    from src.fundamental_quant.legacy_reporting import _atomic, _document, _escape

    output = tmp_path / "legacy.html"
    _atomic(output, _document("historical", _escape("<saved>")))
    assert "&lt;saved&gt;" in output.read_text(encoding="utf-8")
    assert [path.name for path in tmp_path.iterdir()] == ["legacy.html"]


def test_preserved_worker_uses_blocked_current_service_even_if_old_config_enabled(
    tmp_path,
):
    from src.fundamental_quant.worker import execute_job

    root = tmp_path / "never_created"
    config = {
        "fundamental_quant": {
            "root": str(root),
            "enabled": True,
            "schedule": {"enabled": True},
            "notifications": {"enabled": True},
            "llm": {"enabled": True},
        }
    }
    for job in ("daily", "weekly"):
        result = execute_job(config, job)
        assert result["status"] == "blocked"
        assert result["research_status"] == "blocked"
        assert "legacy_fundamental_workflow_retired" in result["error_codes"]
    assert not root.exists()


def test_new_supported_roots_do_not_import_or_register_legacy_implementations():
    allowed = {"service", "reporting", "archive"}
    for relative in (
        "main.py",
        "src/core/runtime_service.py",
        "src/core/schedule_manager.py",
        "src/interactive/command_dispatcher.py",
        "src/interactive/assistant/service.py",
        "src/interactive/assistant/research_worker.py",
        "scripts/financial_pipeline.py",
        "scripts/research_report.py",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "run_longitudinal_research" not in text
        assert "legacy_reporting" not in text and "legacy_service" not in text
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ImportFrom) and node.module:
                module = node.module
                if "fundamental_quant" in module:
                    assert module.split("fundamental_quant.", 1)[-1] in allowed
            if isinstance(node, ast.Import):
                assert not any(
                    item.name.startswith("src.fundamental_quant.")
                    and item.name.rsplit(".", 1)[-1] not in allowed
                    for item in node.names
                )
