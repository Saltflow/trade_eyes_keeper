"""Transport regression: natural-language examples never execute commands."""

import json
from unittest.mock import Mock, patch

import pytest

from src.interactive.feishu_app import FeishuApp
from src.interactive.feishu_handler import handle_feishu_message


def app():
    result = FeishuApp(
        {
            "interactive": {
                "feishu": {
                    "enabled": True,
                    "app_id": "test-app",
                    "app_secret": "test-secret",
                    "allowed_chat_ids": ["chat"],
                    "assistant": {"enabled": True},
                }
            }
        }
    )
    result._assistant = Mock()
    return result


def event(text, *, group=False, mention=None):
    return {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": "owner"}},
            "message": {
                "chat_id": "chat",
                "message_id": "message-1",
                "chat_type": "group" if group else "p2p",
                "mentions": mention or [],
                "content": json.dumps({"text": text}),
            },
        },
    }


@pytest.mark.parametrize(
    "text",
    [
        "解释 /config set workers 4",
        "查看 /data/research 中的报告",
        "把日报改成每周一次",
    ],
)
def test_plain_text_goes_to_assistant_without_legacy_execution(text):
    instance = app()
    with patch.object(instance.command_executor, "execute") as execute:
        assert handle_feishu_message(instance, event(text)) == "accepted"
    execute.assert_not_called()
    instance._assistant.submit.assert_called_once_with(
        "chat", "owner", text, "message-1"
    )


def test_legacy_slash_is_preserved():
    instance = app()
    with patch.object(instance.command_executor, "execute") as execute:
        handle_feishu_message(instance, event("/config set workers 4"))
    execute.assert_called_once()
    instance._assistant.submit.assert_not_called()


@pytest.mark.parametrize(
    "identity, expected", [("this-bot", "accepted"), ("someone-else", "ignored")]
)
def test_group_mentions_must_target_authenticated_bot(identity, expected):
    instance = app()
    mention = [{"id": {"open_id": identity}, "key": "@_user_1"}]
    with patch.object(instance, "get_bot_open_id", return_value="this-bot"):
        assert (
            handle_feishu_message(
                instance, event("@_user_1 修改日报频次", group=True, mention=mention)
            )
            == expected
        )
    assert instance._assistant.submit.called == (expected == "accepted")


def test_unaddressed_group_conversation_is_ignored():
    instance = app()
    assert handle_feishu_message(instance, event("把配置改了", group=True)) == "ignored"
    instance._assistant.submit.assert_not_called()


def test_routing_diagnostics_distinguish_ignored_and_queued_without_content(caplog):
    instance = app()
    with caplog.at_level("INFO"):
        handle_feishu_message(instance, event("private-user-content", group=True))
        handle_feishu_message(instance, event("private-user-content"))
    assert "reason=group_not_addressed" in caplog.text
    assert "reason=assistant_queued" in caplog.text
    assert "private-user-content" not in caplog.text
    assert "message-1" not in caplog.text
    assert "test-secret" not in caplog.text


def test_file_upload_honors_chat_gate_and_skip_flags(tmp_path, monkeypatch):
    instance = app()
    path = tmp_path / "result.csv"
    path.write_text("return\n1", encoding="utf-8")
    with patch("requests.post") as post:
        assert not instance.send_file("other", path)[0]
        monkeypatch.setenv("SKIP_FEISHU", "true")
        assert not instance.send_file("chat", path)[0]
    post.assert_not_called()


def test_bot_info_uses_authenticated_identity():
    instance = app()
    with patch.object(instance, "get_tenant_token", return_value="token"), patch(
        "requests.get"
    ) as get:
        get.return_value.json.return_value = {"code": 0, "bot": {"open_id": "this-bot"}}
        assert instance.get_bot_open_id() == "this-bot"
        assert instance.get_bot_open_id() == "this-bot"
    assert get.call_count == 1


def test_assistant_startup_failure_keeps_legacy_bot_enabled(monkeypatch):
    from src.interactive.feishu_app import FeishuApp

    app = FeishuApp(
        {
            "interactive": {
                "feishu": {
                    "enabled": True,
                    "app_id": "test-app",
                    "app_secret": "test-secret",
                    "allowed_chat_ids": ["*"],
                    "assistant": {"enabled": True},
                }
            }
        }
    )

    def fail_startup():
        raise OSError("assistant database unavailable")

    monkeypatch.setattr(app, "get_assistant", fail_startup)
    app.validate_config()
    assert app.enabled
    app.stop()
