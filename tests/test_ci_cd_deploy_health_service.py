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


def test_deploy_flow_does_not_start_a_health_server_nohup_loop():
    source = Path(ci_cd_deploy.__file__).read_text(encoding="utf-8")

    deploy_source = source[source.index("def deploy()") :]
    assert "nohup python3 main.py --health-server" not in deploy_source
    assert "_build_health_systemd_command()" in deploy_source
