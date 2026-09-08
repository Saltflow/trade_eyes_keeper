"""Local deployment acceptance tests; all SSH/SCP and notifications are mocked."""

import io
import shlex
import socket
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import ci_cd_deploy
from src.search.config import get_market_optimizer_configs

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_PATH = PROJECT_ROOT / "config" / "config.yaml.example"
VALIDATION_STEP = "Validate remote optimizer configuration"
PROGRESSION_STEPS = (
    "System test",
    "Clean up legacy daily/brief cron entries",
    "Add optimizer cron if missing",
    "Send deployment notification",
    "Install and restart health server service",
)


def _write_config(path, config):
    path.write_text(yaml.safe_dump(config), encoding="utf-8")


def _run_validation_payload(command):
    """Execute only the Python validation payload, without a shell or SSH."""
    arguments = shlex.split(command)
    assert arguments[:3] == ["cd", str(Path.cwd()).replace("\\", "/"), "&&"]
    assert arguments[3:8] == ["timeout", "30", "python3", "-B", "-c"]
    stdout, stderr = io.StringIO(), io.StringIO()
    exit_code = 0
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            # The payload is constant application code, never config content.
            payload = compile(arguments[8], "<remote-config-validation>", "exec")
            exec(payload, {})  # noqa: S102
        except SystemExit as exc:
            exit_code = exc.code
    return exit_code == 0, stdout.getvalue(), stderr.getvalue()


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch):
    def unexpected_network(*args, **kwargs):
        pytest.fail("Deployment acceptance tests must never access the network")

    monkeypatch.setattr(socket, "create_connection", unexpected_network)
    monkeypatch.setattr(socket.socket, "connect", unexpected_network)


@pytest.fixture
def server_config(tmp_path):
    config_dir = tmp_path / "server checkout" / "config"
    config_dir.mkdir(parents=True)
    app_path = config_dir / "config.yaml"
    app_path.write_bytes(EXAMPLE_PATH.read_bytes())
    constraints_path = config_dir / "optimizer_constraints.yaml"
    constraints_path.write_bytes(
        (PROJECT_ROOT / "config" / "optimizer_constraints.yaml").read_bytes()
    )
    return SimpleNamespace(
        root=config_dir.parent, app=app_path, constraints=constraints_path
    )


