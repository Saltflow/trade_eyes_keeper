"""Offline acceptance for persistent Baostock limits and SDK session guards."""

from __future__ import annotations

import importlib
import json
import multiprocessing
import socket
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest


class _Clock:
    def __init__(self, now=None):
        self.now = now or datetime(2026, 9, 22, tzinfo=timezone.utc).timestamp()
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        assert seconds >= 0
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds):
        self.now += seconds


def _config(state_dir, **overrides):
    settings = {
        "state_dir": str(state_dir),
        "min_request_interval_seconds": 2,
        "daily_request_budget": 40_000,
        "retry_backoff_initial_seconds": 60,
        "retry_backoff_max_seconds": 900,
        "connection_lock_timeout_seconds": 10,
    }
    settings.update(overrides)
    return {"provider_access": {"baostock": settings}}


@pytest.fixture
def access():
    return importlib.import_module("src.data.baostock_access")


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def reject(*_args, **_kwargs):
        raise AssertionError("this test must never create a network connection")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


def _controller(access, config, clock):
    return access.BaostockAccessController(config, clock=clock, sleep=clock.sleep)


class _Socket:
    def __init__(self):
        self.close_calls = 0
        self.timeout = None

    def close(self):
        self.close_calls += 1

    def settimeout(self, value):
        self.timeout = value

    def gettimeout(self):
        return self.timeout


class _SdkTransport:
    """Use real SDK parsing/pagination, replacing only connect and transport."""

    def __init__(self, monkeypatch, *, failure=None):
        self.module = pytest.importorskip("baostock")
        self.socketutil = importlib.import_module("baostock.util.socketutil")
        self.context = importlib.import_module("baostock.common.context")
        self.constants = importlib.import_module("baostock.common.contants")
        self.headers = importlib.import_module("baostock.data.messageheader")
        self.socket = _Socket()
        self.calls = []
        self.connect_calls = 0
        self.failure = failure
        monkeypatch.setattr(self.constants, "BAOSTOCK_PER_PAGE_COUNT", 2)
        monkeypatch.setattr(self.context, "default_socket", None, raising=False)
        monkeypatch.setattr(self.context, "user_id", "offline", raising=False)

        def connect(_socketutil):
            self.connect_calls += 1
            self.context.default_socket = self.socket

        self.transport = self.send
        monkeypatch.setattr(self.socketutil.SocketUtil, "connect", connect)
        monkeypatch.setattr(self.socketutil, "send_msg", self.transport)

    def response(self, message_type, fields):
        body = self.constants.MESSAGE_SPLIT.join(str(field) for field in fields)
        return self.headers.to_message_header(message_type, len(body)) + body + "\n"

    def send(self, message):
        fields = message[self.constants.MESSAGE_HEADER_LENGTH :].split(
            self.constants.MESSAGE_SPLIT
        )
        method = fields[0]
        page = fields[2] if method.startswith("query_") else None
        label = f"{method}:{page}" if page is not None else method
        self.calls.append(label)
        if self.failure == (label, "blacklist"):
            return self.response("04", ["10002004", "IP已经被拉入黑名单"])
        if self.failure == (label, "empty"):
            return None
        if self.failure == (label, "malformed_short"):
            return "incomplete response"
        if self.failure == (label, "malformed_success"):
            return self.response("01", ["0", "success"])
        if self.failure == (label, "malformed_page"):
            return self.response("96", ["0", "success", method, "anonymous"])
        if method in ("login", "logout"):
            return self.response("01", ["0", "success", method, "anonymous"])
        assert method == "query_history_k_data_plus"
        rows = (
            [["2026-01-05", "10"], ["2026-01-06", "11"]]
            if page == "1"
            else [["2026-01-07", "12"]]
        )
        return self.response(
            "96",
            [
                "0",
                "success",
                method,
                "anonymous",
                page,
                "2",
                json.dumps({"record": rows}),
                "sh.600000",
                "date,close",
                "2026-01-05",
                "2026-01-07",
                "d",
                "3",
            ],
        )


def _query(module):
    return module.query_history_k_data_plus(
        "sh.600000", "date,close", start_date="2026-01-05", end_date="2026-01-07"
    )


