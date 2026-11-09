"""Real ingress/handler command boundaries, with all external I/O replaced."""

import json
from unittest.mock import Mock

import pytest
import requests

from src.interactive.feishu_app import FeishuApp
from src.interactive.feishu_handler import handle_feishu_message
from src.interactive.ingress import (
    DurableIngress,
    ingress_access_check,
    ingress_command_dispatch,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(
        requests.sessions.Session,
        "request",
        Mock(side_effect=AssertionError("external transport forbidden")),
    )


def app():
    instance = FeishuApp(
        {
            "interactive": {
                "feishu": {
                    "enabled": True,
                    "app_id": "synthetic-app",
                    "app_secret": "synthetic-secret",
                    "allowed_chat_ids": ["chat"],
                    "rate_limit_per_minute": 1,
                    "assistant": {"enabled": True},
                }
            }
        }
    )
    instance.get_bot_open_id = Mock(return_value="authenticated-bot")
    instance.command_executor.execute = Mock()
    instance._assistant = Mock()
    instance.send_message = Mock(return_value=(True, "synthetic-ack"))
    return instance


def event(style="plain", text="/daily", *, identity="authenticated-bot"):
    mention = {"id": {"open_id": identity}, "key": "@_user_1"}
    content = {"text": text}
    if style == "mention":
        content = {"text": "@_user_1 " + text}
    elif style == "post":
        content = {
            "zh_cn": {
                "content": [
                    [
                        {"tag": "at", "user_id": identity},
                        {"tag": "text", "text": " " + text},
                    ]
                ]
            }
        }
    return {
        "header": {
            "event_type": "im.message.receive_v1",
            "app_id": "synthetic-app",
        },
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": "owner"}},
            "message": {
                "message_id": "synthetic-message",
                "chat_id": "chat",
                "chat_type": "p2p" if style == "plain" else "group",
                "message_type": "post" if style == "post" else "text",
                "mentions": [] if style == "plain" else [mention],
                "content": json.dumps(content),
            },
        },
    }


def row(inbox):
    with inbox.connection() as db:
        return dict(db.execute("SELECT * FROM execution_ingress").fetchone())


@pytest.mark.parametrize("style", ["plain", "mention", "post"])
def test_command_effect_then_crash_is_unknown_and_never_replayed(tmp_path, style):
    path = tmp_path / "ingress.sqlite3"
    inbox = DurableIngress(path, "synthetic-app", ["chat"])
    inbox.accept(event(style))
    first = app()
    effects = []

    def execute(*args, **kwargs):
        effects.append(row(inbox)["state"])
        raise RuntimeError("synthetic crash after command effect")

    first.command_executor.execute.side_effect = execute
    with pytest.raises(RuntimeError, match="after command effect"):
        inbox.dispatch(first, handle_feishu_message)
    assert row(inbox)["state"] == "unknown"
    assert effects == ["unknown"]
    assert ingress_access_check.get() is None
    assert ingress_command_dispatch.get() is None

    restarted = DurableIngress(path, "synthetic-app", ["chat"])
    second = app()  # Fresh process-equivalent BotAccess has no dedup memory.
    second.command_executor.execute.side_effect = execute
    restarted.accept(event(style))
    restarted.dispatch(second, handle_feishu_message)
    second.command_executor.execute.assert_not_called()
    assert effects == ["unknown"]
    assert row(restarted)["state"] == "unknown"


@pytest.mark.parametrize("style", ["plain", "mention", "post"])
def test_success_commits_command_boundary_before_execution_then_finishes(
    tmp_path, style
):
    inbox = DurableIngress(tmp_path / "ingress.sqlite3", "synthetic-app", ["chat"])
    inbox.accept(event(style))
    instance = app()
    observed = []
    instance.command_executor.execute.side_effect = lambda *args, **kwargs: (
        observed.append(row(inbox)["state"])
    )
    inbox.dispatch(instance, handle_feishu_message)
    assert observed == ["unknown"]
    assert row(inbox)["state"] == "done"
    assert row(inbox)["result"] == "accepted"
    instance._assistant.submit.assert_not_called()


