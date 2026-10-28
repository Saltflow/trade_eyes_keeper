"""Offline isolation checks for the optional private Feishu acceptance bridge."""

import json
import sqlite3
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import pytest
import yaml

from scripts.personal_monitor_transport import make_transport
from scripts.run_personal_monitor_feishu_test import (
    discover_message,
    load_test_settings,
    production_app_ids,
    run_receiver,
)


@pytest.fixture
def configured(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yaml").write_text(
        yaml.safe_dump({"interactive": {"feishu": {"app_id": "cli_production123"}}}),
        encoding="utf-8",
    )
    (tmp_path / "config" / ".env").write_text(
        "FEISHU_APP_ID=cli_prodfromenv123\nFEISHU_APP_SECRET=private-production-secret\n",
        encoding="utf-8",
    )
    raw = {
        "test_mode": True,
        "app_id": "cli_newtest123",
        "allowed_chat_ids": ["oc_test"],
        "allowed_sender_ids": ["ou_self"],
        "workspace_name": "acceptance",
    }
    path = tmp_path / "test.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    env = {
        "PERSONAL_MONITOR_TEST_FEISHU_APP_SECRET": "private-test-secret",
        "PERSONAL_MONITOR_TEST_DEEPSEEK_API_KEY": "private-test-key",
    }
    return tmp_path, path, raw, env


def load(configured, **kwargs):
    root, path, _raw, env = configured
    return load_test_settings(path, root=root, environ=env, **kwargs)


def event(
    text="查看我的提醒",
    *,
    kind="text",
    content=None,
    owner="ou_self",
    chat="oc_test",
    chat_type="p2p",
    message_id="om_test",
    created=None,
):
    return {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": owner}},
            "message": {
                "chat_id": chat,
                "chat_type": chat_type,
                "message_id": message_id,
                "message_type": kind,
                "create_time": str(
                    int((created or datetime.now(timezone.utc)).timestamp() * 1000)
                ),
                "content": json.dumps(
                    content if content is not None else {"text": text}
                ),
            },
        },
    }


def test_production_ids_only_are_read_and_dotenv_never_loaded(configured, monkeypatch):
    root, _path, _raw, _env = configured
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    with patch(
        "dotenv.load_dotenv", side_effect=AssertionError("must never load dotenv")
    ):
        assert production_app_ids(root, {}) == {
            "cli_production123",
            "cli_prodfromenv123",
        }
        settings = load(configured)
    assert settings["app_secret"] == "private-test-secret"
    assert "FEISHU_APP_SECRET" not in __import__("os").environ
    assert (
        settings["workspace"]
        == root / "data" / "personal_monitor_feishu_test" / "acceptance"
    )


@pytest.mark.parametrize("app_id", ["cli_production123", "cli_prodfromenv123"])
def test_production_app_is_rejected_before_any_connection(configured, app_id):
    _root, path, raw, _env = configured
    raw["app_id"] = app_id
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with patch("requests.post") as post, patch("requests.get") as get, pytest.raises(
        ValueError, match="生产 App ID"
    ):
        load(configured)
    post.assert_not_called()
    get.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("test_mode", False),
        ("allowed_chat_ids", ["*"]),
        ("allowed_chat_ids", []),
        ("allowed_sender_ids", []),
        ("workspace_name", "../runtime"),
        ("market_check_times", ["25:99"]),
        ("llm", {"api_key": "must-not-pass"}),
    ],
)
def test_unsafe_or_production_like_configuration_is_rejected(configured, field, value):
    _root, path, raw, _env = configured
    raw[field] = value
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        load(configured)


def test_unknown_production_identity_fails_closed(configured):
    root, _path, _raw, _env = configured
    (root / "config" / "config.yaml").unlink()
    (root / "config" / ".env").unlink()
    with pytest.raises(ValueError, match="无法确认生产"):
        load(configured)


def test_production_config_path_is_rejected(configured):
    root, _path, _raw, env = configured
    with pytest.raises(ValueError, match="独立测试配置"):
        load_test_settings(root / "config" / "config.yaml", root=root, environ=env)


