"""Exercise the supervisor with fake children and real checkpoint files only."""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from scripts import backfill_reference_universe as batch_cli
from scripts import run_reference_universe_isolated as isolated
from src.data.reference_universe_backfill import (
    REFERENCE_MANIFEST_CONTRACT,
    ReferenceUniverseBackfillService,
    load_reference_manifest,
)

END = date(2026, 9, 21)


def _success(code, end=END):
    return {
        "code": code,
        "market": "a_share",
        "instrument_type": "equity",
        "market_history": {
            "status": "success",
            "actual_end": end.isoformat(),
            "requested_window_coverage": "full",
        },
        "statements": {"status": "success", "field_availability": {}},
    }


def _supervisor(
    tmp_path,
    *,
    codes=("000001", "000002", "000003"),
    access_settings=None,
    **kwargs,
):
    config_path = tmp_path / "config.yaml"
    config = {
        "point_in_time_data": {"history_years": 9, "official_pdf_recent_years": 3},
        "provider_access": {
            "baostock": {
                "state_dir": str(tmp_path / "provider_state"),
                **(access_settings or {}),
            }
        },
    }
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "contract": REFERENCE_MANIFEST_CONTRACT,
                "as_of": END.isoformat(),
                "companies": [{"code": code} for code in codes],
            }
        ),
        encoding="utf-8",
    )
    return isolated.IsolatedReferenceSupervisor(
        batch_cli.scoped_config(config, history_years=5, official_pdf_recent_years=5),
        load_reference_manifest(manifest_path),
        config_path=config_path,
        manifest_path=manifest_path,
        output_dir=tmp_path / "dataset",
        evaluation_date=END,
        **kwargs,
    )


def _seed(supervisor, code, row=None, **changes):
    checkpoint = supervisor.service._checkpoint_payload(
        batch_id=supervisor.batch_id,
        fingerprint=supervisor.fingerprint,
        row=row or _success(code),
    )
    checkpoint.update(changes)
    isolated._atomic_json(supervisor.checkpoint_dir / f"{code}.json", checkpoint)


class FakeChildren:
    def __init__(
        self, monkeypatch, *, outcomes=None, step=1, source_access=None, after_exit=None
    ):
        self.outcomes = outcomes or {}
        self.source_access = source_access or {}
        self.after_exit = after_exit
        self.step = step
        self.clock = 0.0
        self.children = []
        self.commands = []
        self.launch_times = []
        self.sleep_durations = []
        self.live = 0
        self.peak = 0
        self.progress = []
        self.interrupt = False
        wall_clock_start = datetime.now(isolated.SHANGHAI_TZ)
        parent = self

        class VirtualDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                instant = wall_clock_start + timedelta(seconds=parent.clock)
                return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

        monkeypatch.setattr(isolated, "datetime", VirtualDateTime)
        atomic_write = isolated._atomic_json

        def record(path, payload):
            atomic_write(path, payload)
            if path.name == "supervisor_progress.json":
                self.progress.append(json.loads(path.read_text(encoding="utf-8")))

        monkeypatch.setattr(isolated, "_atomic_json", record)
        monkeypatch.setattr(
            isolated,
            "time",
            SimpleNamespace(monotonic=lambda: self.clock, sleep=self.sleep),
        )
        monkeypatch.setattr(
            isolated,
            "subprocess",
            SimpleNamespace(Popen=self.launch, DEVNULL=-3, CREATE_NO_WINDOW=0x08000000),
        )
        # Any final service.run()/fetch would violate the bounded-attempt contract.
        monkeypatch.setattr(
            ReferenceUniverseBackfillService, "run", self.unexpected_fetch
        )

    @staticmethod
    def unexpected_fetch(*args, **kwargs):
        raise AssertionError("supervisor must never enter a fetching report path")

    def sleep(self, duration):
        self.sleep_durations.append(duration)
        if self.interrupt:
            raise KeyboardInterrupt
        self.clock += self.step

    def launch(self, command, **kwargs):
        self.commands.append(command)
        self.launch_times.append(self.clock)
        code = command[command.index("--codes") + 1]
        outcomes = self.outcomes.setdefault(code, ["success"])
        outcome = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        if outcome == "launch_error":
            raise OSError("mock spawn failure")
        child = FakeChild(self, command, outcome)
        self.children.append(child)
        self.live += 1
        self.peak = max(self.peak, self.live)
        return child


