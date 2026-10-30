"""Mention isolation before the shared profile admits or writes a turn."""

import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.personal_monitor_shared_test import SharedPersonalTestBridge


def message(kind="text", *, own=False, mentions=None):
    if kind == "post":
        addressed = "own-bot" if own else "other-bot"
        content = {
            "zh_cn": {
                "content": [
                    [
                        {"tag": "at", "user_id": addressed},
                        {"tag": "img", "image_key": "original-platform-image"},
                    ]
                ]
            }
        }
    elif kind == "image":
        content = {"image_key": "original-platform-image"}
    else:
        content = {"text": "@_user_1 查看我现在的持仓"}
    result = {
        "message_id": "original-platform-message",
        "chat_id": "allowed-chat",
        "message_type": kind,
        "content": json.dumps(content),
        "create_time": "1792900020740",
        "update_time": "1792900020740",
    }
    if mentions is not None:
        result["mentions"] = mentions
    return result


def mentioned(identity):
    return [{"id": {"open_id": identity}, "key": "@_user_1"}]


@pytest.fixture
def bridge():
    # Deliberately no store/runtime: ignored messages cannot touch them.
    result = object.__new__(SharedPersonalTestBridge)
    result._admission_paused = threading.Event()
    result._admission_lock = threading.RLock()
    result.settings = {
        "allowed_sender_ids": ["authenticated-owner"],
        "allowed_chat_ids": ["allowed-chat"],
    }
    result.transport = SimpleNamespace(
        get_bot_open_id=Mock(return_value="own-bot"),
        send_message=Mock(),
        send_personal_message=Mock(),
        download_message_image=Mock(),
    )
    result.assistant = Mock()
    result.assistant.submit.return_value = True
    result.identity_resolver = lambda body: (
        "authenticated-owner",
        body["chat_id"],
        body["message_id"],
        body,
    )
    return result


@pytest.mark.parametrize("kind", ["text", "image", "post"])
def test_foreign_bot_mentions_never_admit_write_send_or_download(bridge, kind):
    item = message(kind, mentions=mentioned("other-bot"))
    assert bridge.process(item) == "ignored_foreign_mentions"
    bridge.assistant.submit.assert_not_called()
    bridge.transport.send_message.assert_not_called()
    bridge.transport.send_personal_message.assert_not_called()
    bridge.transport.download_message_image.assert_not_called()


@pytest.mark.parametrize("identity", ["", None, 123, Mock()])
def test_unverified_bot_identity_is_fail_closed(bridge, identity):
    bridge.transport.get_bot_open_id.return_value = identity
    assert (
        bridge.process(message(mentions=mentioned("own-bot")))
        == "ignored_foreign_mentions"
    )
    bridge.assistant.submit.assert_not_called()


def test_bot_identity_lookup_exception_is_fail_closed(bridge):
    bridge.transport.get_bot_open_id.side_effect = RuntimeError("private-auth-failure")
    assert (
        bridge.process(message(mentions=mentioned("own-bot")))
        == "ignored_foreign_mentions"
    )
    bridge.assistant.submit.assert_not_called()


@pytest.mark.parametrize(
    "mentions",
    [[{}], ["own-bot"], "malformed", {}, "", [{"id": "own-bot", "key": "@_user_1"}]],
)
def test_malformed_mentions_do_not_authorize_admission(bridge, mentions):
    assert bridge.process(message(mentions=mentions)) == "ignored_foreign_mentions"
    bridge.assistant.submit.assert_not_called()


@pytest.mark.parametrize("kind", ["text", "image", "post"])
def test_no_mentions_preserves_existing_test_profile_behavior(bridge, kind):
    assert bridge.process(message(kind)) == "queued"
    bridge.transport.get_bot_open_id.assert_not_called()
    bridge.assistant.submit.assert_called_once()


def test_own_bot_mention_strips_only_authenticated_mention_key(bridge):
    assert bridge.process(message(mentions=mentioned("own-bot"))) == "queued"
    assert bridge.assistant.submit.call_args.args[2] == "查看我现在的持仓"
    assert bridge.assistant.submit.call_args.args[3] == "original-platform-message"


def test_rich_post_with_own_at_and_image_keeps_image_reference_and_times(bridge):
    item = message("post", own=True, mentions=mentioned("own-bot"))
    assert bridge.process(item) == "queued"
    call = bridge.assistant.submit.call_args
    assert call.args[2] == ""
    assert call.kwargs["attachments"] == [
        {
            "type": "image",
            "message_id": "original-platform-message",
            "image_key": "original-platform-image",
        }
    ]
    assert call.kwargs["message_time"] == {
        "create_time": "1792900020740",
        "update_time": "1792900020740",
    }


def test_unrendered_claimed_self_mention_does_not_count_as_addressing(bridge):
    item = message("image", own=True, mentions=mentioned("own-bot"))
    assert bridge.process(item) == "ignored_foreign_mentions"
    bridge.assistant.submit.assert_not_called()


def test_own_mention_cannot_turn_slash_command_into_legacy_execution(bridge):
    item = message(mentions=mentioned("own-bot"))
    item["content"] = json.dumps({"text": "@_user_1 /daily"})
    assert bridge.process(item) == "legacy_command_disabled"
    bridge.assistant.submit.assert_not_called()


def test_route_allowlist_is_checked_before_identity_lookup(bridge):
    item = message(mentions=mentioned("own-bot"))
    item["chat_id"] = "foreign-chat"
    assert bridge.process(item) == "unauthorized"
    bridge.transport.get_bot_open_id.assert_not_called()
    bridge.assistant.submit.assert_not_called()
