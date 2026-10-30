from pathlib import Path

import ci_cd_deploy


def test_workspace_pytest_uses_a_project_owned_basetemp(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(ci_cd_deploy, "PROJECT_DIR", str(tmp_path))

    def fake_run_local(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return True, "passed", ""

    monkeypatch.setattr(ci_cd_deploy, "_run_local", fake_run_local)

    assert ci_cd_deploy._run_workspace_pytest("tests/test_example.py", timeout=17)
    assert captured["args"][:4] == (
        ci_cd_deploy.sys.executable,
        "-m",
        "pytest",
        "tests/test_example.py",
    )
    base_temp = Path(captured["args"][-1])
    assert captured["args"][-2] == "--basetemp"
    assert base_temp.parent.parent == tmp_path
    assert captured["kwargs"]["timeout"] == 17
    assert not base_temp.parent.exists()


def test_dry_run_stays_local_and_does_not_parse_mock_remote_output(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")

    def unexpected_remote_preflight():
        raise AssertionError("dry run must not open an SSH connectivity probe")

    monkeypatch.setattr(ci_cd_deploy, "_check_prerequisites", unexpected_remote_preflight)

    assert ci_cd_deploy.deploy() is True