class FakeChild:
    def __init__(self, parent, command, outcome):
        self.parent, self.command, self.outcome = parent, command, outcome
        self.pid = 8000 + len(parent.children)
        self.polls = 0
        self.returncode = None
        self.killed = False
        self.waited = False
        self.finished_at = None

    def arg(self, name):
        return self.command[self.command.index(name) + 1]

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        self.polls += 1
        if self.outcome == "timeout" or self.polls < 3:
            return None
        self.returncode = 1 if self.outcome in {"failed", "bad_exit"} else 0
        self.parent.live -= 1
        if self.outcome == "missing":
            return self.exited()
        raw = yaml.safe_load(Path(self.arg("--config")).read_text(encoding="utf-8"))
        config = batch_cli.scoped_config(
            raw,
            history_years=int(self.arg("--history-years")),
            official_pdf_recent_years=int(self.arg("--official-pdf-recent-years")),
        )
        service = ReferenceUniverseBackfillService(
            config,
            load_reference_manifest(self.arg("--manifest")),
            output_dir=self.arg("--output-dir"),
            workers=1,
        )
        end = date.fromisoformat(self.arg("--evaluation-date"))
        batch_id, fingerprint = service._batch_identity(end)
        code = self.arg("--codes")
        row = _success(code, date(2020, 1, 1) if self.outcome == "stale" else end)
        if self.outcome == "failed":
            row["statements"] = {"status": "failed", "reason": "mock provider failure"}
        if code in self.parent.source_access:
            row["source_access"] = self.parent.source_access[code]
        checkpoint = service._checkpoint_payload(
            batch_id=batch_id, fingerprint=fingerprint, row=row
        )
        isolated._atomic_json(
            service.output_dir
            / "reference_batches"
            / batch_id
            / "codes"
            / f"{code}.json",
            checkpoint,
        )
        return self.exited()

    def exited(self):
        self.finished_at = self.parent.clock
        if self.parent.after_exit:
            self.parent.after_exit(self)
        return self.returncode

    def kill(self):
        self.killed = True
        if self.returncode is None:
            self.parent.live -= 1
        self.returncode = -9

    def wait(self, timeout):
        assert timeout == 10
        assert self.returncode is not None
        self.waited = True
        return self.returncode


def test_batch_scoped_overrides_do_not_mutate_config():
    base = {"point_in_time_data": {"history_years": 9, "official_pdf_recent_years": 3}}
    scoped = batch_cli.scoped_config(base, history_years=5, official_pdf_recent_years=5)
    assert scoped["point_in_time_data"] == {
        "history_years": 5,
        "official_pdf_recent_years": 5,
    }
    assert base["point_in_time_data"] == {
        "history_years": 9,
        "official_pdf_recent_years": 3,
    }


@pytest.mark.parametrize("field", ["history_years", "official_pdf_recent_years"])
def test_batch_rejects_nonpositive_overrides(field):
    with pytest.raises(ValueError, match="positive"):
        batch_cli.scoped_config({}, **{field: 0})


def test_direct_cli_routes_both_overrides_without_config_write(tmp_path, monkeypatch):
    supervisor = _supervisor(tmp_path, codes=("000001",))
    original = supervisor.config_path.read_bytes()
    captured = {}

    class Service:
        def __init__(self, config, manifest, **kwargs):
            captured["config"] = config
            captured.update(kwargs)

        def run(self, **kwargs):
            captured["run"] = kwargs
            return {"state": "complete"}

    monkeypatch.setattr(batch_cli, "ReferenceUniverseBackfillService", Service)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "backfill_reference_universe.py",
            "--config",
            str(supervisor.config_path),
            "--manifest",
            str(supervisor.manifest_path),
            "--output-dir",
            str(tmp_path / "direct"),
            "--history-years",
            "5",
            "--official-pdf-recent-years",
            "5",
            "--evaluation-date",
            END.isoformat(),
            "--no-retry-failures",
        ],
    )
    assert batch_cli.main() == 0
    assert captured["config"]["point_in_time_data"]["history_years"] == 5
    assert captured["config"]["point_in_time_data"]["official_pdf_recent_years"] == 5
    assert captured["workers"] == 2  # Preserve the direct batch CLI default.
    assert captured["run"]["retry_failures"] is False
    assert supervisor.config_path.read_bytes() == original


