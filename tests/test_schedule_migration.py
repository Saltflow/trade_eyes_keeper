"""Legacy cron migration changes only this project's verified schedules."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from src.core.config_store import ConfigStore
from src.core.schedule_migration import LegacyCronMigrationError, migrate_legacy_cron


class FakeCrontab:
    def __init__(self, contents: str):
        self.contents = contents
        self.installs = []
        self.fail_install = False
        self.read_error = False

    def run(self, args, **kwargs):
        assert args in (["crontab", "-l"], ["crontab", "-"])
        if args[-1] == "-l":
            if self.read_error:
                return SimpleNamespace(returncode=1, stdout="", stderr="not permitted")
            return SimpleNamespace(returncode=0, stdout=self.contents, stderr="")
        if self.fail_install:
            return SimpleNamespace(returncode=1, stdout="", stderr="install failed")
        self.contents = kwargs["input"]
        self.installs.append(self.contents)
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def _setup(tmp_path: Path, monkeypatch, config: dict, contents: str):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    cron = FakeCrontab(contents)
    monkeypatch.setattr("src.core.schedule_migration.subprocess.run", cron.run)
    return path, cron


def test_adopts_optimizer_time_and_preserves_foreign_jobs_verbatim(
    tmp_path, monkeypatch
):
    original = (
        "# maintenance\n"
        "15 1 * * * cd /srv/trade-other && python3 main.py --optimize\n"
        "5 3 * * * cd /srv/trade && python3 main.py --optimize >> /tmp/opt.log 2>&1\n"
        "0 4 * * * python3 /srv/another/main.py --once\n"
        "0 5 * * * echo '/srv/trade/main.py --optimize'\n"
    )
    path, cron = _setup(tmp_path, monkeypatch, {"stocks": ["600036"]}, original)

    result = migrate_legacy_cron(path, "/srv/trade")

    assert result["removed_jobs"] == 1
    assert ConfigStore(path).load_raw() == {
        "stocks": ["600036"],
        "scheduler": {"optimize_enabled": True, "optimize_time": "03:05"},
    }
    assert cron.contents == original.splitlines(keepends=True)[0] + "".join(
        original.splitlines(keepends=True)[1:2] + original.splitlines(keepends=True)[3:]
    )


@pytest.mark.parametrize("enabled", [False, True])
def test_explicit_optimizer_switch_wins_over_legacy_cron(
    tmp_path, monkeypatch, enabled
):
    config = {"scheduler": {"optimize_enabled": enabled, "optimize_time": "01:10"}}
    path, cron = _setup(
        tmp_path,
        monkeypatch,
        config,
        "*/10 * * * * cd /srv/trade && python3 main.py --optimize\n",
    )
    before = path.read_bytes()
    result = migrate_legacy_cron(path, "/srv/trade")
    assert result == {"removed_jobs": 1, "migrated_fields": []}
    assert path.read_bytes() == before
    assert cron.contents == ""


@pytest.mark.parametrize(
    "cron_contents",
    [
        "0 2 * * 1-5 cd /srv/trade && python3 main.py --optimize\n",
        "*/5 2 * * * cd /srv/trade && python3 main.py --optimize\n",
        "0 2 * * * cd /srv/trade && python3 main.py --optimize\n"
        "0 3 * * * cd /srv/trade && python3 main.py --optimize\n",
        "@hourly cd /srv/trade && python3 main.py --optimize\n",
        "0 2 * * * cd /srv/trade && python3 main.py --optimize --group us\n",
        "CRON_TZ=UTC\n0 2 * * * cd /srv/trade && python3 main.py --optimize\n",
    ],
)
def test_ambiguous_optimizer_migration_preserves_both_files(
    tmp_path, monkeypatch, cron_contents
):
    path, cron = _setup(tmp_path, monkeypatch, {"stocks": []}, cron_contents)
    before = path.read_bytes()
    with pytest.raises(LegacyCronMigrationError):
        migrate_legacy_cron(path, "/srv/trade")
    assert path.read_bytes() == before
    assert cron.contents == cron_contents
    assert cron.installs == []


def test_no_old_cron_does_not_enable_optimizer(tmp_path, monkeypatch):
    path, cron = _setup(tmp_path, monkeypatch, {"stocks": []}, "# unchanged\n")
    before = path.read_bytes()
    assert migrate_legacy_cron(path, "/srv/trade")["removed_jobs"] == 0
    assert path.read_bytes() == before
    assert cron.installs == []


def test_absent_crontab_does_not_enable_optimizer(tmp_path, monkeypatch):
    path, _cron = _setup(tmp_path, monkeypatch, {"stocks": []}, "")
    monkeypatch.setattr(
        "src.core.schedule_migration.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="no crontab for service-user"
        ),
    )
    assert migrate_legacy_cron(path, "/srv/trade")["removed_jobs"] == 0
    assert ConfigStore(path).load_raw() == {"stocks": []}


def test_daily_and_brief_use_explicit_yaml_times(tmp_path, monkeypatch):
    config = {
        "scheduler": {
            "run_time": "19:00",
            "brief_reports": [{"id": "morning_snapshot", "run_time": "09:50"}],
        }
    }
    path, cron = _setup(
        tmp_path,
        monkeypatch,
        config,
        "0 18 * * 1-5 cd /srv/trade && python3 main.py --once\n"
        "30 9 * * 1-5 python3 /srv/trade/main.py --brief morning_snapshot\n",
    )
    result = migrate_legacy_cron(path, "/srv/trade")
    assert result == {"removed_jobs": 2, "migrated_fields": []}
    assert ConfigStore(path).load_raw() == config
    assert cron.contents == ""


def test_missing_daily_and_brief_times_are_adopted(tmp_path, monkeypatch):
    path, cron = _setup(
        tmp_path,
        monkeypatch,
        {"scheduler": {}},
        "15 19 * * * cd '/srv/trade project' && /opt/venv/bin/python3 -u main.py --once\n"
        "35 14 * * * python3 '/srv/trade project/main.py' --brief afternoon_snapshot\n",
    )
    result = migrate_legacy_cron(path, "/srv/trade project")
    raw = ConfigStore(path).load_raw()
    assert result["removed_jobs"] == 2
    assert raw["scheduler"]["run_time"] == "19:15"
    assert raw["scheduler"]["brief_reports"] == [
        {"id": "afternoon_snapshot", "enabled": True, "run_time": "14:35"}
    ]
    assert "optimize_enabled" not in raw["scheduler"]
    assert cron.contents == ""


def test_compound_job_does_not_delete_unrelated_command(tmp_path, monkeypatch):
    contents = "0 2 * * * python3 /srv/trade/main.py --optimize && /usr/bin/backup\n"
    path, cron = _setup(tmp_path, monkeypatch, {"stocks": []}, contents)
    before = path.read_bytes()
    with pytest.raises(LegacyCronMigrationError, match="额外 shell 命令"):
        migrate_legacy_cron(path, "/srv/trade")
    assert path.read_bytes() == before
    assert cron.contents == contents
    assert not cron.installs


def test_install_failure_does_not_save_config(tmp_path, monkeypatch):
    contents = "0 2 * * * cd /srv/trade && python3 main.py --optimize\n"
    path, cron = _setup(tmp_path, monkeypatch, {"stocks": []}, contents)
    cron.fail_install = True
    before = path.read_bytes()
    with pytest.raises(LegacyCronMigrationError, match="安装"):
        migrate_legacy_cron(path, "/srv/trade")
    assert path.read_bytes() == before
    assert cron.contents == contents


def test_config_commit_failure_restores_old_crontab(tmp_path, monkeypatch):
    contents = "0 2 * * * cd /srv/trade && python3 main.py --optimize\n"
    path, cron = _setup(tmp_path, monkeypatch, {"stocks": []}, contents)
    before = path.read_bytes()

    def fail_replace(*_args):
        raise OSError("config is not writable")

    monkeypatch.setattr("src.core.config_store.os.replace", fail_replace)
    with pytest.raises(OSError, match="not writable"):
        migrate_legacy_cron(path, "/srv/trade")
    assert path.read_bytes() == before
    assert cron.contents == contents
    assert cron.installs == ["", contents]


def test_read_failure_never_modifies_config_or_cron(tmp_path, monkeypatch):
    path, cron = _setup(tmp_path, monkeypatch, {"stocks": []}, "# original\n")
    before = path.read_bytes()
    cron.read_error = True
    with pytest.raises(LegacyCronMigrationError, match="读取"):
        migrate_legacy_cron(path, "/srv/trade")
    assert path.read_bytes() == before
    assert not cron.installs


def test_uncertain_install_result_is_checked_and_rolled_back(tmp_path, monkeypatch):
    contents = "0 2 * * * cd /srv/trade && python3 main.py --optimize\n"
    path, cron = _setup(tmp_path, monkeypatch, {"stocks": []}, contents)
    before = path.read_bytes()

    def install_then_timeout(args, **kwargs):
        result = cron.run(args, **kwargs)
        if args == ["crontab", "-"] and kwargs["input"] == "":
            raise subprocess.TimeoutExpired(args, 15)
        return result

    monkeypatch.setattr(
        "src.core.schedule_migration.subprocess.run", install_then_timeout
    )
    with pytest.raises(subprocess.TimeoutExpired):
        migrate_legacy_cron(path, "/srv/trade")
    assert cron.contents == contents
    assert path.read_bytes() == before


def test_concurrent_crontab_edit_aborts_without_overwriting_it(tmp_path, monkeypatch):
    contents = "0 2 * * * cd /srv/trade && python3 main.py --optimize\n"
    path, cron = _setup(tmp_path, monkeypatch, {"stocks": []}, contents)
    before = path.read_bytes()
    reads = 0

    def change_before_install(args, **kwargs):
        nonlocal reads
        if args == ["crontab", "-l"]:
            reads += 1
            if reads == 2:
                cron.contents += "# concurrent edit\n"
        return cron.run(args, **kwargs)

    monkeypatch.setattr(
        "src.core.schedule_migration.subprocess.run", change_before_install
    )
    with pytest.raises(LegacyCronMigrationError, match="其他进程修改"):
        migrate_legacy_cron(path, "/srv/trade")
    assert cron.contents == contents + "# concurrent edit\n"
    assert path.read_bytes() == before
    assert not cron.installs