@pytest.mark.parametrize("style", ["mention", "post"])
@pytest.mark.parametrize("identity", ["foreign-bot", ""])
def test_unverified_mentions_never_cross_command_boundary(tmp_path, style, identity):
    inbox = DurableIngress(tmp_path / "ingress.sqlite3", "synthetic-app", ["chat"])
    inbox.accept(event(style, identity=identity))
    instance = app()
    inbox.dispatch(instance, handle_feishu_message)
    instance.command_executor.execute.assert_not_called()
    instance._assistant.submit.assert_not_called()
    assert row(inbox)["state"] == "done"
    assert row(inbox)["result"] == "ignored"


@pytest.mark.parametrize("style", ["plain", "mention", "post"])
def test_quoted_command_remains_natural_language_and_retryable(tmp_path, style):
    inbox = DurableIngress(tmp_path / "ingress.sqlite3", "synthetic-app", ["chat"])
    inbox.accept(event(style, text="解释 /daily 命令"))
    instance = app()
    instance._assistant.submit.side_effect = [
        RuntimeError("admission unavailable"),
        True,
    ]
    inbox.dispatch(instance, handle_feishu_message)
    assert row(inbox)["state"] == "pending"
    assert row(inbox)["access_result"] == "accepted"
    inbox.dispatch(instance, handle_feishu_message)
    assert row(inbox)["state"] == "done"
    assert instance._assistant.submit.call_count == 2
    instance.command_executor.execute.assert_not_called()
    assert len(instance.access.rate_limiter._buckets["chat"]) == 1


def test_rich_text_at_without_authenticated_mention_is_ignored(tmp_path):
    inbox = DurableIngress(tmp_path / "ingress.sqlite3", "synthetic-app", ["chat"])
    body = event("post")
    body["event"]["message"]["mentions"] = []
    inbox.accept(body)
    instance = app()
    inbox.dispatch(instance, handle_feishu_message)
    assert row(inbox)["result"] == "ignored"
    instance.command_executor.execute.assert_not_called()
    instance._assistant.submit.assert_not_called()


def test_boundary_commit_failure_prevents_command_and_allows_safe_retry(
    tmp_path, monkeypatch
):
    inbox = DurableIngress(tmp_path / "ingress.sqlite3", "synthetic-app", ["chat"])
    inbox.accept(event("mention"))
    instance = app()
    original = inbox.begin_command
    monkeypatch.setattr(
        inbox, "begin_command", Mock(side_effect=RuntimeError("commit unavailable"))
    )
    with pytest.raises(RuntimeError, match="commit unavailable"):
        inbox.dispatch(instance, handle_feishu_message)
    instance.command_executor.execute.assert_not_called()
    assert row(inbox)["state"] == "pending"
    assert ingress_access_check.get() is None
    assert ingress_command_dispatch.get() is None
    monkeypatch.setattr(inbox, "begin_command", original)
    inbox.dispatch(instance, handle_feishu_message)
    instance.command_executor.execute.assert_called_once()
    assert row(inbox)["state"] == "done"
    assert len(instance.access.rate_limiter._buckets["chat"]) == 1


def test_stale_pending_selection_cannot_execute_a_settled_command(
    tmp_path, monkeypatch
):
    inbox = DurableIngress(tmp_path / "ingress.sqlite3", "synthetic-app", ["chat"])
    inbox.accept(event("post"))
    stale = inbox.pending()
    first = app()
    inbox.dispatch(first, handle_feishu_message)
    first.command_executor.execute.assert_called_once()
    monkeypatch.setattr(inbox, "pending", lambda: stale)
    second = app()
    inbox.dispatch(second, handle_feishu_message)
    second.command_executor.execute.assert_not_called()
    assert row(inbox)["state"] == "done"
    assert row(inbox)["result"] == "accepted"


def test_identity_resolution_failure_before_command_does_not_consume_it(tmp_path):
    inbox = DurableIngress(tmp_path / "ingress.sqlite3", "synthetic-app", ["chat"])
    inbox.accept(event("mention"))
    instance = app()
    instance.get_bot_open_id.side_effect = [
        RuntimeError("identity temporarily unavailable"),
        "authenticated-bot",
    ]
    with pytest.raises(RuntimeError, match="identity temporarily unavailable"):
        inbox.dispatch(instance, handle_feishu_message)
    instance.command_executor.execute.assert_not_called()
    assert row(inbox)["state"] == "pending"
    inbox.dispatch(instance, handle_feishu_message)
    instance.command_executor.execute.assert_called_once()
    assert row(inbox)["state"] == "done"