def test_policy_has_one_explicit_validated_config_source(access, tmp_path):
    defaults = access.BaostockAccessPolicy.from_config({})
    assert defaults.min_request_interval_seconds == 2
    assert defaults.daily_request_budget == 40_000
    assert defaults.retry_backoff_initial_seconds == 60
    assert defaults.retry_backoff_max_seconds == 900
    assert defaults.connection_lock_timeout_seconds == 1200
    configured = access.BaostockAccessPolicy.from_config(
        _config(tmp_path, daily_request_budget=20, min_request_interval_seconds=3)
    )
    assert configured.state_dir == tmp_path.resolve()
    assert configured.daily_request_budget == 20
    assert configured.min_request_interval_seconds == 3


@pytest.mark.parametrize(
    "override",
    [
        {"min_request_interval_seconds": 0},
        {"daily_request_budget": 40001},
        {"daily_request_budget": 2.5},
        {"daily_request_budget": True},
        {"retry_backoff_max_seconds": 30},
        {"connection_lock_timeout_seconds": float("nan")},
        {"misspelled_budget": 1},
    ],
)
def test_policy_rejects_invalid_or_ignored_controls(access, tmp_path, override):
    with pytest.raises((ValueError, TypeError)):
        access.BaostockAccessPolicy.from_config(_config(tmp_path, **override))


def test_request_spacing_and_quota_persist_across_controllers(access, tmp_path):
    clock = _Clock()
    config = _config(tmp_path)
    first = _controller(access, config, clock)
    first.reserve_request()
    second = _controller(access, config, clock)
    second.reserve_request()

    assert clock.sleeps == [2]
    assert first.status()["request_count"] == 2
    assert second.status()["remaining"] == 39998
    first.record_success()
    assert _controller(access, config, clock).status()["request_count"] == 2


def test_daily_quota_resets_at_china_midnight_only(access, tmp_path):
    clock = _Clock(datetime(2026, 9, 21, 15, 59, 56, tzinfo=timezone.utc).timestamp())
    config = _config(tmp_path, daily_request_budget=2)
    controller = _controller(access, config, clock)
    controller.reserve_request()
    controller.reserve_request()
    with pytest.raises(access.BaostockAccessBlocked) as caught:
        _controller(access, config, clock).reserve_request()
    assert caught.value.reason == "daily_budget"
    assert caught.value.retry_at == "2026-09-22T00:00:00+08:00"
    assert controller.status()["request_count"] == 2

    clock.advance(3)
    next_day = _controller(access, config, clock)
    next_day.reserve_request()

    assert next_day.status()["day"] == "2026-09-22"
    assert next_day.status()["request_count"] == 1


def test_blacklist_does_not_expire_by_time_or_success(access, tmp_path):
    clock = _Clock()
    config = _config(tmp_path)
    controller = _controller(access, config, clock)
    retry_at = datetime.fromtimestamp(clock() + 60, timezone.utc).isoformat()
    controller.record_blacklist("official restriction", retry_at=retry_at)
    controller.record_success()
    clock.advance(3 * 86400)

    with pytest.raises(access.BaostockAccessBlocked) as caught:
        _controller(access, config, clock).reserve_request()
    assert caught.value.reason == "blacklist"
    assert caught.value.to_dict()["blocked"] is True
    assert controller.status()["release_confirmation_required"] is True


def test_verified_official_release_keeps_used_daily_budget(access, tmp_path):
    clock = _Clock()
    config = _config(tmp_path, daily_request_budget=2)
    controller = _controller(access, config, clock)
    controller.reserve_request()
    controller.reserve_request()
    controller.record_blacklist("active blacklist")
    clock.advance(1)

    status = controller.apply_official_status(
        {"stats": {"total": 1, "data": [{"effectiveStatus": 0}]}},
        checked_at=clock(),
    )

    assert status["request_count"] == 2
    assert status["reason"] == "daily_budget"
    with pytest.raises(access.BaostockAccessBlocked) as caught:
        _controller(access, config, clock).reserve_request()
    assert caught.value.reason == "daily_budget"


