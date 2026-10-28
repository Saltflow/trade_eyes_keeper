"""Single-group API polling isolation without live Feishu or model requests."""

import json
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import requests
import yaml

from scripts.personal_monitor_transport import message_identity
from scripts.run_personal_monitor_feishu_group_test import (
    FeishuReadError,
    GroupAPI,
    GroupPoller,
    GroupState,
    RuntimeStatus,
    group_identity,
    item_to_event,
    load_settings,
    read_credentials,
    read_runtime_status,
    read_with_retry,
    run_host,
    run_probe,
    safe_error,
    validate_production_filter,
)

STAMP = 1792828800000
NOW = datetime(2026, 10, 24, tzinfo=timezone.utc)


@pytest.fixture
def configured(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yaml").write_text(
        yaml.safe_dump({"interactive": {"feishu": {"app_id": "cli_prod123"}}}),
        encoding="utf-8",
    )
    (tmp_path / "config" / ".env").write_text(
        "FEISHU_APP_SECRET=private-secret\nDEEPSEEK_API_KEY=private-key\nUNRELATED_KEY=must-not-read\n",
        encoding="utf-8",
    )
    raw = {
        "test_mode": True,
        "chat_id": "oc_test",
        "allowed_sender_ids": ["ou_self"],
        "workspace_name": "test_group",
    }
    path = tmp_path / "group.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    settings = load_settings(path, root=tmp_path, environ={})
    return tmp_path, path, raw, settings


def item(identity="om_1", *, created=STAMP + 123, text="查看持仓", **extra):
    return {
        "message_id": identity,
        "chat_id": "oc_test",
        "msg_type": "text",
        "create_time": str(created),
        "update_time": str(created),
        "deleted": False,
        "updated": False,
        "sender": {"id_type": "open_id", "id": "ou_self", "sender_type": "user"},
        "body": {"content": json.dumps({"text": text}, ensure_ascii=False)},
        **extra,
    }


def proof(configured, **extra):
    root, _path, _raw, settings = configured
    audit = root / "data" / "deployment_audit"
    audit.mkdir(parents=True, exist_ok=True)
    path = audit / "verified.json"
    path.write_text(
        json.dumps(
            {
                "app_id": settings["app_id"],
                "chat_id": settings["chat_id"],
                "group_no_mention_ignored": True,
                "handler_sha256": "a" * 64,
                "checked_at": NOW.isoformat(),
                **extra,
            }
        ),
        encoding="utf-8",
    )
    return path


def response(data, *, code=0, http=200):
    result = Mock(status_code=http)
    result.json.return_value = {"code": code, "data": data, "msg": "private-api-body"}
    return result


def test_credentials_are_whitelisted_in_memory_without_dotenv(configured, monkeypatch):
    root, _path, _raw, settings = configured
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    with patch("dotenv.load_dotenv", side_effect=AssertionError("no dotenv")):
        values = read_credentials(root, {})
    assert values == {
        "app_id": "cli_prod123",
        "app_secret": "private-secret",
        "api_key": "private-key",
    }
    assert "FEISHU_APP_SECRET" not in __import__("os").environ
    assert settings["allowed_chat_ids"] == ["oc_test"]
    assert (
        settings["workspace"]
        == root / "data" / "personal_monitor_feishu_group_test" / "test_group"
    )
    assert not settings["workspace"].exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("chat_id", "*"),
        ("chat_id", ["oc_a", "oc_b"]),
        ("allowed_sender_ids", []),
        ("allowed_sender_ids", ["*"]),
        ("workspace_name", "../runtime"),
        ("app_secret", "forbidden-inline-secret"),
        ("poll_seconds", 0),
        ("market_check_times", ["99:99"]),
    ],
)
def test_settings_fail_closed(configured, field, value):
    root, path, raw, _settings = configured
    raw[field] = value
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        load_settings(path, root=root, environ={})


@pytest.mark.parametrize(
    "change",
    [
        {"app_id": "cli_other"},
        {"chat_id": "oc_other"},
        {"group_no_mention_ignored": False},
        {"handler_sha256": "bad"},
        {"checked_at": (NOW - timedelta(days=2)).isoformat()},
        {"checked_at": "2026-10-24T00:00:00"},
    ],
)
def test_production_gate_checks_identity_hash_and_fresh_time(configured, change):
    root, _path, _raw, settings = configured
    path = proof(configured, **change)
    with pytest.raises(ValueError):
        validate_production_filter(path, settings, root=root, now=NOW)


def test_production_gate_requires_controlled_path_and_returns_content_hash(configured):
    root, _path, _raw, settings = configured
    path = proof(configured)
    assert len(validate_production_filter(path, settings, root=root, now=NOW)) == 64
    unsafe = root / "uncontrolled.json"
    unsafe.write_bytes(path.read_bytes())
    with pytest.raises(ValueError):
        validate_production_filter(unsafe, settings, root=root, now=NOW)


