"""Supported archive roots stay read-only; disputed legacy sources stay isolated."""

import ast
import json
import socket
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OLD_SCRIPTS = (
    "accept_fundamental_quant_email",
    "accept_fundamental_research_offline",
    "accept_longitudinal_comparison",
    "accept_reference_review_offline",
    "accept_research_materials",
    "audit_fundamental_chain",
    "audit_price_benchmark_draft",
    "backfill_enterprise_materials",
    "build_peer_panel",
    "build_research_charts",
    "check_pdf_research_offline",
    "compare_financial_attribution",
    "compare_financial_operating_scope",
    "compare_financial_thresholds",
    "compare_peer_capital_policy",
    "correct_full_comparison",
    "edit_research_with_charts",
    "evaluate_fundamental_quant",
    "freeze_research_baseline",
    "install_fundamental_quant_task",
    "integrate_reviewed_cash_report",
    "render_pdf_research_notes",
    "render_price_research_benchmark",
    "replay_price_benchmark_repairs",
    "review_research_references",
    "run_fr_extraction_pilot",
    "run_full_comparison",
    "run_fundamental_quant_worker",
    "run_pi_research_campaign",
    "run_price_research_benchmark",
)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Unexpected network during retirement acceptance")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.mark.parametrize("name", OLD_SCRIPTS)
def test_old_executable_script_is_physically_removed(name):
    assert not (ROOT / "scripts" / (name + ".py")).exists()


def test_supported_archive_closure_cannot_execute_preserved_legacy_research():
    folder = ROOT / "src/fundamental_quant"
    # These are the current CLI/Bot roots, not every preserved historical file.
    files = [
        folder / (name + ".py")
        for name in ("__init__", "archive", "service", "reporting")
    ]
    assert all(path.is_file() for path in files)
    for path in files:
        text = path.read_text(encoding="utf8")
        assert len(text.splitlines()) <= 100
        for node in ast.walk(ast.parse(text)):
            assert not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"exec", "eval", "__import__"}
            )
            if isinstance(node, ast.Import):
                assert not {a.name.split(".")[0] for a in node.names} & {
                    "requests",
                    "subprocess",
                    "httpx",
                    "openai",
                    "importlib",
                }
            if isinstance(node, ast.ImportFrom) and node.level:
                assert node.module == "archive"
    assert not (
        ROOT / "skills/fundamental-research/scripts/build_offline_reports.py"
    ).exists()


@pytest.mark.parametrize(
    "flag",
    ["--fundamental-quant", "--fundamental-quant-notify", "--fundamental-materials"],
)
def test_legacy_flags_block_even_with_enabled_old_configuration(
    flag, tmp_path, monkeypatch, capsys
):
    import main as app

    config = {
        "fundamental_quant": {
            "root": str(tmp_path / "never_created"),
            "enabled": True,
            "llm": {"enabled": True},
            "schedule": {"enabled": True},
            "materials": {"enabled": True},
            "notifications": {"enabled": True},
        }
    }
    monkeypatch.setattr(app, "load_config", lambda: config)
    assert app.main([flag]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "blocked"
    assert result.get("paid_model_calls", 0) == 0
    assert not (tmp_path / "never_created").exists()


def test_current_data_and_independent_technical_tools_are_preserved():
    for path in (
        "scripts/build_fundamental_data.py",
        "scripts/build_fundamental_data_v2.py",
        "scripts/build_financial_context.py",
        "src/fundamental_data/v2_transport.py",
        "src/fundamental_data/model_context.py",
        "data/fundamental_quant/tokenizer/tokenizer.json",
        "src/interactive/assistant/research.py",
        "tools/feishu-pi/bridge.mjs",
    ):
        assert (ROOT / path).is_file(), path
