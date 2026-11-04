"""Retired research and private launchers cannot be restored by old entrypoints."""

import ast
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
    "run_longitudinal_research",
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


def test_retired_packages_launchers_and_configuration_are_removed():
    assert not list((ROOT / "src/fundamental_quant").glob("*.py"))
    for path in (
        "scripts/run_personal_monitor_feishu_test.py",
        "config/personal_monitor_feishu_test.yaml.example",
        "config/longitudinal_research.json",
    ):
        assert not (ROOT / path).exists(), path
    assert not (
        ROOT / "skills/fundamental-research/scripts/build_offline_reports.py"
    ).exists()


def test_runtime_imports_do_not_depend_on_deleted_research_implementations():
    paths = [ROOT / "main.py"]
    for name in ("src", "scripts"):
        paths.extend((ROOT / name).rglob("*.py"))
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
            if isinstance(node, ast.Import):
                names = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                if "fundamental_quant" in name.split("."):
                    pytest.fail(f"Deleted research import: {path}:{node.lineno}: {name}")
                assert name != "scripts.run_longitudinal_research", path


@pytest.mark.parametrize(
    "flag",
    [
        "--fundamental-quant", "--fundamental-quant-status",
        "--fundamental-quant-report", "--fundamental-quant-notify",
        "--fundamental-materials",
    ],
)
def test_legacy_flags_are_unavailable(flag, monkeypatch, capsys):
    import main as app

    def no_configuration_read(*args, **kwargs):
        pytest.fail("Removed CLI must reject before loading business configuration")

    monkeypatch.setattr(app, "load_config", no_configuration_read)
    with pytest.raises(SystemExit) as exc:
        app.main([flag])
    assert exc.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


@pytest.mark.parametrize("text", [
    "/fundamental", "/fundamental status", "/fundamental 600036",
    "/alerts", "/reset_alerts 600036",
])
def test_removed_bot_command_is_unknown(text):
    from src.interactive import command_parser as commands
    from src.interactive.command_dispatcher import dispatch_command

    result = commands.parse_command(text)
    assert isinstance(result, commands.ErrorCommand)
    assert "未知命令" in dispatch_command(result)


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
