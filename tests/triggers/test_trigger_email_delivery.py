"""Authenticated email uses temporary SQLite/YAML and mocked transports only."""

import smtplib
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
import requests
import yaml

from src.core.execution import ExecutionStore
from src.core.report_execution import build_report_payload, runtime_path
from src.notification.email_notifier import EmailNotifier
from src.notification.manager import NotifierManager
from src.triggers import TriggerEvaluator, TriggerStore
from src.triggers.email_delivery import (
    TriggerEmailNotifier,
    trigger_email_route_allowed,
)

NOW = datetime(2026, 11, 1, 2, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    blocked = Mock(side_effect=AssertionError("real transport forbidden"))
    monkeypatch.setattr(requests, "post", blocked)
    monkeypatch.setattr(smtplib, "SMTP", blocked)
    monkeypatch.setattr(smtplib, "SMTP_SSL", blocked)
    for name in ("EMAIL_SENDER", "EMAIL_PASSWORD", "EMAIL_RECEIVER"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def route(tmp_path, monkeypatch, request):
    kind = getattr(request, "param", "daily")
    config = {
        "storage": {"data_dir": str(tmp_path)},
        "email": {
            "smtp_server": "smtp.invalid",
            "sender_email": "sender@example.invalid",
            "sender_password": "synthetic-secret",
            "receiver_email": "configured@example.invalid",
            "archive_dir": str(tmp_path / "archive"),
        },
        "notification": {
            "email": {
                "enabled": True,
                "trigger_report_routes": [{"owner": "alice", "chat_id": "chat-a"}],
            },
            "feishu": {"enabled": True, "webhook_url": "https://broadcast.invalid"},
            "telegram": {"enabled": True},
        },
        "interactive": {
            "feishu": {
                "enabled": True,
                "allowed_chat_ids": ["chat-a", "chat-b"],
                "app_id": "synthetic-app",
                "app_secret": "synthetic-feishu-secret",
            }
        },
    }
    path = tmp_path / "profile.yaml"

    def write():
        path.write_text(yaml.safe_dump(config), encoding="utf-8")

    write()
    store = TriggerStore(runtime_path(config))
    execution = ExecutionStore(store.path)
    store.create(
        "alice",
        {
            "schedule": {"at": NOW.isoformat()},
            "actions": [{"kind": kind, "payload": {"chat_id": "chat-a"}}],
        },
        "synthetic-report",
    )
    TriggerEvaluator(store).evaluate(now=NOW)
    with store.transaction() as db:
        action = store.claim_pending(db=db)[0]
        binding = {
            "action_id": action["id"],
            "owner": "alice",
            "chat_id": "chat-a",
            "number": 1,
            "revision": 1,
            "kind": kind,
            "config_path": str(path),
            "reason": '触发器 #1：<script>alert("test")</script>',
            "events": [
                {
                    "event_id": "source-q3",
                    "symbol": "601088",
                    "report_period": "2026Q3",
                    "day_offset": 0,
                    "date": "2026-11-01",
                    "title": '三季报 <img src=x onerror="alert(1)">',
                    "url": "https://disclosure.invalid/q3?one=1&two=2",
                    "published_at": NOW.isoformat(),
                    "source": "synthetic-source",
                },
                {"title": "unsafe", "url": "javascript:alert(1)"},
            ],
        }
        routed = {**deepcopy(config), "_trigger_context": binding}
        payload = build_report_payload(
            routed, SimpleNamespace(store=execution), kind=kind, config_path=path
        )
        run_id = execution.submit("reports." + kind, payload, db=db)
        store.mark_submitted(action["id"], run_id, db=db)
        db.execute("UPDATE execution_runs SET status='running' WHERE id=?", (run_id,))
    connection = MagicMock()
    server = connection.__enter__.return_value
    server.send_message.return_value = {}
    factory = Mock(return_value=connection)
    monkeypatch.setattr(smtplib, "SMTP_SSL", factory)
    monkeypatch.setattr(smtplib, "SMTP", factory)
    return SimpleNamespace(
        config=config,
        routed=routed,
        write=write,
        store=store,
        execution=execution,
        action=action,
        run_id=run_id,
        server=server,
        factory=factory,
    )


@pytest.mark.parametrize(
    "routes",
    [
        None,
        [],
        [{"owner": "*", "chat_id": "chat-a"}],
        [{"owner": "alice", "chat_id": "*"}],
        [{"owner": "bob", "chat_id": "chat-a"}],
        [{"owner": "alice", "chat_id": "chat-b"}],
        [{"owner": "alice", "chat_id": "chat-a", "receiver": "other@example.invalid"}],
    ],
)
def test_only_exact_trusted_owner_chat_pair_can_enable_mail(route, routes):
    config = deepcopy(route.routed)
    config["notification"]["email"]["trigger_report_routes"] = routes
    assert not trigger_email_route_allowed(config)
    manager = NotifierManager(config)
    assert manager.email is None and manager.telegram is None
    assert manager.feishu is not None
    route.factory.assert_not_called()


@pytest.mark.parametrize("route", ["brief", "daily"], indirect=True)
def test_authorized_manager_keeps_personal_chat_and_configured_email_only(route):
    manager = NotifierManager(route.routed)
    assert isinstance(manager.email, TriggerEmailNotifier)
    assert manager.feishu.binding["chat_id"] == "chat-a"
    assert manager.telegram is None
    assert manager.email.receiver_email == "configured@example.invalid"
    assert manager.email._delivery_guard() is None


def test_email_preserves_pdf_and_html_content_with_safe_event_context(route):
    notifier = TriggerEmailNotifier(route.routed)
    receipt = notifier._send_email(
        "股票日报",
        "<html><body><p>normal report</p></body></html>",
        pdf_bytes=b"%PDF-synthetic-only",
    )
    assert receipt["status"] == "sent"
    message = route.server.send_message.call_args.args[0]
    assert "触发日报 #1" in str(message["Subject"])
    assert message["To"] == "configured@example.invalid"
    body = next(
        part for part in message.walk() if part.get_content_type() == "text/html"
    )
    body = body.get_payload(decode=True).decode("utf-8")
    assert "normal report" in body and "2026Q3" in body
    assert "&lt;script&gt;" in body and "&lt;img" in body
    assert "<script>" not in body and "<img" not in body
    assert 'href="https://disclosure.invalid/q3?one=1&amp;two=2"' in body
    assert 'href="javascript:' not in body and "公告当天" in body
    assert any(part.get_content_type() == "application/pdf" for part in message.walk())


@pytest.mark.parametrize("change", ["delete", "update", "pause"])
@pytest.mark.parametrize(
    "method",
    ["send_from_session", "send_daily_report_from_session", "send_brief_report"],
)
def test_revocation_before_render_does_not_read_session_or_connect(
    route, change, method
):
    notifier = TriggerEmailNotifier(route.routed)
    if change == "delete":
        route.store.remove("alice", [1])
    elif change == "pause":
        route.store.pause("alice", 1)
    else:
        route.store.update("alice", 1, {"label": "edited"}, expected_revision=1)
    session = Mock()
    args = (session, {}) if method == "send_brief_report" else (session,)
    receipt = getattr(notifier, method)(*args)
    assert receipt == {"status": "skipped", "reason": "trigger_revoked"}
    session.get_all_dataframe.assert_not_called()
    route.factory.assert_not_called()


def test_revocation_during_preparation_prevents_smtp_connection(route, monkeypatch):
    notifier = TriggerEmailNotifier(route.routed)

    def archive(*_):
        route.store.remove("alice", [1])
        return "controlled.html"

    monkeypatch.setattr(notifier, "_save_email_copy", archive)
    receipt = notifier._send_email("controlled", "body")
    assert receipt == {"status": "skipped", "reason": "trigger_revoked"}
    route.factory.assert_not_called()


@pytest.mark.parametrize(
    "change", ["delete", "route", "allowlist", "recipient", "smtp", "disabled"]
)
def test_changes_during_smtp_login_revoke_only_email_before_submission(route, change):
    notifier = TriggerEmailNotifier(route.routed)

    def revoke(*_):
        if change == "delete":
            route.store.remove("alice", [1])
        elif change == "route":
            route.config["notification"]["email"]["trigger_report_routes"] = []
        elif change == "allowlist":
            route.config["interactive"]["feishu"]["allowed_chat_ids"] = ["chat-b"]
        elif change == "recipient":
            route.config["email"]["receiver_email"] = "replacement@example.invalid"
        elif change == "smtp":
            route.config["email"]["smtp_server"] = "replacement.invalid"
        else:
            route.config["notification"]["email"]["enabled"] = False
        route.write()

    route.server.login.side_effect = revoke
    receipt = notifier._send_email("controlled", "<p>synthetic</p>")
    assert receipt["status"] in {"failed", "skipped"}
    route.server.send_message.assert_not_called()


@pytest.mark.parametrize("name", ["SKIP_EMAIL", "SKIP_NOTIFICATIONS"])
@pytest.mark.parametrize("route", ["brief", "daily"], indirect=True)
def test_environment_switch_prevents_smtp(route, monkeypatch, name):
    notifier = TriggerEmailNotifier(route.routed)
    monkeypatch.setenv(name, "true")
    assert notifier._send_email("controlled", "body")["status"] == "skipped"
    route.factory.assert_not_called()


def test_forged_binding_never_connects_even_with_an_authorized_pair(route):
    config = deepcopy(route.routed)
    config["_trigger_context"]["reason"] = "forged queued payload"
    assert TriggerEmailNotifier(config)._send_email("controlled", "body") == {
        "status": "failed",
        "reason": "trigger_binding_invalid",
    }
    route.factory.assert_not_called()


@pytest.mark.parametrize("outcome", ["failed", "unknown", "partial"])
def test_channel_results_remain_independent_and_never_repeat_smtp(
    route, monkeypatch, outcome
):
    if outcome == "partial":
        recipients = "configured@example.invalid,denied@example.invalid"
        route.config["email"]["receiver_email"] = recipients
        route.routed["email"]["receiver_email"] = recipients
        route.write()
    manager = NotifierManager(route.routed)
    if outcome == "failed":
        route.server.login.side_effect = smtplib.SMTPAuthenticationError(
            535, b"synthetic"
        )
    elif outcome == "unknown":
        route.server.send_message.side_effect = smtplib.SMTPServerDisconnected(
            "synthetic DATA ambiguity"
        )
    else:
        route.server.send_message.return_value = {
            "denied@example.invalid": (550, b"synthetic")
        }
    monkeypatch.setattr(
        manager.email,
        "send_daily_report_from_session",
        lambda session: manager.email._send_email("controlled", "body"),
    )
    manager.feishu.send_daily_report_from_session = Mock(
        return_value={"status": "sent"}
    )
    receipt = manager.send_daily_report_from_session(object())
    assert receipt["status"] == "partial"
    assert receipt["deliveries"]["email"]["status"] == outcome
    assert receipt["deliveries"]["feishu"]["status"] == "sent"
    assert route.server.send_message.call_count == (0 if outcome == "failed" else 1)
    manager.feishu.send_daily_report_from_session.assert_called_once()


def test_plain_email_has_no_trigger_authorization_requirement(route):
    notifier = EmailNotifier(route.config)
    assert notifier._send_email("normal report", "plain body")["status"] == "sent"
    assert (
        str(route.server.send_message.call_args.args[0]["Subject"]) == "normal report"
    )


@pytest.mark.parametrize("route", ["brief", "daily"], indirect=True)
def test_default_trigger_delivery_inherits_configured_email(route, monkeypatch):
    for config in (route.config, route.routed):
        config["notification"]["email"].pop("trigger_report_routes")
    route.write()
    assert trigger_email_route_allowed(route.routed)
    manager = NotifierManager(route.routed)
    assert isinstance(manager.email, TriggerEmailNotifier)
    assert manager.email._delivery_guard() is None
    kind = route.routed["_trigger_context"]["kind"]
    method = {
        "brief": "send_brief_report",
        "daily": "send_daily_report_from_session",
    }[kind]
    chat = Mock(return_value={"status": "sent"})
    monkeypatch.setattr(manager.feishu, method, chat)
    monkeypatch.setattr(
        manager.email,
        method,
        lambda *args: manager.email._send_email("synthetic report", "body"),
    )
    receipt = (
        getattr(manager, method)(object(), {})
        if kind == "brief"
        else (getattr(manager, method)(object()))
    )
    assert receipt["status"] == "succeeded"
    assert {name: item["status"] for name, item in receipt["deliveries"].items()} == {
        "email": "sent",
        "feishu": "sent",
    }
    assert manager.telegram is None
    chat.assert_called_once()
    route.server.send_message.assert_called_once()
    message = route.server.send_message.call_args.args[0]
    assert message["To"] == "configured@example.invalid"
    assert {"brief": "触发简报", "daily": "触发日报"}[kind] in str(message["Subject"])


@pytest.mark.parametrize("route", ["daily", "brief"], indirect=True)
def test_default_email_still_rejects_forged_trigger_binding(route):
    route.routed["notification"]["email"].pop("trigger_report_routes")
    route.config["notification"]["email"].pop("trigger_report_routes")
    route.write()
    route.routed["_trigger_context"]["owner"] = "forged-owner"
    receipt = TriggerEmailNotifier(route.routed)._send_email("synthetic", "body")
    assert receipt == {"status": "failed", "reason": "trigger_binding_invalid"}
    route.factory.assert_not_called()


@pytest.mark.parametrize("route", ["brief", "daily"], indirect=True)
def test_explicit_empty_routes_keep_email_off(route):
    route.routed["notification"]["email"]["trigger_report_routes"] = []
    assert NotifierManager(route.routed).email is None
    route.factory.assert_not_called()