def test_discovery_allows_empty_lists_and_no_model_key_but_only_prints_ids(configured):
    root, path, raw, env = configured
    raw["allowed_chat_ids"], raw["allowed_sender_ids"] = [], []
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    env.pop("PERSONAL_MONITOR_TEST_DEEPSEEK_API_KEY")
    settings = load_test_settings(path, root=root, environ=env, discover=True)
    emit = Mock()
    assert discover_message(event("private-message-text"), emit=emit)
    assert json.loads(emit.call_args.args[0]) == {
        "chat_id": "oc_test",
        "sender_open_id": "ou_self",
    }
    assert "private-message-text" not in emit.call_args.args[0]
    assert not discover_message(event(chat_type="group"), emit=emit)
    assert not settings["workspace"].exists()


def test_discovery_receiver_does_not_construct_bridge_or_send(configured):
    root, path, raw, env = configured
    raw["allowed_chat_ids"], raw["allowed_sender_ids"] = [], []
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    settings = load_test_settings(path, root=root, environ=env, discover=True)
    context = Mock()
    context.Queue.return_value.get.side_effect = [
        ("message", event()),
        KeyboardInterrupt(),
    ]
    with patch(
        "scripts.run_personal_monitor_feishu_test.multiprocessing.get_context",
        return_value=context,
    ), patch(
        "src.interactive.assistant.service.FeishuAssistant",
        side_effect=AssertionError("discovery must not construct bridge"),
    ), patch("requests.post") as post, pytest.raises(KeyboardInterrupt):
        run_receiver(settings, discover=True)
    post.assert_not_called()
    context.Process.return_value.terminate.assert_called_once()
    assert not settings["workspace"].exists()


def test_restricted_transport_never_constructs_legacy_executor(configured):
    with patch(
        "src.interactive.feishu_app.CommandExecutor",
        side_effect=AssertionError("legacy executor disabled"),
    ):
        transport = make_transport(load(configured))
    assert transport.enabled
    assert not hasattr(transport, "command_executor")
    with pytest.raises(RuntimeError, match="完整助手"):
        transport.get_assistant()
    transport.stop()
    assert not transport.enabled


def audit_rows(instance):
    with sqlite3.connect(instance.database) as db:
        db.row_factory = sqlite3.Row
        return [
            dict(row, payload=json.loads(row["payload"]))
            for row in db.execute("SELECT * FROM test_audit ORDER BY sequence")
        ]


def pending_batch(instance, transport, client, *, owner="ou_self", chat="oc_test"):
    image = b"\x89PNG\r\n\x1a\npending fixture"
    transport.download_message_image.return_value = {
        "image": image,
        "mime_type": "image/png",
    }
    client.read_position_images.return_value = {
        "account_hint": None,
        "as_of": None,
        "positions": [
            {
                "code": "601088",
                "quantity": 1000,
                "price": None,
                "market_value": None,
                "currency": "CNY",
            }
        ],
        "reminders": [],
        "ambiguities": ["price missing"],
    }
    return instance.tools.prepare_images(
        owner,
        [{"message_id": "om_image", "image_key": "img_pending"}],
        "",
        {},
        chat=chat,
    )


def test_discovery_only_receiver_rejects_normal_business_mode_before_sdk(configured):
    settings = load(configured)
    with patch(
        "multiprocessing.get_context", side_effect=AssertionError("no SDK")
    ), pytest.raises(ValueError, match="仅保留只读会话发现"):
        run_receiver(settings)
    assert not settings["workspace"].exists()


def test_default_discovery_cli_cannot_start_a_business_receiver(configured):
    from scripts import run_personal_monitor_feishu_test as module

    with patch.object(module, "load_test_settings") as load_settings, pytest.raises(
        SystemExit
    ) as error:
        module.main(["--config", "unused.yaml"])
    assert error.value.code == 2
    load_settings.assert_not_called()
