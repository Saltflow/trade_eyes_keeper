from pathlib import Path

import ci_cd_deploy as deploy


def test_data_recovery_regression_blocks_deployment(monkeypatch):
    monkeypatch.setattr(deploy, "_run_local", lambda *a, **kw: (True, "OK", ""))
    calls = []
    required = {
        "tests/test_vanguard_identity_cache.py",
        "tests/test_backtest_recovery.py",
        "tests/test_us_market_history.py",
    }
    root = Path(__file__).resolve().parents[1]
    assert all((root / name).is_file() for name in required)

    def pytest_run(*args, **kw):
        calls.append(args)
        if required.issubset(args):
            return False, "shared cache recovery regressed", ""
        return True, "passed", ""

    monkeypatch.setattr(deploy, "_run_workspace_pytest", pytest_run)
    assert deploy._pre_deploy_checks(False) is False
    assert required.issubset(calls[0])
    assert len(calls) == 1