def test_migration_budget_hold_survives_release_without_fabricating_usage_and_expires_at_midnight(
    access, tmp_path
):
    clock = _Clock(datetime(2026, 9, 22, 15, 59, 56, tzinfo=timezone.utc).timestamp())
    config = _config(tmp_path)
    controller = _controller(access, config, clock)
    controller.reserve_request()
    controller.hold_daily_budget("earlier usage was not metered")
    controller.record_blacklist("active restriction")

    status = controller.apply_official_status(
        {"stats": {"total": 0, "data": []}}, checked_at=clock()
    )

    assert status["reason"] == "daily_budget"
    assert status["request_count"] == 1
    assert status["remaining"] == 0
    assert status["retry_at"] == "2026-09-23T00:00:00+08:00"
    clock.advance(5)
    restarted = _controller(access, config, clock)
    restarted.check_available()
    assert restarted.status()["request_count"] == 0
    restarted.reserve_request()
    assert restarted.status()["request_count"] == 1


def test_official_refresh_checks_all_rows_and_does_not_infer_release_from_date(
    access, tmp_path
):
    clock = _Clock()
    controller = _controller(access, _config(tmp_path), clock)

    status = controller.apply_official_status(
        {
            "stats": {
                "total": 2,
                "data": [
                    {"effectiveStatus": 0},
                    {"effectiveStatus": 1, "pendingReleaseDate": "2026-09-22 07:00:00"},
                ],
            }
        },
        checked_at=clock(),
    )

    assert status["reason"] == "blacklist"
    assert status["blocked"] is True
    with pytest.raises(access.BaostockAccessBlocked):
        controller.check_available()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"stats": {"total": 2, "data": [{"effectiveStatus": 0}]}},
        {"stats": {"total": 1, "data": [{"effectiveStatus": "0"}]}},
        {"stats": {"total": 1, "data": [{}]}},
    ],
)
def test_incomplete_official_refresh_cannot_clear_blacklist(access, tmp_path, payload):
    clock = _Clock()
    controller = _controller(access, _config(tmp_path), clock)
    controller.record_blacklist("active blacklist")
    with pytest.raises(ValueError):
        controller.apply_official_status(payload, checked_at=clock())
    assert controller.status()["reason"] == "blacklist"


def test_official_response_started_before_new_ban_cannot_clear_it(access, tmp_path):
    clock = _Clock()
    controller = _controller(access, _config(tmp_path), clock)
    checked_at = clock()
    clock.advance(1)
    controller.record_blacklist("newer than the refresh request")

    status = controller.apply_official_status(
        {"stats": {"total": 0, "data": []}}, checked_at=checked_at
    )

    assert status["reason"] == "blacklist"


def test_transient_backoff_is_persistent_bounded_and_resets_after_success(
    access, tmp_path
):
    clock = _Clock()
    config = _config(tmp_path)
    controller = _controller(access, config, clock)
    controller.reserve_request()
    for delay in (60, 120, 240, 480, 900, 900):
        retry_at = controller.record_transient("offline network failure")
        assert datetime.fromisoformat(retry_at).timestamp() - clock() == delay
        with pytest.raises(access.BaostockTransientError) as caught:
            _controller(access, config, clock).check_available()
        assert caught.value.to_dict()["blocked"] is False
        assert caught.value.reason == "cooldown"
        clock.advance(delay)
        controller.check_available()
    controller.record_success()
    retry_at = controller.record_transient("a new independent failure")
    assert datetime.fromisoformat(retry_at).timestamp() - clock() == 60
    assert controller.status()["request_count"] == 1


def test_corrupt_persisted_state_fails_closed(access, tmp_path):
    controller = _controller(access, _config(tmp_path), _Clock())
    controller.reserve_request()
    controller.state_path.write_text('{"contract": "broken"}', encoding="utf-8")

    with pytest.raises(access.BaostockAccessBlocked) as caught:
        controller.reserve_request()
    assert caught.value.reason == "state_invalid"
    assert controller.status()["blocked"] is True


def test_real_sdk_login_query_pagination_logout_share_one_wire_budget(
    access, tmp_path, monkeypatch
):
    sdk = _SdkTransport(monkeypatch)
    clock = _Clock()
    controller = _controller(access, _config(tmp_path, daily_request_budget=4), clock)
    previous_timeout = socket.getdefaulttimeout()

    with access.guarded_baostock_session(
        sdk.module, 7, controller=controller
    ) as module:
        result = _query(module)
        rows = []
        while result.next():
            rows.append(result.get_row_data())

    assert rows == [["2026-01-05", "10"], ["2026-01-06", "11"], ["2026-01-07", "12"]]
    assert sdk.calls == [
        "login",
        "query_history_k_data_plus:1",
        "query_history_k_data_plus:2",
        "logout",
    ]
    assert clock.sleeps == [2, 2, 2]
    assert sdk.socket.close_calls >= 1
    assert sdk.socketutil.send_msg is sdk.transport
    assert socket.getdefaulttimeout() == previous_timeout
    with pytest.raises(access.BaostockAccessBlocked):
        controller.check_available()