def test_probe_reports_230027_without_db_model_send_or_websocket(configured, capsys):
    _root, _path, _raw, settings = configured
    transport = Mock()
    transport.get_tenant_token.return_value = "private-token"
    with patch(
        "scripts.run_personal_monitor_feishu_group_test.make_transport",
        return_value=transport,
    ), patch(
        "scripts.run_personal_monitor_feishu_group_test.requests.get",
        side_effect=[
            response({"chat_mode": "group"}),
            response({}, code=230027, http=400),
        ],
    ) as get, patch(
        "scripts.run_personal_monitor_feishu_group_test.HistoryBot",
        side_effect=AssertionError("probe must not construct bridge"),
    ), patch(
        "multiprocessing.get_context", side_effect=AssertionError("no second websocket")
    ):
        assert run_probe(settings) == 2
    transport.send_message.assert_not_called()
    transport.send_personal_message.assert_not_called()
    assert not settings["workspace"].exists()
    output = capsys.readouterr().out
    assert '"code": 230027' in output
    assert "private-token" not in output and "private-api-body" not in output
    assert get.call_args.kwargs["params"]["container_id"] == "oc_test"
    assert get.call_args.kwargs["allow_redirects"] is False


def test_missing_gate_blocks_normal_before_api_or_database(configured):
    root, _path, _raw, settings = configured
    transport = Mock()
    with patch(
        "scripts.run_personal_monitor_feishu_group_test.make_transport",
        return_value=transport,
    ), patch(
        "scripts.run_personal_monitor_feishu_group_test.GroupAPI"
    ) as api, pytest.raises(ValueError):
        run_host(settings, proof_path=None, root=root)
    api.return_value.probe.assert_not_called()
    assert not settings["workspace"].exists()


@pytest.mark.parametrize(
    "alter",
    [
        {"chat_id": "oc_other"},
        {"deleted": True},
        {"updated": True},
        {"mentions": [{"id": "ou_bot"}]},
        {"sender": {"id_type": "open_id", "id": "ou_other", "sender_type": "user"}},
        {"sender": {"id_type": "app_id", "id": "cli_bot", "sender_type": "app"}},
        {"sender": {"id_type": "user_id", "id": "ou_self", "sender_type": "user"}},
        {"create_time": ""},
        {"update_time": str(STAMP + 999)},
    ],
)
def test_non_human_edited_or_outside_messages_are_ignored(alter):
    assert item_to_event(item(**alter), "oc_test", ["ou_self"]) is None


@pytest.mark.parametrize(
    "text", ["@机器人 查看状态", "a@example.com", " /daily", "/optimize"]
)
def test_mentions_and_legacy_slash_never_enter_bridge(text):
    assert item_to_event(item(text=text), "oc_test", ["ou_self"]) is None


def test_real_api_fields_are_retained_as_group_source_and_original_time():
    raw = item(
        msg_type="image", body={"content": json.dumps({"image_key": "img_actual"})}
    )
    event = item_to_event(raw, "oc_test", ["ou_self"])
    assert event["header"]["source"] == "feishu_history_api"
    owner, chat, identity, message = group_identity(event)
    assert (owner, chat, identity) == ("ou_self", "oc_test", "om_1")
    assert (
        message["chat_type"] == "group" and message["create_time"] == raw["create_time"]
    )
    assert message["update_time"] == raw["update_time"]
    assert (
        message["message_type"] == "image"
        and message["content"] == raw["body"]["content"]
    )
    assert message_identity(event) is None  # original private helper stays private


def test_rich_post_at_is_ignored_even_when_api_mentions_array_is_missing():
    content = {
        "zh_cn": {
            "content": [
                [
                    {"tag": "at", "user_id": "ou_bot"},
                    {"tag": "img", "image_key": "img_actual"},
                ]
            ]
        }
    }
    assert (
        item_to_event(
            item(msg_type="post", body={"content": json.dumps(content)}),
            "oc_test",
            ["ou_self"],
        )
        is None
    )