@pytest.mark.parametrize("explicit", [False, True])
def test_supervisor_cli_routes_options_and_preserves_defaults(
    tmp_path, monkeypatch, explicit
):
    supervisor = _supervisor(tmp_path, codes=("000001",))
    original = supervisor.config_path.read_bytes()
    captured = {}

    class Supervisor:
        def __init__(self, config, manifest, **kwargs):
            captured["config"] = config
            captured.update(kwargs)

        def run(self):
            return {
                "state": "complete",
                "batch_id": "mock",
                "failed_codes": [],
                "output_file": "mock-report.json",
            }

    argv = [
        "run_reference_universe_isolated.py",
        "--config",
        str(supervisor.config_path),
        "--manifest",
        str(supervisor.manifest_path),
        "--output-dir",
        str(tmp_path / "cli"),
        "--evaluation-date",
        END.isoformat(),
    ]
    if explicit:
        argv.extend(
            [
                "--history-years",
                "5",
                "--official-pdf-recent-years",
                "5",
                "--workers",
                "2",
                "--attempts-per-run",
                "1",
                "--company-timeout-seconds",
                "90",
            ]
        )
    monkeypatch.setattr(isolated, "IsolatedReferenceSupervisor", Supervisor)
    monkeypatch.setattr(sys, "argv", argv)
    assert isolated.main() == 0
    settings = captured["config"]["point_in_time_data"]
    assert settings["history_years"] == (5 if explicit else 9)
    assert settings["official_pdf_recent_years"] == (5 if explicit else 3)
    assert captured["workers"] == (2 if explicit else 1)
    assert captured["attempts_per_run"] == (1 if explicit else 2)
    assert captured["company_timeout_seconds"] == (90 if explicit else 1200)
    assert captured["evaluation_date"] == END
    assert supervisor.config_path.read_bytes() == original


def test_two_children_bound_concurrency_and_publish_pids(tmp_path, monkeypatch):
    supervisor = _supervisor(tmp_path, workers=2)
    original = supervisor.config_path.read_bytes()
    fake = FakeChildren(monkeypatch)
    report = supervisor.run()
    assert report["state"] == "complete"
    assert report["worker_processes"] == 2
    assert report["checkpoint_count"] == 3
    assert fake.peak == 2 and fake.live == 0
    assert len({child.pid for child in fake.children}) == 3
    assert all(len(p["running"]) <= 2 for p in fake.progress)
    active = next(p for p in fake.progress if len(p["running"]) == 2)
    assert active["running_pids"] == {"000001": 8000, "000002": 8001}
    assert fake.progress[-1]["running"] == []
    assert fake.progress[-1]["completed"] == 3
    for command in fake.commands:
        assert command[command.index("--workers") + 1] == "1"
        assert command[command.index("--history-years") + 1] == "5"
        assert command[command.index("--official-pdf-recent-years") + 1] == "5"
    assert supervisor.config_path.read_bytes() == original


def test_parallel_resume_reuses_only_matching_current_success(tmp_path, monkeypatch):
    supervisor = _supervisor(
        tmp_path, codes=("000001", "000002", "000003", "000004"), workers=2
    )
    _seed(supervisor, "000001")
    supervisor._failure("000002", RuntimeError("previous failure"))
    _seed(supervisor, "000003", fingerprint="other-batch")
    _seed(supervisor, "000004", row=_success("000004", date(2020, 1, 1)))
    fake = FakeChildren(monkeypatch)
    report = supervisor.run()
    assert report["state"] == "complete"
    assert report["reused_checkpoint_count"] == 1
    assert {child.arg("--codes") for child in fake.children} == {
        "000002",
        "000003",
        "000004",
    }
    assert fake.peak == 2
    again = supervisor.run()
    assert again["reused_checkpoint_count"] == 4
    assert len(fake.children) == 3