@pytest.mark.parametrize("failure_point", ["login", "query_history_k_data_plus:2"])
def test_real_sdk_blacklist_stops_iteration_and_suppresses_logout(
    access, tmp_path, monkeypatch, failure_point
):
    sdk = _SdkTransport(monkeypatch, failure=(failure_point, "blacklist"))
    clock = _Clock()
    config = _config(tmp_path)
    controller = _controller(access, config, clock)

    session = access.guarded_baostock_session(sdk.module, 7, controller=controller)
    with pytest.raises(access.BaostockAccessBlocked), session as module:
        result = _query(module)
        while result.next():
            result.get_row_data()

    assert sdk.calls[-1] == failure_point
    assert "logout" not in sdk.calls
    assert sdk.socket.close_calls >= 1
    assert sdk.socketutil.send_msg is sdk.transport
    controller.record_success()
    clock.advance(3 * 86400)
    with pytest.raises(access.BaostockAccessBlocked):
        _controller(access, config, clock).check_available()


def test_persisted_blacklist_prevents_sdk_connect_or_login(
    access, tmp_path, monkeypatch
):
    sdk = _SdkTransport(monkeypatch)
    clock = _Clock()
    config = _config(tmp_path)
    _controller(access, config, clock).record_blacklist("official blacklist")

    session = access.guarded_baostock_session(
        sdk.module, 7, controller=_controller(access, config, clock)
    )
    with pytest.raises(access.BaostockAccessBlocked), session:
        pytest.fail("a blocked session must never yield")

    assert sdk.connect_calls == 0
    assert sdk.calls == []
    assert sdk.socketutil.send_msg is sdk.transport


def test_real_sdk_empty_response_is_transient_and_does_not_become_short_data(
    access, tmp_path, monkeypatch
):
    sdk = _SdkTransport(monkeypatch, failure=("query_history_k_data_plus:2", "empty"))
    controller = _controller(access, _config(tmp_path), _Clock())

    session = access.guarded_baostock_session(sdk.module, 7, controller=controller)
    with pytest.raises(access.BaostockTransientError), session as module:
        result = _query(module)
        while result.next():
            result.get_row_data()

    assert "logout" not in sdk.calls
    assert sdk.socket.close_calls >= 1
    assert sdk.socketutil.send_msg is sdk.transport


@pytest.mark.parametrize("failure", ["malformed_short", "malformed_success"])
def test_malformed_wire_response_becomes_transient(
    access, tmp_path, monkeypatch, failure
):
    sdk = _SdkTransport(monkeypatch, failure=("login", failure))
    controller = _controller(access, _config(tmp_path), _Clock())

    session = access.guarded_baostock_session(sdk.module, 7, controller=controller)
    with pytest.raises(access.BaostockTransientError), session:
        pytest.fail("malformed login response must not yield a session")

    assert sdk.calls == ["login"]
    assert controller.status()["reason"] == "cooldown"
    assert sdk.socket.close_calls >= 1
    assert sdk.socketutil.send_msg is sdk.transport


@pytest.mark.parametrize(
    "failure_point", ["query_history_k_data_plus:1", "query_history_k_data_plus:2"]
)
def test_sdk_query_and_next_page_parse_errors_set_transient_cooldown(
    access, tmp_path, monkeypatch, failure_point
):
    sdk = _SdkTransport(monkeypatch, failure=(failure_point, "malformed_page"))
    controller = _controller(access, _config(tmp_path), _Clock())

    session = access.guarded_baostock_session(sdk.module, 7, controller=controller)
    with pytest.raises(access.BaostockTransientError), session as module:
        result = _query(module)
        while result.next():
            result.get_row_data()

    assert sdk.calls[-1] == failure_point
    assert "logout" not in sdk.calls
    assert controller.status()["reason"] == "cooldown"
    assert sdk.socket.close_calls >= 1
    assert sdk.socketutil.send_msg is sdk.transport