def test_pages_keep_frozen_window_and_same_millisecond_messages(configured):
    _root, _path, _raw, settings = configured
    transport = Mock()
    transport.get_tenant_token.return_value = "private-token"
    get = Mock(
        side_effect=[
            response(
                {
                    "items": [item("om_b"), item("om_a")],
                    "has_more": True,
                    "page_token": "page2",
                }
            ),
            response({"items": [item("om_c"), item("om_a")], "has_more": False}),
        ]
    )
    api = GroupAPI(transport, settings["chat_id"], get=get)
    rows = api.collect_window(STAMP, STAMP + 10000)
    assert [row["message_id"] for row in rows] == ["om_a", "om_b", "om_c"]
    first, second = [call.kwargs["params"] for call in get.call_args_list]
    assert {key: value for key, value in second.items() if key != "page_token"} == first
    assert first["start_time"] == str(STAMP // 1000)
    assert second["page_token"] == "page2"


def test_poll_rescans_session_to_catch_late_index_and_deduplicates_restart(configured):
    _root, _path, _raw, settings = configured
    state = GroupState(settings, started_ms=STAMP + 100)
    api, bridge = Mock(), Mock()
    bridge.process.return_value = "sent"
    api.collect_window.side_effect = [
        [item("om_first"), item("om_before", created=STAMP + 99)],
        [item("om_first"), item("om_late_index", created=STAMP + 123)],
        [item("om_first"), item("om_late_index", created=STAMP + 123)],
    ]
    poller = GroupPoller(settings, api, state, bridge)
    assert poller.poll_once(STAMP + 10000)["processed"] == 1
    assert poller.poll_once(STAMP + 20000)["processed"] == 1
    restart = GroupState(settings, started_ms=STAMP + 99999)
    assert restart.origin_ms == STAMP + 100
    assert (
        GroupPoller(settings, api, restart, bridge).poll_once(STAMP + 30000)[
            "processed"
        ]
        == 0
    )
    assert bridge.process.call_count == 2
    assert all(
        call.args[0] == STAMP + 100 for call in api.collect_window.call_args_list
    )
    assert b"private-secret" not in state.path.read_bytes()


def test_exact_window_boundary_waits_for_next_window(configured):
    _root, _path, _raw, settings = configured
    state = GroupState(settings, started_ms=STAMP)
    api, bridge = Mock(), Mock()
    bridge.process.return_value = "sent"
    api.collect_window.return_value = [item("om_boundary", created=STAMP + 8000)]
    poller = GroupPoller(settings, api, state, bridge)
    assert poller.poll_once(STAMP + 10000)["processed"] == 0
    assert poller.poll_once(STAMP + 11000)["processed"] == 1


def test_page_error_never_processes_partial_window_or_advances(configured):
    _root, _path, _raw, settings = configured
    state = GroupState(settings, started_ms=STAMP)
    api, bridge = Mock(), Mock()
    api.collect_window.side_effect = FeishuReadError("invalid_history_pagination")
    with pytest.raises(FeishuReadError):
        GroupPoller(settings, api, state, bridge).poll_once(STAMP + 10000)
    bridge.process.assert_not_called()
    with sqlite3.connect(state.path) as db:
        assert (
            db.execute("SELECT completed_until_ms FROM group_binding").fetchone()[0]
            == STAMP
        )


def test_interrupted_message_is_unknown_not_reexecuted(configured):
    _root, _path, _raw, settings = configured
    state = GroupState(settings, started_ms=STAMP)
    assert state.claim(item())
    restarted = GroupState(settings, started_ms=STAMP + 1)
    assert not restarted.claim(item())
    with sqlite3.connect(state.path) as db:
        assert (
            db.execute("SELECT state FROM group_messages").fetchone()[0]
            == "interrupted_unknown"
        )


def test_workspace_is_bound_to_exact_app_and_chat(configured):
    _root, _path, _raw, settings = configured
    GroupState(settings, started_ms=STAMP)
    for alteration in ({"app_id": "cli_other"}, {"chat_id": "oc_other"}):
        with pytest.raises(ValueError):
            GroupState(dict(settings, **alteration), started_ms=STAMP)


def test_malformed_or_looping_page_is_rejected(configured):
    _root, _path, _raw, settings = configured
    api = GroupAPI(Mock(), settings["chat_id"])
    api.history_page = Mock(
        return_value={"items": [item()], "has_more": True, "page_token": "same"}
    )
    with pytest.raises(FeishuReadError, match="pagination"):
        api.collect_window(STAMP, STAMP + 10000)


def test_api_page_reaches_real_personal_bridge_without_relabeling_or_production_write(
    configured,
):
    from scripts.personal_monitor_shared_test import SharedPersonalTestBridge
    from src.core.execution import ExecutionRuntime, ExecutionStore

    root, _path, _raw, settings = configured
    before = (root / "config" / "config.yaml").read_bytes()
    transport, client = Mock(), Mock()
    transport.get_tenant_token.return_value = "offline-fixture-token"
    transport.gate.is_allowed.side_effect = lambda chat: chat == "oc_test"
    transport.send_message.return_value = (True, "local capture")
    transport.send_personal_message.return_value = {
        "outcome": "sent",
        "platform_message_id": "om_reply",
    }
    client.complete.side_effect = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "account1",
                    "type": "function",
                    "function": {
                        "name": "personal_monitor_account",
                        "arguments": json.dumps({"account": "群测试账户"}),
                    },
                }
            ],
        },
        {"role": "assistant", "content": "已创建"},
    ]
    state = GroupState(settings, started_ms=STAMP)
    execution = ExecutionRuntime(ExecutionStore(settings["workspace"] / "test.sqlite3"))
    bridge = SharedPersonalTestBridge(
        settings,
        transport=transport,
        client=client,
        identity_resolver=group_identity,
        execution_runtime=execution,
    )
    api = GroupAPI(
        transport,
        "oc_test",
        get=Mock(
            return_value=response(
                {"items": [item(text="创建账户群测试账户")], "has_more": False}
            )
        ),
    )
    poller = GroupPoller(settings, api, state, bridge)
    execution.start()
    try:
        assert poller.poll_once(STAMP + 10000)["processed"] == 1
        deadline = time.monotonic() + 10
        while (
            not bridge.store.status("ou_self")["accounts"]
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        assert bridge.store.status("ou_self")["accounts"][0]["account"] == "群测试账户"
        assert poller.poll_once(STAMP + 20000)["processed"] == 0
    finally:
        execution.stop(drain=True, timeout=10)
        bridge.stop()
    assert (root / "config" / "config.yaml").read_bytes() == before
    assert b"private-secret" not in state.path.read_bytes()
    assert (
        transport.send_personal_message.call_count + transport.send_message.call_count
        >= 1
    )
    assert not execution.running


@pytest.mark.parametrize("http", [429, 500, 502, 503, 599])
def test_temporary_http_failures_retry_even_with_non_json_proxy_body(http):
    transport = Mock()
    transport.get_tenant_token.return_value = "private-token"
    reply = Mock(status_code=http)
    reply.json.side_effect = ValueError("private proxy body")
    api = GroupAPI(transport, "oc_test", get=Mock(return_value=reply))
    with pytest.raises(FeishuReadError) as caught:
        api.history_page(STAMP, STAMP + 10000)
    assert caught.value.retryable and caught.value.http_status == http
    assert "private" not in str(caught.value)


@pytest.mark.parametrize("error", [requests.Timeout, requests.ConnectionError])
def test_temporary_network_failures_are_classified_without_exception_body(error):
    transport = Mock()
    transport.get_tenant_token.return_value = "private-token"
    api = GroupAPI(transport, "oc_test", get=Mock(side_effect=error("private-token")))
    with pytest.raises(FeishuReadError) as caught:
        api.history_page(STAMP, STAMP + 10000)
    assert caught.value.retryable
    assert "private" not in str(caught.value)


def test_stable_non_json_403_retains_http_code_without_retry():
    transport = Mock()
    transport.get_tenant_token.return_value = "private-token"
    reply = Mock(status_code=403)
    reply.json.side_effect = ValueError("secret body")
    api = GroupAPI(transport, "oc_test", get=Mock(return_value=reply))
    with pytest.raises(FeishuReadError) as caught:
        api.history_page(STAMP, STAMP + 10000)
    assert caught.value.http_status == 403 and not caught.value.retryable


def test_scope_403_fails_closed_without_backoff_or_losing_safe_code(configured, capsys):
    _root, _path, _raw, settings = configured
    transport = Mock()
    transport.get_tenant_token.return_value = "private-token"
    get = Mock(return_value=response({}, code=99991672, http=403))
    api = GroupAPI(transport, "oc_test", get=get)
    wait, guard = Mock(return_value=False), Mock()
    status = RuntimeStatus(settings)
    with pytest.raises(FeishuReadError) as caught:
        read_with_retry(
            lambda: api.history_page(STAMP, STAMP + 10000),
            validate=guard,
            status=status,
            stop_event=threading.Event(),
            wait=wait,
        )
    assert caught.value.http_status == 403 and caught.value.code == 99991672
    assert not caught.value.retryable
    wait.assert_not_called()
    assert get.call_count == 1
    assert "private" not in capsys.readouterr().err


def test_read_backoff_is_capped_and_recovery_retains_safe_last_error(configured):
    _root, _path, _raw, settings = configured
    read = Mock(
        side_effect=[FeishuReadError("history", http_status=503, retryable=True)] * 9
        + ["ok"]
    )
    wait = Mock(return_value=False)
    status = RuntimeStatus(settings)
    result = read_with_retry(
        read, validate=Mock(), status=status, stop_event=threading.Event(), wait=wait
    )
    assert result == "ok"
    assert [call.args[0] for call in wait.call_args_list] == [
        2,
        4,
        8,
        16,
        32,
        60,
        60,
        60,
        60,
    ]
    saved = json.loads(status.path.read_text(encoding="utf-8"))
    assert saved["state"] == "running" and saved["consecutive_read_failures"] == 0
    assert saved["last_error"]["http_status"] == 503 and saved["last_success_at"]
    assert "private" not in status.path.read_text(encoding="utf-8")
    restarted = RuntimeStatus(settings)
    assert restarted.payload["last_error"] == saved["last_error"]


def test_page_network_loss_retries_whole_frozen_window_without_repeat_notifications(
    configured,
):
    _root, _path, _raw, settings = configured
    state = GroupState(settings, started_ms=STAMP)
    transport = Mock()
    transport.get_tenant_token.return_value = "private-token"
    first_page = response(
        {"items": [item("om_existing")], "has_more": True, "page_token": "second"}
    )
    complete_page = response(
        {"items": [item("om_new"), item("om_existing")], "has_more": False}
    )
    get = Mock(
        side_effect=[
            response({"items": [item("om_existing")], "has_more": False}),
            first_page,
            requests.ConnectionError("secret upstream diagnostic"),
            first_page,
            complete_page,
        ]
    )
    api = GroupAPI(transport, "oc_test", get=get)
    bridge = Mock()
    bridge.process.return_value = "sent"
    poller = GroupPoller(settings, api, state, bridge)
    assert poller.poll_once(STAMP + 10000)["processed"] == 1
    before = (
        sqlite3.connect(state.path)
        .execute("SELECT completed_until_ms FROM group_binding")
        .fetchone()[0]
    )

    def wait(_seconds):
        # Page 1 has not leaked any business work and the window is untouched.
        assert bridge.process.call_count == 1
        with sqlite3.connect(state.path) as db:
            assert (
                db.execute("SELECT completed_until_ms FROM group_binding").fetchone()[0]
                == before
            )
        return False

    result = read_with_retry(
        lambda: poller.poll_once(STAMP + 20000),
        validate=Mock(),
        status=RuntimeStatus(settings),
        stop_event=threading.Event(),
        wait=wait,
    )
    assert result["processed"] == 1
    assert bridge.process.call_count == 2
    assert [
        call.args[0]["event"]["message"]["message_id"]
        for call in bridge.process.call_args_list
    ] == ["om_existing", "om_new"]
    # Failed/retried windows keep identical time bounds, including page 2.
    params = [call.kwargs["params"] for call in get.call_args_list][1:]
    assert len({(p["start_time"], p["end_time"]) for p in params}) == 1


def test_proof_expiry_during_backoff_prevents_another_get(configured):
    _root, _path, _raw, settings = configured
    read = Mock(side_effect=FeishuReadError("history", retryable=True))
    guard = Mock(side_effect=[None, ValueError("expired")])
    with pytest.raises(ValueError, match="expired"):
        read_with_retry(
            read,
            validate=guard,
            status=RuntimeStatus(settings),
            stop_event=threading.Event(),
            wait=Mock(return_value=False),
        )
    assert read.call_count == 1


@pytest.mark.parametrize(
    "identity,age,state",
    [
        ({"alive": False}, 1, "not_running"),
        ({"alive": True, "started_at": 2}, 1, "process_identity_mismatch"),
        (
            {"alive": True, "started_at": 1, "argv": ["unrelated.py"], "cwd": "."},
            1,
            "process_identity_mismatch",
        ),
        (None, 181, "stale"),
        (None, -10, "stale"),
        (None, 1, "running"),
    ],
)
def test_runtime_status_checks_pid_identity_and_freshness(
    configured, identity, age, state
):
    import scripts.run_personal_monitor_feishu_group_test as module

    _, _, _, settings = configured
    settings["workspace"].mkdir(parents=True)
    path = settings["workspace"] / "poller_status.json"
    path.write_text(
        json.dumps(
            {
                "state": "running",
                "pid": 123,
                "process_started_at": 1,
                "updated_at": (NOW - timedelta(seconds=age)).isoformat(),
            }
        )
    )
    original = path.read_bytes()
    actual = identity or {
        "alive": True,
        "started_at": 1,
        "argv": [module.__file__],
        "cwd": ".",
    }
    result = read_runtime_status(settings, inspector=Mock(return_value=actual), now=NOW)
    assert result["state"] == state
    assert result["healthy"] is (state == "running")
    assert path.read_bytes() == original


def test_runtime_status_unavailable_process_inspection_fails_closed(configured):
    _, _, _, settings = configured
    RuntimeStatus(settings)
    result = read_runtime_status(
        settings, inspector=Mock(side_effect=OSError("private"))
    )
    assert result["state"] == "process_unverified" and not result["healthy"]
    assert "private" not in json.dumps(result)


def test_status_cli_never_starts_receiver_or_connects_feishu(configured, capsys):
    import scripts.run_personal_monitor_feishu_group_test as module

    _, _, _, settings = configured
    with patch.object(module, "load_settings", return_value=settings), patch.object(
        module, "run_host", side_effect=AssertionError("must not start")
    ), patch.object(
        module,
        "read_runtime_status",
        return_value={"state": "not_running", "healthy": False},
    ), patch(
        "src.core.runtime_service.read_service_status", return_value={"ready": False}
    ):
        assert module.main(["--config", "unused.yaml", "--status"]) == 2
    assert "not_running" in capsys.readouterr().out


def test_status_diagnostic_dependency_cannot_disable_receiver(configured):
    _, _, _, settings = configured
    with patch(
        "scripts.run_personal_monitor_feishu_group_test.process_identity",
        side_effect=ImportError,
    ):
        status = RuntimeStatus(settings)
    assert status.payload["state"] == "starting"
    assert status.payload["process_started_at"] is None


@pytest.mark.skipif(os.name != "nt", reason="Real Windows destination sharing lock")
def test_status_windows_reader_releases_and_atomic_update_recovers(configured):
    _, _, _, settings = configured
    status = RuntimeStatus(settings)
    reader = status.path.open("r", encoding="utf-8")
    reader.read(1)
    released = threading.Event()

    def release():
        reader.close()
        released.set()

    timer = threading.Timer(0.06, release)
    timer.start()
    try:
        status.update(activity="polling", state="running")
    finally:
        timer.join(timeout=2)
        reader.close()
    assert released.is_set()
    assert json.loads(status.path.read_text(encoding="utf-8"))["activity"] == "polling"
    assert not list(settings["workspace"].glob(".poller_status.json.*.tmp"))


@pytest.mark.skipif(os.name != "nt", reason="Real Windows destination sharing lock")
def test_status_windows_permanent_reader_fails_bounded_without_losing_old_file(
    configured,
):
    _, _, _, settings = configured
    sleep = Mock()
    status = RuntimeStatus(settings, sleep=sleep, replace_attempts=3)
    original = status.path.read_bytes()
    with status.path.open("r", encoding="utf-8") as reader:
        reader.read(1)
        with pytest.raises(PermissionError) as caught:
            status.update(activity="polling")
    assert caught.value.winerror in {5, 32, 33}
    assert [call.args[0] for call in sleep.call_args_list] == [0.01, 0.02]
    assert status.path.read_bytes() == original
    assert not list(settings["workspace"].glob(".poller_status.json.*.tmp"))
    status.update(activity="polling")
    assert json.loads(status.path.read_text(encoding="utf-8"))["activity"] == "polling"


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_status_windows_transient_codes_retry_only_to_fixed_bound(configured, winerror):
    _, _, _, settings = configured
    sleep = Mock()
    status = RuntimeStatus(settings, sleep=sleep)
    error = PermissionError(13, "private error")
    error.winerror = winerror
    replace = patch.object(Path, "replace", side_effect=error)
    with replace as operation, pytest.raises(PermissionError):
        status.update(activity="polling")
    assert operation.call_count == 8
    assert [call.args[0] for call in sleep.call_args_list] == [
        0.01,
        0.02,
        0.04,
        0.08,
        0.1,
        0.1,
        0.1,
    ]


def test_status_does_not_retry_permanent_nonsharing_errors(configured):
    _, _, _, settings = configured
    sleep = Mock()
    status = RuntimeStatus(settings, sleep=sleep)
    replace = patch.object(
        Path, "replace", side_effect=OSError(28, "private disk full error")
    )
    with replace as operation, pytest.raises(OSError):
        status.update(activity="polling")
    assert operation.call_count == 1
    sleep.assert_not_called()


def test_status_instance_lock_covers_payload_and_whole_atomic_commit(configured):
    _, _, _, settings = configured
    status = RuntimeStatus(settings)
    actual_replace = Path.replace
    gate = threading.Lock()
    active, maximum = 0, 0
    seen = []

    def inspected_replace(source, destination):
        nonlocal active, maximum
        with gate:
            active += 1
            maximum = max(maximum, active)
        try:
            snapshot = json.loads(source.read_text(encoding="utf-8"))
            assert snapshot["left"] == snapshot["right"]
            seen.append(source.name)
            time.sleep(0.001)
            return actual_replace(source, destination)
        finally:
            with gate:
                active -= 1

    def write_series(index):
        for iteration in range(12):
            token = f"{index}:{iteration}"
            status.update(left=token, right=token)

    with patch.object(Path, "replace", inspected_replace), ThreadPoolExecutor(
        max_workers=8
    ) as pool:
        futures = [pool.submit(write_series, index) for index in range(8)]
        for future in futures:
            future.result(timeout=10)
    assert maximum == 1 and len(seen) == len(set(seen)) == 96
    saved = json.loads(status.path.read_text(encoding="utf-8"))
    assert saved["left"] == saved["right"]
    assert not list(settings["workspace"].glob(".poller_status.json.*.tmp"))


def test_independent_status_instances_use_unique_temporary_files(configured):
    _, _, _, settings = configured
    first, second = RuntimeStatus(settings), RuntimeStatus(settings)
    barrier = threading.Barrier(2)
    actual_replace = Path.replace
    seen = set()
    seen_threads = set()
    gate = threading.Lock()

    def overlap(source, destination):
        with gate:
            seen.add(source.name)
            initial = threading.get_ident() not in seen_threads
            seen_threads.add(threading.get_ident())
        if initial:
            barrier.wait(timeout=5)
        return actual_replace(source, destination)

    with patch.object(Path, "replace", overlap), ThreadPoolExecutor(
        max_workers=2
    ) as pool:
        a = pool.submit(first.update, marker="first")
        b = pool.submit(second.update, marker="second")
        a.result(timeout=10)
        b.result(timeout=10)
    assert len(seen) == 2
    assert json.loads(first.path.read_text(encoding="utf-8"))["marker"] in {
        "first",
        "second",
    }
    assert not list(settings["workspace"].glob(".poller_status.json.*.tmp"))


def test_error_diagnostics_keep_safe_location_and_codes_without_private_data(
    configured, capsys
):
    _, _, _, settings = configured
    secret = "private-secret-user-position-body"
    private_path = "C:\\credentials\\private-account\\private-image.png"
    status = RuntimeStatus(settings)
    try:
        exc = PermissionError(13, secret, private_path)
        exc.winerror = 5
        raise exc
    except PermissionError as caught:
        diagnostic = safe_error(caught)
        status.error(caught)
    assert diagnostic["errno"] == 13 and diagnostic["winerror"] == 5
    assert diagnostic["frames"][-1]["file"] == Path(__file__).name
    assert diagnostic["frames"][-1]["line"] > 0
    assert diagnostic["frames"][-1]["function"] == (
        "test_error_diagnostics_keep_safe_location_and_codes_without_private_data"
    )
    saved = status.path.read_text(encoding="utf-8")
    output = capsys.readouterr().err
    for text in (saved, output, json.dumps(diagnostic)):
        assert secret not in text and private_path not in text
        assert "private-account" not in text and "private-image.png" not in text
        assert all(
            "/" not in frame["file"] and "\\" not in frame["file"]
            for frame in diagnostic["frames"]
        )
    restarted = RuntimeStatus(settings)
    assert restarted.payload["last_error"] == status.payload["last_error"]


def test_status_logging_failure_still_reports_original_error_safely(configured, capsys):
    _, _, _, settings = configured
    status = RuntimeStatus(settings)
    original = FeishuReadError("history", code=230027, http_status=403)
    failure = PermissionError(13, "private-secret-body", "private-filepath")
    failure.winerror = 5
    with patch.object(status, "update", side_effect=failure), pytest.raises(
        PermissionError
    ):
        status.error(original)
    output = capsys.readouterr().err
    data = json.loads(output)
    assert data["last_error"]["code"] == 230027
    assert data["status_write_error"]["winerror"] == 5
    assert "private-secret-body" not in output and "private-filepath" not in output


def test_process_access_denied_is_unverified_not_dead():
    import psutil

    from scripts.run_personal_monitor_feishu_group_test import process_identity

    with patch.object(
        psutil, "Process", side_effect=psutil.AccessDenied(123)
    ), pytest.raises(OSError, match="process inspection unavailable"):
        process_identity(123)


@pytest.mark.parametrize(
    "activity,heartbeat_age,phase_age,budget,expected",
    [
        ("processing", 600, 600, 900, "running"),
        ("processing", 900, 900, 900, "running"),
        ("processing", 901, 901, 900, "stale"),
        # An unrelated fresh status write cannot hide a stuck model call.
        ("processing", 1, 901, 900, "stale"),
        ("processing", 250, 250, 240, "stale"),
        ("polling", 181, 181, 900, "stale"),
        # Healthy repeated polling does not reset the phase clock.
        ("polling", 1, 10000, 900, "running"),
    ],
)
def test_status_distinguishes_slow_processing_from_stale_worker(
    configured, activity, heartbeat_age, phase_age, budget, expected
):
    import scripts.run_personal_monitor_feishu_group_test as module

    _, _, _, settings = configured
    settings["workspace"].mkdir(parents=True)
    saved = {
        "state": "running",
        "pid": 123,
        "process_started_at": 1,
        "updated_at": (NOW - timedelta(seconds=heartbeat_age)).isoformat(),
        "activity": activity,
        "activity_message_id": "om_slow" if activity == "processing" else None,
        "phase_started_at": (NOW - timedelta(seconds=phase_age)).isoformat(),
        "last_success_at": (NOW - timedelta(seconds=1000)).isoformat(),
        "last_error": {
            "error_type": "FeishuReadError",
            "operation": "history",
            "code": None,
            "http_status": 503,
            "retryable": True,
        },
    }
    (settings["workspace"] / "poller_status.json").write_text(json.dumps(saved))
    identity = {
        "alive": True,
        "started_at": 1,
        "argv": [module.__file__],
        "cwd": ".",
    }
    result = read_runtime_status(
        settings,
        inspector=Mock(return_value=identity),
        now=NOW,
        processing_timeout_seconds=budget,
    )
    assert result["state"] == expected
    assert result["healthy"] is (expected == "running")
    assert result["recorded_state"] == "running"
    assert result["started_at"] == result["process_started_at"] == 1
    assert result["heartbeat_age_seconds"] == heartbeat_age
    assert result["phase_age_seconds"] == phase_age
    assert result["activity"] == activity
    assert result["last_success_at"] == saved["last_success_at"]
    assert result["last_error"]["retryable"] is True


def test_phase_clock_changes_only_for_new_activity_or_message(configured):
    _, _, _, settings = configured
    current = NOW
    status = RuntimeStatus(settings, clock=lambda: current)
    last_success = NOW.isoformat()
    status.update(activity="polling", state="running", last_success_at=last_success)
    current = NOW + timedelta(seconds=10)
    status.update(activity="processing", activity_message_id="om_first")
    first_phase = status.payload["phase_started_at"]
    current = NOW + timedelta(seconds=200)
    status.update(activity="processing", activity_message_id="om_first")
    assert status.payload["phase_started_at"] == first_phase
    assert status.payload["updated_at"] == current.isoformat()
    current = NOW + timedelta(seconds=220)
    status.update(activity="processing", activity_message_id="om_second")
    assert status.payload["phase_started_at"] == current.isoformat()
    current = NOW + timedelta(seconds=230)
    status.update(activity="polling")
    polling_phase = status.payload["phase_started_at"]
    assert status.payload["activity_message_id"] is None
    current = NOW + timedelta(seconds=300)
    status.error(FeishuReadError("history", retryable=True), failures=1, delay=2)
    assert status.payload["phase_started_at"] == polling_phase
    assert status.payload["last_success_at"] == last_success
    assert status.payload["last_error"]["retryable"] is True
    status.update(activity="polling")
    assert status.payload["phase_started_at"] == polling_phase
    saved = json.loads(status.path.read_text(encoding="utf-8"))
    assert saved["updated_at"] == current.isoformat()
    assert saved["phase_started_at"] == polling_phase


@pytest.mark.parametrize("fails", [False, True])
def test_poller_exposes_processing_before_bridge_and_clears_it_after(configured, fails):
    _, _, _, settings = configured
    state = GroupState(settings, started_ms=STAMP)
    status = RuntimeStatus(settings)
    calls = []

    def activity(**values):
        calls.append(values)
        status.update(**values)

    def collect(_origin, _until):
        assert status.payload["activity"] == "polling"
        assert status.payload["activity_message_id"] is None
        return [item(text="sensitive body must never enter activity")]

    def process(_event):
        saved = json.loads(status.path.read_text(encoding="utf-8"))
        assert saved["activity"] == "processing"
        assert saved["activity_message_id"] == "om_1"
        assert "sensitive body" not in json.dumps(saved)
        if fails:
            raise RuntimeError("uncertain business effects")
        return "sent"

    api = Mock(collect_window=Mock(side_effect=collect))
    bridge = Mock(process=Mock(side_effect=process))
    poller = GroupPoller(settings, api, state, bridge, activity=activity)
    assert poller.poll_once(STAMP + 10000)["processed"] == 1
    assert calls == [
        {"activity": "polling"},
        {"activity": "processing", "activity_message_id": "om_1"},
        {"activity": "polling"},
    ]
    assert status.payload["activity"] == "polling"
    assert status.payload["activity_message_id"] is None
    with sqlite3.connect(state.path) as db:
        assert db.execute("SELECT state FROM group_messages").fetchone()[0] == (
            "interrupted_unknown" if fails else "sent"
        )


def test_dead_worker_status_still_reports_phase_and_last_network_progress(configured):
    _, _, _, settings = configured
    status = RuntimeStatus(settings, clock=lambda: NOW)
    status.update(activity="polling", last_success_at=NOW.isoformat())
    status.error(FeishuReadError("history", retryable=True), failures=1, delay=2)
    result = read_runtime_status(
        settings, inspector=Mock(return_value={"alive": False}), now=NOW
    )
    assert result["state"] == "not_running"
    assert result["recorded_state"] == "retrying"
    assert result["heartbeat_age_seconds"] == 0
    assert result["phase_age_seconds"] == 0
    assert result["last_success_at"] == NOW.isoformat()
    assert result["last_error"]["retryable"] is True


def test_receiver_cli_rejects_duplicate_lease_before_run_or_status_write(
    configured, capsys
):
    import scripts.run_personal_monitor_feishu_group_test as module

    _, _, _, settings = configured
    with patch.object(module, "load_settings", return_value=settings), patch.object(
        module, "validate_production_filter"
    ), patch.object(module, "FileLease") as lease, patch.object(
        module, "RuntimeStatus"
    ) as status, patch.object(module, "GroupState") as state:
        lease.return_value.__enter__.side_effect = RuntimeError(
            "同一测试工作区的进程已运行"
        )
        assert module.main(["--config", "unused.yaml", "--host-runtime"]) == 2
    lease.assert_called_once_with(settings["workspace"] / "poller.lock")
    status.assert_not_called()
    state.assert_not_called()
    assert not settings["workspace"].exists()
    assert '"error_type": "RuntimeError"' in capsys.readouterr().err


@pytest.mark.parametrize("mode", ["--probe", "--status"])
def test_readonly_cli_does_not_take_receiver_lease(configured, mode):
    import scripts.run_personal_monitor_feishu_group_test as module

    _, _, _, settings = configured
    with patch.object(module, "load_settings", return_value=settings), patch.object(
        module, "FileLease", side_effect=AssertionError("read-only must not lock")
    ), patch.object(module, "run_probe", return_value=0) as probe, patch.object(
        module, "read_runtime_status", return_value={"healthy": True, "pid": 1}
    ) as inspect, patch(
        "src.core.runtime_service.read_service_status",
        return_value={"ready": True, "pid": 1},
    ):
        assert module.main(["--config", "unused.yaml", mode]) == 0
    if mode == "--probe":
        probe.assert_called_once_with(settings)
        inspect.assert_not_called()
    else:
        probe.assert_not_called()
        inspect.assert_called_once_with(settings)


def test_real_receiver_lease_is_exclusive_and_released_on_exit(configured):
    from scripts.personal_monitor_process import FileLease

    _, _, _, settings = configured
    path = settings["workspace"] / "poller.lock"
    rejected = pytest.raises(RuntimeError, match="进程已运行")
    with FileLease(path), rejected, FileLease(path):
        pytest.fail("duplicate receiver entered")
    with FileLease(path):
        pass
    with pytest.raises(ValueError, match="exercise failure"), FileLease(path):
        raise ValueError("exercise failure")
    with FileLease(path):
        pass


def test_receiver_cli_holds_lease_through_run_and_releases_after_failure(configured):
    import scripts.run_personal_monitor_feishu_group_test as module
    from scripts.personal_monitor_process import FileLease

    _, _, _, settings = configured
    path = settings["workspace"] / "poller.lock"

    def construct(*_args):
        with pytest.raises(RuntimeError, match="进程已运行"), FileLease(path):
            pytest.fail("Host construction was not protected")
        raise RuntimeError("exercise constructor failure")

    with patch.object(module, "load_settings", return_value=settings), patch.object(
        module, "validate_production_filter"
    ), patch("src.core.execution.ExecutionStore", side_effect=construct):
        assert module.main(["--config", "unused.yaml", "--host-runtime"]) == 2
    with FileLease(path):
        pass


def history_receiver_stub(settings):
    from scripts.run_personal_monitor_feishu_group_test import HistoryBot

    receiver = object.__new__(HistoryBot)
    receiver.settings = settings
    receiver._stop, receiver._paused = threading.Event(), threading.Event()
    receiver.runtime = Mock(running=True)
    receiver.status = RuntimeStatus(settings)
    receiver.api = Mock()
    receiver.api.probe.return_value = {"history": {"ok": True}}
    receiver.poller, receiver.bridge = Mock(), Mock()
    receiver.validate_config = Mock()
    return receiver


def test_history_receiver_nonretryable_error_is_durable_and_host_can_close(configured):
    _, _, _, settings = configured
    receiver = history_receiver_stub(settings)
    receiver.poller.poll_once.side_effect = FeishuReadError(
        "history", code=230027, http_status=403
    )
    with pytest.raises(FeishuReadError):
        receiver.run()
    assert receiver.status.payload["state"] == "failed"
    assert receiver.status.payload["last_error"]["code"] == 230027
    receiver.stop()
    receiver.bridge.pause_admission.assert_called_once()
    receiver.bridge.stop.assert_called_once()


def test_history_receiver_backoff_stop_keeps_error_without_retry(configured):
    _, _, _, settings = configured
    receiver = history_receiver_stub(settings)
    receiver.api.probe.side_effect = FeishuReadError(
        "history", http_status=503, retryable=True
    )
    receiver._stop.wait = Mock(return_value=True)
    receiver.run()
    assert receiver.api.probe.call_count == 1
    assert receiver.status.payload["last_error"]["http_status"] == 503
    receiver.poller.poll_once.assert_not_called()


def test_history_receiver_cannot_hide_a_dead_shared_dispatcher(configured):
    _, _, _, settings = configured
    receiver = history_receiver_stub(settings)
    receiver.runtime.running = False
    with pytest.raises(RuntimeError, match="Shared execution dispatcher stopped"):
        receiver.run()
    assert receiver.status.payload["state"] == "failed"
    receiver.api.probe.assert_not_called()