def test_failed_attempt_retries_in_a_fresh_child_then_succeeds(tmp_path, monkeypatch):
    supervisor = _supervisor(tmp_path, codes=("000001",), workers=1)
    fake = FakeChildren(monkeypatch, outcomes={"000001": ["failed", "success"]})
    report = supervisor.run()
    assert report["state"] == "complete"
    assert len(fake.children) == 2
    assert fake.children[0].pid != fake.children[1].pid
    assert fake.peak == 1
    assert fake.launch_times[1] - fake.children[0].finished_at == 60
    assert max(fake.sleep_durations) <= 0.2


@pytest.mark.parametrize(
    "outcome", ["timeout", "failed", "missing", "stale", "bad_exit", "launch_error"]
)
def test_attempt_budget_has_no_hidden_final_retry(tmp_path, monkeypatch, outcome):
    supervisor = _supervisor(
        tmp_path, codes=("000001",), attempts_per_run=2, company_timeout_seconds=60
    )
    fake = FakeChildren(
        monkeypatch,
        outcomes={"000001": [outcome]},
        step=65 if outcome == "timeout" else 1,
    )
    report = supervisor.run()
    assert report["state"] == "partial"
    assert report["failed_codes"] == ["000001"]
    assert len(fake.commands) == 2
    assert fake.live == 0
    assert supervisor._checkpoint("000001")["status"] == "failed"
    if outcome == "timeout":
        assert all(child.killed and child.waited for child in fake.children)
        assert (
            "exceeded 60 seconds"
            in report["instruments"][0]["market_history"]["reason"]
        )


def test_interrupted_supervisor_stops_all_children_and_keeps_resume_state(
    tmp_path, monkeypatch
):
    supervisor = _supervisor(tmp_path, workers=2)
    fake = FakeChildren(
        monkeypatch, outcomes={"000001": ["timeout"], "000002": ["timeout"]}
    )
    fake.interrupt = True
    report = supervisor.run()
    assert report["state"] == "interrupted"
    assert len(fake.commands) == 2
    assert all(child.killed and child.waited for child in fake.children)
    assert fake.progress[-1]["state"] == "interrupted"
    assert fake.progress[-1]["running"] == []
    assert fake.progress[-1]["pending"] == 1


def test_output_root_lock_prevents_simultaneous_supervisors(tmp_path, monkeypatch):
    supervisor = _supervisor(tmp_path)
    fake = FakeChildren(monkeypatch)
    dataset_lock = isolated._dataset_lock(supervisor.service.output_dir)
    with dataset_lock, pytest.raises(
        RuntimeError, match="another reference supervisor"
    ):
        supervisor.run()
    assert not fake.commands
    # The OS lock is released when its owner exits the context.
    assert supervisor.run()["state"] == "complete"


def test_invalid_checkpoint_json_is_retried_without_crashing(tmp_path, monkeypatch):
    supervisor = _supervisor(tmp_path, codes=("000001",))
    supervisor.checkpoint_dir.mkdir(parents=True)
    (supervisor.checkpoint_dir / "000001.json").write_text("[]", encoding="utf-8")
    fake = FakeChildren(monkeypatch)
    assert supervisor.run()["state"] == "complete"
    assert len(fake.children) == 1


@pytest.mark.parametrize("workers", [0, 5])
def test_supervisor_rejects_unbounded_workers(tmp_path, workers):
    with pytest.raises(ValueError, match="between 1 and 4"):
        _supervisor(tmp_path, workers=workers)


def _access_status(*, blocked=False, reason=None, retry_at=None):
    return {
        "source": "baostock",
        "blocked": blocked,
        "reason": reason,
        "retry_at": retry_at,
        "request_count": 12,
        "remaining": 39988,
    }