def test_sdk_recv_eof_terminates_after_one_read_and_closes_socket(
    access, tmp_path, monkeypatch
):
    transport = importlib.import_module("baostock.util.socketutil")
    actual_send = transport.send_msg
    sdk = _SdkTransport(monkeypatch)
    reads = []

    def recv(_size):
        reads.append(1)
        if len(reads) > 1:
            raise AssertionError("SDK would spin forever on repeated EOF")
        return b""

    monkeypatch.setattr(sdk.socket, "send", lambda payload: len(payload), raising=False)
    monkeypatch.setattr(sdk.socket, "recv", recv, raising=False)
    monkeypatch.setattr(transport, "send_msg", actual_send)
    controller = _controller(access, _config(tmp_path), _Clock())

    session = access.guarded_baostock_session(sdk.module, 7, controller=controller)
    with pytest.raises(access.BaostockTransientError), session:
        pytest.fail("EOF must not produce a usable session")

    assert len(reads) == 1
    assert sdk.socket.close_calls >= 1
    assert getattr(sdk.context, "default_socket", None) is None
    assert transport.send_msg is actual_send


def test_injected_adapter_pagination_error_is_not_swallowed(access, tmp_path):
    calls = []

    class Cursor:
        error_code = "0"
        error_msg = ""
        count = 0

        def next(self):
            self.count += 1
            if self.count == 1:
                return True
            self.error_code, self.error_msg = "10001011", "IP黑名单"
            return False

        def get_row_data(self):
            return ["10"]

    def login():
        calls.append("login")
        return SimpleNamespace(error_code="0", error_msg="")

    def logout():
        calls.append("logout")
        return SimpleNamespace(error_code="0", error_msg="")

    def query():
        calls.append("query")
        return Cursor()

    module = SimpleNamespace(login=login, logout=logout, query_rows=query)
    controller = _controller(access, _config(tmp_path), _Clock())

    session = access.guarded_baostock_session(module, 7, controller=controller)
    with pytest.raises(access.BaostockAccessBlocked), session as guarded:
        result = guarded.query_rows()
        while result.next():
            result.get_row_data()

    assert calls == ["login", "query"]
    assert controller.status()["reason"] == "blacklist"
    assert controller.status()["request_count"] == 2


def test_new_blacklist_prevents_a_previously_successful_cursor_from_requesting_next_page(
    access, tmp_path, monkeypatch
):
    sdk = _SdkTransport(monkeypatch)
    controller = _controller(access, _config(tmp_path), _Clock())

    session = access.guarded_baostock_session(sdk.module, 7, controller=controller)
    with pytest.raises(access.BaostockAccessBlocked), session as module:
        result = _query(module)
        for _ in range(2):
            assert result.next()
            result.get_row_data()
        controller.record_blacklist("restriction observed by another worker")
        result.next()

    assert sdk.calls == ["login", "query_history_k_data_plus:1"]
    assert sdk.socket.close_calls >= 1


def _hold_session_lock(config, acquired):
    from src.data.baostock_access import BaostockAccessController

    with BaostockAccessController(config).session_lock():
        acquired.set()
        # Never terminate a process waiting on a shared Event: its Condition
        # waiter could otherwise prevent another process from calling set().
        time.sleep(30)


def test_session_lock_excludes_other_process_and_releases_after_kill(tmp_path):
    context = multiprocessing.get_context("spawn")
    first_ready, second_ready = context.Event(), context.Event()
    config = _config(tmp_path, connection_lock_timeout_seconds=10)
    first = context.Process(target=_hold_session_lock, args=(config, first_ready))
    second = context.Process(target=_hold_session_lock, args=(config, second_ready))
    processes = [first, second]
    try:
        first.start()
        assert first_ready.wait(10), f"first worker failed: {first.exitcode}"
        second.start()
        assert not second_ready.wait(0.3), "session lock was not exclusive"
        first.terminate()
        first.join(5)
        assert not first.is_alive()
        assert second_ready.wait(10), f"lock survived dead holder: {second.exitcode}"
    finally:
        for process in processes:
            if process.pid is None:
                continue
            if process.is_alive():
                process.terminate()
            process.join(5)
