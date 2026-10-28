"""Shared JSON commits use fixture files only; no services or transports run."""

from __future__ import annotations

import errno
import json
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core import report_execution, runtime_files, runtime_service


def windows_error(code):
    error = PermissionError(errno.EACCES, "fixture Windows sharing error")
    error.winerror = code
    return error


def temporary_files(path):
    return list(path.parent.glob(f".{path.name}.*.tmp"))


@pytest.mark.parametrize("attempts", [0, 9, True, 2.5, "8", None])
def test_retry_limit_is_explicit_and_cannot_be_expanded(tmp_path, attempts):
    path = tmp_path / "status.json"
    with pytest.raises(ValueError, match="integer from 1 to 8"):
        runtime_files.atomic_write_json(path, {}, replace_attempts=attempts)
    assert not list(tmp_path.iterdir())


def test_json_format_unicode_and_nonfinite_policy_are_preserved(tmp_path):
    path = tmp_path / "nested" / "status.json"
    payload = {"message": "神华", "nan": float("nan")}
    runtime_files.atomic_write_json(path, payload, indent=2)
    assert path.read_text(encoding="utf-8") == json.dumps(
        payload, ensure_ascii=False, indent=2
    )
    before = path.read_bytes()
    with pytest.raises(ValueError, match="Out of range float"):
        runtime_files.atomic_write_json(path, payload, allow_nan=False)
    assert path.read_bytes() == before
    assert not temporary_files(path)
    with pytest.raises(TypeError, match="not JSON serializable"):
        runtime_files.atomic_write_json(path, {"invalid": object()})
    assert path.read_bytes() == before


@pytest.mark.parametrize("code", [5, 32, 33])
def test_only_windows_access_sharing_lock_errors_are_retried(
    tmp_path, monkeypatch, code
):
    path = tmp_path / "status.json"
    original = Path.replace
    attempted, sleeps = [], []

    def replace(source, target):
        attempted.append(source)
        if len(attempted) < 3:
            raise windows_error(code)
        return original(source, target)

    monkeypatch.setattr(Path, "replace", replace)
    runtime_files.atomic_write_json(path, {"ready": True}, sleep=sleeps.append)
    assert len(attempted) == 3
    assert sleeps == [0.01, 0.02]
    assert json.loads(path.read_text()) == {"ready": True}
    assert not temporary_files(path)


@pytest.mark.parametrize("code", [None, 2, 123])
def test_other_errors_propagate_immediately_and_preserve_old_file(
    tmp_path, monkeypatch, code
):
    path = tmp_path / "status.json"
    path.write_text('{"old": true}', encoding="utf-8")
    error = windows_error(code)
    calls, sleeps = [], []

    def replace(source, target):
        calls.append((source, target))
        raise error

    monkeypatch.setattr(Path, "replace", replace)
    with pytest.raises(PermissionError) as raised:
        runtime_files.atomic_write_json(path, {"new": True}, sleep=sleeps.append)
    assert raised.value is error
    assert len(calls) == 1
    assert sleeps == []
    assert json.loads(path.read_text()) == {"old": True}
    assert not temporary_files(path)


def test_permanent_lock_has_eight_attempts_and_cleans_only_its_temporary(
    tmp_path, monkeypatch
):
    path = tmp_path / "status.json"
    path.write_text('{"old": true}', encoding="utf-8")
    unrelated = tmp_path / ".status.json.someone-else.tmp"
    unrelated.write_text("foreign temporary", encoding="utf-8")
    attempted, sleeps = [], []
    error = windows_error(32)

    def replace(source, target):
        attempted.append(source)
        raise error

    monkeypatch.setattr(Path, "replace", replace)
    with pytest.raises(PermissionError) as raised:
        runtime_files.atomic_write_json(path, {"new": True}, sleep=sleeps.append)
    assert raised.value is error
    assert len(attempted) == 8
    assert len(set(attempted)) == 1
    assert sleeps == [0.01, 0.02, 0.04, 0.08, 0.1, 0.1, 0.1]
    assert sum(sleeps) == pytest.approx(0.45)
    assert json.loads(path.read_text()) == {"old": True}
    assert temporary_files(path) == [unrelated]
    assert unrelated.read_text() == "foreign temporary"


def test_replace_and_cleanup_share_the_same_short_retry_budget(tmp_path, monkeypatch):
    path = tmp_path / "status.json"
    original_replace, original_unlink = Path.replace, Path.unlink
    replace_calls, unlink_calls, sleeps = [], [], []

    def replace(source, target):
        replace_calls.append(source)
        if len(replace_calls) < 4:
            raise windows_error(5)
        return original_replace(source, target)

    def unlink(source, *args, **kwargs):
        if source.name.startswith(".status.json."):
            unlink_calls.append(source)
            if len(unlink_calls) < 3:
                raise windows_error(33)
        return original_unlink(source, *args, **kwargs)

    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(Path, "unlink", unlink)
    runtime_files.atomic_write_json(path, {"ready": True}, sleep=sleeps.append)
    assert len(replace_calls) == 4
    assert len(unlink_calls) == 3
    assert sleeps == [0.01, 0.02, 0.04, 0.08, 0.1]
    assert not temporary_files(path)