@pytest.mark.parametrize("reason", ["blacklist", "daily_budget", "state_invalid"])
def test_hard_block_preflight_preserves_success_and_pending_checkpoints(
    tmp_path, monkeypatch, reason
):
    supervisor = _supervisor(tmp_path, access_settings={"daily_request_budget": 1})
    _seed(supervisor, "000001")
    failed = _success("000002")
    failed["statements"] = {"status": "failed", "reason": "previous provider failure"}
    _seed(supervisor, "000002", row=failed)
    preserved = {
        path: path.read_bytes() for path in supervisor.checkpoint_dir.glob("*.json")
    }
    controller = supervisor.access_controller
    if reason == "blacklist":
        controller.record_blacklist("mock blacklist")
    elif reason == "daily_budget":
        controller.reserve_request()  # Charges local state only; sends nothing.
    else:
        controller.state_path.parent.mkdir(parents=True)
        controller.state_path.write_text("{}", encoding="utf-8")
    fake = FakeChildren(monkeypatch)

    report = supervisor.run()

    assert report["state"] == "source_blocked"
    assert report["source_access"]["reason"] == reason
    assert report["requested_company_count"] == 3
    assert report["checkpoint_count"] == 2
    assert report["completed_company_count"] == 1
    assert report["reused_checkpoint_count"] == 1
    assert report["pending_company_count"] == 2
    assert report["pending_codes"] == ["000002", "000003"]
    assert report["pending_failed_codes"] == report["failed_codes"] == ["000002"]
    assert report["instruments"][1] == failed
    assert not fake.commands
    assert all(path.read_bytes() == payload for path, payload in preserved.items())
    assert not (supervisor.checkpoint_dir / "000003.json").exists()
    for name in ("progress.json", "supervisor_progress.json", "report.json"):
        payload = json.loads((supervisor.batch_dir / name).read_text(encoding="utf-8"))
        assert payload["state"] == "source_blocked"
        assert payload["pending_codes"] == ["000002", "000003"]
        assert payload["source_access"]["reason"] == reason


def test_gate_is_rechecked_between_child_launches(tmp_path, monkeypatch):
    supervisor = _supervisor(tmp_path, workers=2)
    supervisor._failure("000001", RuntimeError("keep previous failure"))
    _seed(supervisor, "000003")
    previous = (supervisor.checkpoint_dir / "000001.json").read_bytes()
    fake = FakeChildren(monkeypatch, outcomes={"000001": ["timeout"]})
    monkeypatch.setattr(
        supervisor.access_controller,
        "status",
        lambda: _access_status(blocked=bool(fake.commands), reason="blacklist"),
    )

    report = supervisor.run()

    assert report["state"] == "source_blocked"
    assert len(fake.commands) == 1
    assert fake.children[0].killed and fake.children[0].waited
    assert fake.live == 0
    assert (supervisor.checkpoint_dir / "000001.json").read_bytes() == previous
    assert not (supervisor.checkpoint_dir / "000002.json").exists()
    assert report["pending_codes"] == ["000001", "000002"]
    assert fake.progress[-1]["running"] == []


@pytest.mark.parametrize("outcome", ["missing", "success"])
def test_gate_is_rechecked_after_exit_before_writing_synthetic_failure(
    tmp_path, monkeypatch, outcome
):
    supervisor = _supervisor(tmp_path)
    _seed(supervisor, "000003")
    fake = FakeChildren(
        monkeypatch,
        outcomes={"000001": [outcome]},
        after_exit=lambda child: supervisor.access_controller.record_blacklist(
            "provider blocked during child exit"
        ),
    )

    report = supervisor.run()

    assert report["state"] == "source_blocked"
    assert len(fake.commands) == 1
    assert fake.children[0].waited
    assert not report["failed_codes"]
    assert not (supervisor.checkpoint_dir / "000002.json").exists()
    if outcome == "missing":
        assert not (supervisor.checkpoint_dir / "000001.json").exists()
        assert report["pending_codes"] == ["000001", "000002"]
    else:
        assert supervisor._checkpoint("000001")["status"] == "success"
        assert report["pending_codes"] == ["000002"]