@pytest.fixture
def deployment(monkeypatch, tmp_path, server_config):
    local_config_dir = tmp_path / "local checkout" / "config"
    local_config_dir.mkdir(parents=True)
    local_config = local_config_dir / "config.yaml"
    local_config.write_bytes(EXAMPLE_PATH.read_bytes())
    state = SimpleNamespace(
        events=[],
        server=server_config,
        local_config=local_config,
        backup=None,
        post_push_config=None,
        sync_failure=None,
        sync_exception=None,
        restore_failure=False,
        validation_result=None,
    )
    for variable in ("SYNC_CONFIG", "SYNC_ENV", "DRY_RUN"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("CLEAN_BEFORE_DEPLOY", "false")
    monkeypatch.setattr(ci_cd_deploy, "PROJECT_DIR", str(local_config_dir.parent))
    monkeypatch.setattr(ci_cd_deploy, "REMOTE_DIR", server_config.root.as_posix())
    monkeypatch.setattr(ci_cd_deploy, "REMOTE_HOST", "deployment.invalid")
    monkeypatch.setattr(ci_cd_deploy, "_get_ssh_key", lambda: "unused-test-key")
    monkeypatch.setattr(ci_cd_deploy, "_check_prerequisites", lambda: None)
    monkeypatch.setattr(ci_cd_deploy, "_pre_deploy_checks", lambda dry_run: True)
    monkeypatch.setattr(ci_cd_deploy, "_ensure_remote_repo", lambda: True)
    monkeypatch.setattr(ci_cd_deploy.time, "sleep", lambda seconds: None)
    monkeypatch.chdir(server_config.root)

    def fake_push():
        state.events.append("Git push")
        if state.post_push_config is not None:
            _write_config(server_config.app, state.post_push_config)
        return True

    def fake_scp(command, **kwargs):
        assert command[0] == "scp", "No other subprocess is allowed in this test"
        source = Path(command[-2])
        state.events.append(f"Sync {source.name}")
        if state.sync_exception is not None:
            raise state.sync_exception
        if state.sync_failure is not None:
            return subprocess.CompletedProcess(command, 1, "", state.sync_failure)
        (server_config.app.parent / source.name).write_bytes(source.read_bytes())
        return subprocess.CompletedProcess(command, 0, "", "")

    def fake_ssh(command, description="", timeout=60):
        state.events.append(description)
        if description == "Backup server config.yaml":
            state.backup = (
                server_config.app.read_bytes() if server_config.app.exists() else None
            )
        elif description == "Restore server config.yaml":
            if state.restore_failure:
                return False, "", "cp: Permission denied"
            if state.backup is not None:
                server_config.app.write_bytes(state.backup)
        elif description == VALIDATION_STEP:
            if state.validation_result is not None:
                return state.validation_result
            before = {
                path.relative_to(server_config.root): path.read_bytes()
                for path in server_config.root.rglob("*")
                if path.is_file()
            }
            result = _run_validation_payload(command)
            after = {
                path.relative_to(server_config.root): path.read_bytes()
                for path in server_config.root.rglob("*")
                if path.is_file()
            }
            assert after == before, "Validation must not write or repair server files"
            return result
        responses = {
            "System test": "---EXIT: 0 ---\n---ERRORS---\n0\n---TAIL---\n",
            "Verify cron": "0 2 * * * python3 main.py --optimize\n",
            "Count optimizer run manifests": "0\n",
            "Check email archives": "[ARCHIVE_NA]\n",
            "Get git version": "test-version\n",
            "Send deployment notification": "[NOTIFY_OK]\n",
            "Verify health server HTTP response": "[HS_HTTP_OK]\n",
        }
        return True, responses.get(description, ""), ""

    monkeypatch.setattr(ci_cd_deploy, "_git_push", fake_push)
    monkeypatch.setattr(ci_cd_deploy, "_ssh_cmd", fake_ssh)
    monkeypatch.setattr(ci_cd_deploy.subprocess, "run", fake_scp)
    return state


def _assert_progression_blocked(deployment, capsys):
    assert not set(PROGRESSION_STEPS).intersection(deployment.events)
    output = capsys.readouterr().out
    assert "Deployment completed successfully!" not in output
    assert "[FAIL]" in output
    return output


def test_example_declares_the_approved_complete_market_contracts():
    config = yaml.safe_load(EXAMPLE_PATH.read_text(encoding="utf-8"))
    resolved = get_market_optimizer_configs(config)
    actual = {
        group: (
            market.strategy_name,
            market.solver_id,
            market.gate_profile,
            market.walk_forward_profile,
            market.execution_profile,
            market.benchmark_profile,
        )
        for group, market in resolved.items()
    }
    assert actual == {
        "a_share": (
            "technical_ensemble",
            "local_genetic",
            "standard",
            "a_share_84m",
            "a_share_cny",
            "a_share",
        ),
        "hk": (
            "regime_pullback",
            "simulated_annealing",
            "standard",
            "hk_84m",
            "hk_hkd",
            "hk",
        ),
        "us": (
            "percentile",
            "random",
            "exploratory",
            "us_84m",
            "us_usd",
            "us",
        ),
    }


@pytest.mark.parametrize(
    ("case", "error"),
    [
        ("legacy_engine", "global optimizer fallback fields are forbidden: engine"),
        ("no_markets", "optimizer.markets must be a mapping"),
        ("missing_hk", "optimizer.markets is missing groups: ['hk']"),
        ("us_solver", "us: unknown solver"),
        ("us_profile", "us: unknown or invalid execution_profiles profile"),
        ("malformed_yaml", "unable to load configuration config/config.yaml"),
        ("missing_file", "configuration file not found: config/config.yaml"),
        (
            "missing_constraints",
            "configuration file not found: config/optimizer_constraints.yaml",
        ),
    ],
)
def test_invalid_effective_server_config_blocks_deployment(
    deployment, capsys, case, error
):
    config = yaml.safe_load(deployment.server.app.read_text(encoding="utf-8"))
    if case == "legacy_engine":
        config["optimizer"] = {"engine": "technical_ensemble"}
    elif case == "no_markets":
        config["optimizer"].pop("markets")
    elif case == "missing_hk":
        config["optimizer"]["markets"].pop("hk")
    elif case == "us_solver":
        config["optimizer"]["markets"]["us"]["solver_id"] = "invalid-solver"
    elif case == "us_profile":
        config["optimizer"]["markets"]["us"]["execution_profile"] = "invalid-profile"
    _write_config(deployment.server.app, config)
    if case == "malformed_yaml":
        deployment.server.app.write_text("optimizer: [\n", encoding="utf-8")
    elif case == "missing_file":
        deployment.server.app.unlink()
    elif case == "missing_constraints":
        deployment.server.constraints.unlink()

    assert ci_cd_deploy.deploy() is False

    assert VALIDATION_STEP in deployment.events
    assert "Sync config.yaml" not in deployment.events
    output = _assert_progression_blocked(deployment, capsys)
    assert "[FAIL] optimizer_config" in output
    assert "[OPTIMIZER_CONFIG_ERROR]" in output
    assert error in output.replace("\\", "/")


def test_code_only_deploy_validates_restored_server_config(deployment):
    config = yaml.safe_load(deployment.server.app.read_text(encoding="utf-8"))
    config["health_server"]["ssl"] = True
    config["optimizer"]["markets"]["hk"]["solver_id"] = "random"
    _write_config(deployment.server.app, config)
    original = deployment.server.app.read_bytes()
    deployment.post_push_config = {"optimizer": {"engine": "percentile"}}

    assert ci_cd_deploy.deploy() is True

    assert deployment.server.app.read_bytes() == original
    assert "Sync config.yaml" not in deployment.events
    validation_index = deployment.events.index(VALIDATION_STEP)
    assert deployment.events.index("Restore server config.yaml") < validation_index
    assert deployment.events.index("Install dependencies") < validation_index
    assert all(
        deployment.events.index(step) > validation_index for step in PROGRESSION_STEPS
    )


def test_explicit_sync_replaces_legacy_config_and_is_not_undone(
    deployment, monkeypatch
):
    monkeypatch.setenv("SYNC_CONFIG", "true")
    _write_config(deployment.server.app, {"optimizer": {"engine": "percentile"}})
    expected = deployment.local_config.read_bytes()

    assert ci_cd_deploy.deploy() is True

    assert deployment.server.app.read_bytes() == expected
    events = deployment.events
    assert events.index("Restore server config.yaml") < events.index("Sync config.yaml")
    assert events.index("Sync config.yaml") < events.index(VALIDATION_STEP)
    assert events.count("Restore server config.yaml") == 1


def test_invalid_explicit_sync_is_rejected_without_restoring_valid_backup(
    deployment, monkeypatch, capsys
):
    monkeypatch.setenv("SYNC_CONFIG", "true")
    _write_config(deployment.local_config, {"optimizer": {"engine": "percentile"}})
    expected = deployment.local_config.read_bytes()

    assert ci_cd_deploy.deploy() is False

    assert deployment.server.app.read_bytes() == expected
    output = _assert_progression_blocked(deployment, capsys)
    assert "global optimizer fallback fields are forbidden: engine" in output


@pytest.mark.parametrize("failure", ["missing", "scp_error", "timeout", "os_error"])
def test_explicit_sync_failure_cannot_continue_with_old_valid_config(
    deployment, monkeypatch, capsys, failure
):
    monkeypatch.setenv("SYNC_CONFIG", "true")
    original = deployment.server.app.read_bytes()
    if failure == "missing":
        deployment.local_config.unlink()
    elif failure == "scp_error":
        deployment.sync_failure = "scp: Permission denied"
    elif failure == "timeout":
        deployment.sync_exception = subprocess.TimeoutExpired("scp", 30)
    else:
        deployment.sync_exception = OSError("scp unavailable")

    assert ci_cd_deploy.deploy() is False

    assert deployment.server.app.read_bytes() == original
    assert VALIDATION_STEP not in deployment.events
    assert "[FAIL] config_sync" in _assert_progression_blocked(deployment, capsys)


def test_restore_failure_stops_deployment(deployment, capsys):
    deployment.restore_failure = True

    assert ci_cd_deploy.deploy() is False

    assert VALIDATION_STEP not in deployment.events
    output = _assert_progression_blocked(deployment, capsys)
    assert "[FAIL] config_restore" in output
    assert "cp: Permission denied" in output


@pytest.mark.parametrize(
    "result",
    [
        (False, "[OPTIMIZER_CONFIG_OK]\n", "remote process failed"),
        (True, "", ""),
        (True, "unrelated output\n", ""),
        (True, "echo '[OPTIMIZER_CONFIG_OK]'\n", ""),
        (False, "", "Timeout"),
    ],
)
def test_validator_requires_successful_exit_and_explicit_confirmation(
    deployment, capsys, result
):
    deployment.validation_result = result

    assert ci_cd_deploy.deploy() is False

    assert "[FAIL] optimizer_config" in _assert_progression_blocked(deployment, capsys)


def test_dry_run_config_sync_does_not_upload(deployment, monkeypatch):
    monkeypatch.setenv("SYNC_CONFIG", "true")
    monkeypatch.setenv("DRY_RUN", "true")
    original = deployment.server.app.read_bytes()

    assert ci_cd_deploy._sync_config() is True

    assert deployment.server.app.read_bytes() == original
    assert deployment.events == []
