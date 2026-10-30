from pathlib import Path

import ci_cd_deploy


def test_runtime_service_migrates_after_stop_and_before_start():
    command = ci_cd_deploy._build_runtime_systemd_command()

    assert "systemctl disable --now trade-eyes-health.service" in command
    assert "rm -f /etc/systemd/system/trade-eyes-health.service" in command
    assert "ExecStart=/usr/bin/python3" in command
    assert "/main.py --service" in command
    assert "KillMode=control-group" in command
    assert "Restart=on-failure" in command
    assert command.index("systemctl stop trade-eyes.service") < command.index(
        "migrate_legacy_cron"
    ) < command.index("systemctl restart trade-eyes.service")
    assert "systemctl enable trade-eyes.service" in command


def test_deploy_requires_current_pid_and_fresh_local_heartbeat():
    command = ci_cd_deploy._build_runtime_verify_command()
    assert "read_service_status(load_config())" in command
    assert 'str(status.get("pid")) == pid' in command
    assert 'status.get("ready")' in command
    assert "[SERVICE_READY]" in command
    assert "curl" not in command
    assert "http://" not in command


def test_deploy_uses_one_runtime_and_notifies_after_readiness():
    source = Path(ci_cd_deploy.__file__).read_text(encoding="utf-8")
    deploy_source = source[source.index("def deploy()") :]
    assert "nohup python3 main.py --health-server" not in deploy_source
    assert "_build_runtime_systemd_command()" in deploy_source
    assert "opt_cron_line" not in deploy_source
    assert "SKIP_NOTIFICATIONS=true timeout 180 python3 main.py --once" in deploy_source
    assert deploy_source.index("Verify local service readiness") < deploy_source.index(
        "Send deployment notification"
    )


def test_deploy_prefers_user_scoped_key_when_legacy_relative_key_is_absent(
    monkeypatch,
):
    monkeypatch.setenv("DEPLOY_SSH_KEY", "deploy_key")
    monkeypatch.setattr(
        ci_cd_deploy,
        "STANDARD_DEPLOY_KEY",
        r"C:\Users\one\.ssh\trade_eyes_keeper_deploy_key",
    )
    monkeypatch.setattr(
        ci_cd_deploy.os.path,
        "exists",
        lambda value: value == ci_cd_deploy.STANDARD_DEPLOY_KEY,
    )

    assert ci_cd_deploy._get_ssh_key().endswith(
        "/.ssh/trade_eyes_keeper_deploy_key"
    )


def test_deploy_connectivity_probe_allows_a_realistic_ssh_handshake(monkeypatch):
    captured = {}

    class Result:
        returncode = 0
        stdout = "pong\n"
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["timeout"] = kwargs["timeout"]
        return Result()

    monkeypatch.setattr(ci_cd_deploy.subprocess, "run", fake_run)
    monkeypatch.setattr(ci_cd_deploy, "REMOTE_HOST", "127.0.0.2")

    assert ci_cd_deploy._test_ssh_connectivity() == (True, "")
    assert "ConnectTimeout=20" in captured["command"]
    assert captured["timeout"] == 30


def test_ssh_command_is_noninteractive_and_uses_keepalives(monkeypatch):
    captured = {}

    class Result:
        returncode = 0
        stdout = "ok\n"
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["stdin"] = kwargs["stdin"]
        captured["timeout"] = kwargs["timeout"]
        return Result()

    monkeypatch.setattr(ci_cd_deploy.subprocess, "run", fake_run)

    assert ci_cd_deploy._ssh_cmd("echo ok", timeout=47) == (True, "ok\n", "")
    assert "ConnectTimeout=20" in captured["command"]
    assert "ServerAliveInterval=10" in captured["command"]
    assert "ServerAliveCountMax=3" in captured["command"]
    assert "BatchMode=yes" in captured["command"]
    assert captured["stdin"] is ci_cd_deploy.subprocess.DEVNULL
    assert captured["timeout"] == 47


def test_retired_web_unit_is_masked_before_new_service_starts():
    command = ci_cd_deploy._build_runtime_systemd_command()
    assert command.index("systemctl mask trade-eyes-health.service") < command.index(
        "systemctl restart trade-eyes.service"
    )
    assert "--notify-start" not in command