def test_cleanup_permanent_failure_is_not_silently_reported_as_success(
    tmp_path, monkeypatch
):
    path = tmp_path / "status.json"
    original_unlink = Path.unlink
    sleeps = []

    def unlink(source, *args, **kwargs):
        if source.name.startswith(".status.json."):
            raise windows_error(32)
        return original_unlink(source, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(PermissionError):
        runtime_files.atomic_write_json(path, {"committed": True}, sleep=sleeps.append)
    assert json.loads(path.read_text()) == {"committed": True}
    assert len(sleeps) == 7
    assert sum(sleeps) == pytest.approx(0.45)


def test_simultaneous_writers_have_distinct_temporaries_and_complete_json(
    tmp_path, monkeypatch
):
    path = tmp_path / "status.json"
    runtime_files.atomic_write_json(path, {"writer": -1})
    original = Path.replace
    barrier = threading.Barrier(8)
    attempted, first_threads = [], set()
    guard = threading.Lock()

    def replace(source, target):
        with guard:
            first = threading.get_ident() not in first_threads
            first_threads.add(threading.get_ident())
            attempted.append(source)
        if first:
            barrier.wait(timeout=5)
        return original(source, target)

    monkeypatch.setattr(Path, "replace", replace)

    def write(index):
        runtime_files.atomic_write_json(
            path, {"writer": index, "values": [index] * 1024, "message": "神华"}
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(8)))
    assert len(set(attempted)) == 8
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["writer"] in range(8)
    assert saved["values"] == [saved["writer"]] * 1024
    assert saved["message"] == "神华"
    assert not temporary_files(path)


@pytest.mark.skipif(os.name != "nt", reason="ordinary Windows reader share-delete")
def test_real_windows_ordinary_reader_reproduces_original_replace_failure(tmp_path):
    target, source = tmp_path / "status.json", tmp_path / "replacement.json"
    target.write_text('{"old": true}', encoding="utf-8")
    source.write_text('{"new": true}', encoding="utf-8")
    with target.open("rb"), pytest.raises(PermissionError) as raised:
        source.replace(target)
    assert raised.value.winerror in {5, 32, 33}
    assert json.loads(target.read_text()) == {"old": True}
    assert source.exists()


def host_writer(path, health=None):
    # Avoid constructing an ExecutionStore or starting a scheduler/Bot.
    service = runtime_service.RuntimeService.__new__(runtime_service.RuntimeService)
    service.status_path = path
    service.execution_runtime = SimpleNamespace(
        health=lambda: {"ready": True} if health is None else health
    )
    service.failure, service.health_reporter = None, None
    service.bots, service.threads = {}, {}
    service.scheduling, service.started_at = True, "fixture-start"
    service._owner_evidence, service._diagnostics = {}, []
    service._planned_stop, service._accepted_stop_request_id = False, None
    service._phase = "constructed"
    return lambda: service._write_status("running")


@pytest.mark.skipif(os.name != "nt", reason="ordinary Windows reader share-delete")
@pytest.mark.parametrize("caller", ["helper", "host", "report"])
def test_real_windows_reader_short_release_recovers_through_each_caller(
    tmp_path, monkeypatch, caller
):
    path = tmp_path / "status.json"
    path.write_text('{"old": true}', encoding="utf-8")
    original = Path.replace
    observed = threading.Event()
    actual_errors = []

    def replace(source, target):
        observed.set()
        try:
            return original(source, target)
        except PermissionError as exc:
            actual_errors.append(exc.winerror)
            raise

    monkeypatch.setattr(Path, "replace", replace)
    writers = {
        "helper": lambda: runtime_files.atomic_write_json(path, {"new": True}),
        "host": host_writer(path),
        "report": lambda: report_execution._write_json(path, {"new": True}),
    }
    reader = path.open("rb")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(writers[caller])
        try:
            assert observed.wait(timeout=3)
            time.sleep(0.06)
        finally:
            reader.close()
        future.result(timeout=3)
    assert actual_errors and set(actual_errors) <= {5, 32, 33}
    saved = json.loads(path.read_text())
    assert saved.get("new") is True or saved.get("state") == "running"
    assert not temporary_files(path)
    assert not list(tmp_path.rglob("*.sqlite3"))


@pytest.mark.skipif(os.name != "nt", reason="ordinary Windows reader share-delete")
def test_real_windows_permanent_reader_fails_within_bounded_time(tmp_path, monkeypatch):
    path = tmp_path / "status.json"
    path.write_text('{"old": true}', encoding="utf-8")
    original, attempts = Path.replace, []

    def replace(source, target):
        attempts.append(source)
        return original(source, target)

    monkeypatch.setattr(Path, "replace", replace)
    started = time.monotonic()
    with path.open("rb"), pytest.raises(PermissionError) as raised:
        runtime_files.atomic_write_json(path, {"new": True})
    elapsed = time.monotonic() - started
    assert raised.value.winerror in {5, 32, 33}
    assert len(attempts) == 8
    assert 0.4 <= elapsed < 2.5
    assert json.loads(path.read_text()) == {"old": True}
    assert not temporary_files(path)


def test_report_and_host_keep_their_distinct_nonfinite_json_policies(tmp_path):
    path = tmp_path / "result.json"
    report_execution._write_json(path, {"name": "神华"})
    assert path.read_text(encoding="utf-8") == '{"name": "神华"}'
    with pytest.raises(ValueError, match="Out of range float"):
        report_execution._write_json(path, {"value": float("nan")})
    write = host_writer(
        tmp_path / "host.json", {"ready": True, "name": "神华", "value": float("nan")}
    )
    write()
    saved = json.loads((tmp_path / "host.json").read_text(encoding="utf-8"))
    assert saved["state"] == "running"
    assert saved["scheduler_enabled"] is True
    assert saved["execution"]["ready"] is True
    assert saved["execution"]["name"] == "神华"
    assert math.isnan(saved["execution"]["value"])
    assert "神华" in (tmp_path / "host.json").read_text(encoding="utf-8")
    assert saved["started_at"] == "fixture-start"