def test_new_child_source_block_stops_peers_without_replacing_their_checkpoints(
    tmp_path, monkeypatch
):
    supervisor = _supervisor(tmp_path, workers=2)
    supervisor._failure("000002", RuntimeError("original peer failure"))
    _seed(supervisor, "000003")
    previous = (supervisor.checkpoint_dir / "000002.json").read_bytes()
    fake = FakeChildren(
        monkeypatch,
        outcomes={"000001": ["failed"], "000002": ["timeout"]},
        source_access={
            "000001": _access_status(blocked=True, reason="blacklist"),
        },
    )

    report = supervisor.run()

    assert report["state"] == "source_blocked"
    assert report["source_access"]["reason"] == "blacklist"
    assert report["failed_codes"] == report["pending_codes"] == ["000001", "000002"]
    assert len(fake.commands) == 2
    assert all(child.waited for child in fake.children)
    assert fake.children[1].killed
    assert fake.live == 0
    assert (supervisor.checkpoint_dir / "000002.json").read_bytes() == previous
    assert supervisor._checkpoint("000001")["row"]["source_access"]["blocked"]


def test_released_gate_does_not_reapply_old_checkpoint_block(tmp_path, monkeypatch):
    supervisor = _supervisor(tmp_path, codes=("000001",))
    failed = _success("000001")
    failed["statements"]["status"] = "failed"
    failed["source_access"] = _access_status(blocked=True, reason="blacklist")
    _seed(supervisor, "000001", row=failed)
    fake = FakeChildren(monkeypatch, outcomes={"000001": ["missing", "success"]})

    assert supervisor.run()["state"] == "complete"
    assert len(fake.commands) == 2


def test_provider_cooldown_delays_dispatch_without_launching_children(
    tmp_path, monkeypatch
):
    supervisor = _supervisor(tmp_path, codes=("000001",))
    fake = FakeChildren(monkeypatch)
    retry_at = (
        isolated.datetime.now(isolated.SHANGHAI_TZ) + timedelta(seconds=5)
    ).isoformat()
    monkeypatch.setattr(
        supervisor.access_controller,
        "status",
        lambda: (
            _access_status(reason="cooldown", retry_at=retry_at)
            if fake.clock < 5
            else _access_status()
        ),
    )

    assert supervisor.run()["state"] == "complete"
    assert fake.launch_times == [5]
    assert any(p["state"] == "waiting_retry" for p in fake.progress)
    assert max(fake.sleep_durations) <= 0.2


def test_controller_lock_contention_delays_dispatch(tmp_path, monkeypatch):
    supervisor = _supervisor(tmp_path, codes=("000001",))
    fake = FakeChildren(monkeypatch)

    def status():
        if fake.clock < 5:
            raise isolated.BaostockTransientError(
                "mock busy state lock", reason="lock_busy"
            )
        return _access_status()

    monkeypatch.setattr(supervisor.access_controller, "status", status)
    assert supervisor.run()["state"] == "complete"
    assert fake.launch_times == [5]


def test_retry_backoff_doubles_to_cap_and_leaves_other_companies_runnable(
    tmp_path, monkeypatch
):
    supervisor = _supervisor(
        tmp_path,
        codes=("000001", "000002"),
        attempts_per_run=5,
        access_settings={
            "retry_backoff_initial_seconds": 2,
            "retry_backoff_max_seconds": 5,
        },
    )
    fake = FakeChildren(monkeypatch, outcomes={"000001": ["failed"]})

    report = supervisor.run()

    assert report["state"] == "partial"
    codes = [child.arg("--codes") for child in fake.children]
    assert codes[:3] == ["000001", "000002", "000001"]
    failed_attempts = [
        child for child in fake.children if child.arg("--codes") == "000001"
    ]
    launches = [
        at
        for at, child in zip(fake.launch_times, fake.children)
        if child.arg("--codes") == "000001"
    ]
    gaps = [at - child.finished_at for at, child in zip(launches[1:], failed_attempts)]
    assert gaps[0] >= 2  # The other company is allowed to finish during this wait.
    assert gaps[1:] == [4, 5, 5]
    assert max(fake.sleep_durations) <= 0.2


