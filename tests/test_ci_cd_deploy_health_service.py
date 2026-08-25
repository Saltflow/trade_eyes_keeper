from pathlib import Path

import ci_cd_deploy


def test_health_server_is_managed_only_by_persistent_systemd_unit():
    command = ci_cd_deploy._build_health_systemd_command()

    assert "/etc/systemd/system/trade-eyes-health.service" in command
    assert "systemctl enable trade-eyes-health.service" in command
    assert "systemctl restart trade-eyes-health.service" in command
    assert "Restart=always" in command
    assert "ExecStart=/usr/bin/python3" in command
    assert "nohup python3 main.py --health-server" not in command
    assert "while true" in command  # legacy loop cleanup only
    assert "pkill -f '^/usr/bin/python3 /root/trade_eyes_keeper/main.py$'" in command


def test_deploy_flow_does_not_start_a_health_server_nohup_loop():
    source = Path(ci_cd_deploy.__file__).read_text(encoding="utf-8")

    deploy_source = source[source.index("def deploy()") :]
    assert "nohup python3 main.py --health-server" not in deploy_source
    assert "_build_health_systemd_command()" in deploy_source
    assert "SKIP_NOTIFICATIONS=true timeout 180 python3 main.py --once" in deploy_source


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