def test_transient_child_retry_at_extends_minimum_backoff(tmp_path, monkeypatch):
    supervisor = _supervisor(tmp_path, codes=("000001",))
    fake = FakeChildren(monkeypatch, outcomes={"000001": ["failed", "success"]})
    fake.source_access["000001"] = _access_status(
        reason="transient",
        retry_at=(
            isolated.datetime.now(isolated.SHANGHAI_TZ) + timedelta(seconds=120)
        ).isoformat(),
    )

    assert supervisor.run()["state"] == "complete"
    assert fake.launch_times == [0, 120]
    assert len([p for p in fake.progress if p["state"] == "waiting_retry"]) <= 2


def test_resume_honors_transient_retry_at_in_existing_checkpoint(tmp_path, monkeypatch):
    supervisor = _supervisor(tmp_path, codes=("000001",))
    fake = FakeChildren(monkeypatch)
    failed = _success("000001")
    failed["statements"]["status"] = "failed"
    failed["source_access"] = _access_status(
        reason="transient",
        retry_at=(
            isolated.datetime.now(isolated.SHANGHAI_TZ) + timedelta(seconds=5)
        ).isoformat(),
    )
    _seed(supervisor, "000001", row=failed)

    assert supervisor.run()["state"] == "complete"
    assert fake.launch_times == [5]


def test_block_during_retry_wait_preserves_last_real_failure(tmp_path, monkeypatch):
    supervisor = _supervisor(tmp_path, codes=("000001",))
    committed = []
    path = supervisor.checkpoint_dir / "000001.json"
    fake = FakeChildren(
        monkeypatch,
        outcomes={"000001": ["failed"]},
        after_exit=lambda child: committed.append(path.read_bytes()),
    )
    monkeypatch.setattr(
        supervisor.access_controller,
        "status",
        lambda: _access_status(blocked=fake.clock >= 10, reason="daily_budget"),
    )

    report = supervisor.run()

    assert report["state"] == "source_blocked"
    assert report["pending_codes"] == ["000001"]
    assert report["failed_codes"] == ["000001"]
    assert len(fake.commands) == 1
    assert path.read_bytes() == committed[0]
    assert fake.clock == 10


def test_retry_wait_is_interruptible_without_an_extra_attempt(tmp_path, monkeypatch):
    supervisor = _supervisor(tmp_path, codes=("000001",))
    fake = FakeChildren(monkeypatch, outcomes={"000001": ["failed"]})
    fake.after_exit = lambda child: setattr(fake, "interrupt", True)

    report = supervisor.run()

    assert report["state"] == "interrupted"
    assert report["pending_codes"] == ["000001"]
    assert report["retry_schedule"][0]["next_attempt"] == 2
    assert len(fake.commands) == 1
    assert report["instruments"][0]["market_history"]["status"] == "success"


def test_block_seen_while_stopping_child_preserves_pending_checkpoint(
    tmp_path, monkeypatch
):
    supervisor = _supervisor(tmp_path, codes=("000001",))
    supervisor._failure("000001", RuntimeError("original failure"))
    path = supervisor.checkpoint_dir / "000001.json"
    original = path.read_bytes()
    fake = FakeChildren(monkeypatch, outcomes={"000001": ["timeout"]})
    fake.interrupt = True
    stop_child = supervisor._stop_child

    def stop_and_record_block(process):
        stop_child(process)
        supervisor.access_controller.record_blacklist("block noticed during stop")

    monkeypatch.setattr(supervisor, "_stop_child", stop_and_record_block)

    report = supervisor.run()

    assert report["state"] == "source_blocked"
    assert report["pending_codes"] == ["000001"]
    assert len(fake.commands) == 1
    assert fake.children[0].killed and fake.children[0].waited
    assert path.read_bytes() == original
