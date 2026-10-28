"""Transport acknowledgements and uncertainty, with every network call mocked."""

from __future__ import annotations

import json
import smtplib
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pandas as pd
import pytest
import requests

from src.notification import email_notifier, feishu_notifier
from src.notification.email_notifier import EmailNotifier
from src.notification.feishu_notifier import FeishuNotifier
from src.notification.manager import NotifierManager
from src.notification.receipts import combine_receipts
from src.notification.telegram_notifier import TelegramNotifier


@pytest.fixture(autouse=True)
def no_network_or_environment_skips(monkeypatch):
    for name in ("SKIP_NOTIFICATIONS", "SKIP_EMAIL", "SKIP_FEISHU", "SKIP_TELEGRAM"):
        monkeypatch.delenv(name, raising=False)
    unexpected = Mock(side_effect=AssertionError("real transport forbidden"))
    monkeypatch.setattr(requests, "post", unexpected)
    monkeypatch.setattr(smtplib, "SMTP", unexpected)
    monkeypatch.setattr(smtplib, "SMTP_SSL", unexpected)


@pytest.fixture
def config(tmp_path):
    return {
        "email": {
            "smtp_server": "smtp.invalid",
            "sender_email": "sender@example.invalid",
            "sender_password": "smtp-test-secret",
            "receiver_email": "good@example.invalid,bad@example.invalid",
            "archive_dir": str(tmp_path),
        },
        "notification": {
            "email": {"enabled": True},
            "feishu": {"enabled": True, "webhook_url": "https://hook.invalid/secret"},
            "telegram": {
                "enabled": True,
                "bot_token": "123456:TEST-SECRET",
                "chat_id": "42",
            },
        },
    }


def response(body, status=200):
    return SimpleNamespace(status_code=status, json=Mock(return_value=body))


@pytest.fixture
def smtp(config, monkeypatch):
    notifier = EmailNotifier(config)
    monkeypatch.setattr(notifier, "_save_email_copy", lambda *_: "controlled.html")
    connection = MagicMock()
    server = connection.__enter__.return_value
    server.send_message.return_value = {}
    factory = Mock(return_value=connection)
    monkeypatch.setattr(smtplib, "SMTP_SSL", factory)
    monkeypatch.setattr(smtplib, "SMTP", factory)
    return notifier, connection, server, factory


def test_smtp_acknowledges_each_recipient_once(smtp):
    notifier, _connection, server, _factory = smtp
    receipt = notifier._send_email("controlled", "<p>body</p>")
    assert receipt["status"] == "sent"
    assert receipt["smtp_accepted"] is True
    assert len(receipt["pieces"]) == 2
    assert {piece["status"] for piece in receipt["pieces"]} == {"sent"}
    assert receipt["message_id"].startswith("<")
    assert receipt["archive"] == "controlled.html"
    server.send_message.assert_called_once()


def test_smtp_partial_recipient_rejection_is_partial_not_failed(smtp):
    notifier, _connection, server, _factory = smtp
    server.send_message.return_value = {"bad@example.invalid": (550, b"rejected")}
    receipt = notifier._send_email("controlled", "body")
    assert receipt["status"] == "partial"
    assert receipt["refused_count"] == 1
    recipients = {piece["recipient"]: piece for piece in receipt["pieces"]}
    assert recipients["good@example.invalid"]["status"] == "sent"
    assert recipients["bad@example.invalid"]["status"] == "failed"
    assert recipients["bad@example.invalid"]["smtp_code"] == 550
    server.send_message.assert_called_once()


def test_smtp_quit_failure_cannot_undo_acknowledged_submission(smtp):
    notifier, connection, server, _factory = smtp
    connection.__exit__.side_effect = smtplib.SMTPResponseException(421, b"quit lost")
    receipt = notifier._send_email("controlled", "body")
    assert receipt["status"] == "sent"
    assert receipt["cleanup_error"] == "SMTPResponseException"
    assert receipt["smtp_accepted"] is True
    server.send_message.assert_called_once()


def test_smtp_quit_failure_preserves_partial_recipient_receipt(smtp):
    notifier, connection, server, _factory = smtp
    server.send_message.return_value = {"bad@example.invalid": (550, b"refused")}
    connection.__exit__.side_effect = OSError("cleanup")
    receipt = notifier._send_email("controlled", "body")
    assert receipt["status"] == "partial"
    assert receipt["cleanup_error"] == "OSError"
    server.send_message.assert_called_once()


def test_smtp_quit_failure_does_not_mask_explicit_data_rejection(smtp):
    notifier, connection, server, _factory = smtp
    server.send_message.side_effect = smtplib.SMTPDataError(554, b"not accepted")
    connection.__exit__.side_effect = OSError("quit failed as well")
    receipt = notifier._send_email("controlled", "body")
    assert receipt["status"] == "failed"
    assert receipt["error"] == "SMTPDataError"
    assert receipt["cleanup_error"] == "OSError"
    server.send_message.assert_called_once()


@pytest.mark.parametrize(
    "refused",
    [
        {"stranger@example.invalid": (550, b"reject")},
        {"bad@example.invalid": None},
        {"bad@example.invalid": (True, b"reject")},
    ],
)
def test_smtp_malformed_recipient_acknowledgement_is_unknown(smtp, refused):
    notifier, _connection, server, _factory = smtp
    server.send_message.return_value = refused
    receipt = notifier._send_email("controlled", "body")
    assert receipt["status"] == "unknown"
    assert all(piece["status"] == "unknown" for piece in receipt["pieces"])
    server.send_message.assert_called_once()


@pytest.mark.parametrize(
    "failure,status",
    [
        (smtplib.SMTPDataError(554, b"not accepted"), "failed"),
        (
            smtplib.SMTPSenderRefused(550, b"refused", "sender@example.invalid"),
            "failed",
        ),
        (smtplib.SMTPServerDisconnected("lost after DATA"), "unknown"),
        (TimeoutError("lost response after DATA"), "unknown"),
    ],
)
def test_smtp_classifies_explicit_rejection_vs_submission_uncertainty(
    smtp, failure, status
):
    notifier, _connection, server, _factory = smtp
    server.send_message.side_effect = failure
    receipt = notifier._send_email("controlled", "body")
    assert receipt["status"] == status
    assert all(piece["status"] == status for piece in receipt["pieces"])
    server.send_message.assert_called_once()


def test_smtp_all_recipients_refused_are_proven_failed(smtp):
    notifier, _connection, server, _factory = smtp
    refused = {
        "good@example.invalid": (550, b"rejected"),
        "bad@example.invalid": (551, b"rejected"),
    }
    server.send_message.side_effect = smtplib.SMTPRecipientsRefused(refused)
    receipt = notifier._send_email("controlled", "body")
    assert receipt["status"] == "failed"
    assert {piece["smtp_code"] for piece in receipt["pieces"]} == {550, 551}
    server.send_message.assert_called_once()


def test_smtp_missing_acknowledgement_is_unknown(smtp):
    notifier, _connection, server, _factory = smtp
    server.send_message.return_value = None
    receipt = notifier._send_email("controlled", "body")
    assert receipt["status"] == "unknown"
    assert receipt["reason"] == "smtp_acknowledgement_missing"
    server.send_message.assert_called_once()


def test_smtp_authentication_failure_is_pre_submit(smtp):
    notifier, _connection, server, _factory = smtp
    server.login.side_effect = smtplib.SMTPAuthenticationError(535, b"no")
    receipt = notifier._send_email("controlled", "body")
    assert receipt["status"] == "failed"
    assert receipt["reason"] == "smtp_pre_submit_failed"
    server.send_message.assert_not_called()


def test_smtp_connect_failure_is_pre_submit(smtp):
    notifier, _connection, server, factory = smtp
    factory.side_effect = OSError("connect failed")
    receipt = notifier._send_email("controlled", "body")
    assert receipt["status"] == "failed"
    server.send_message.assert_not_called()


def test_smtp_starttls_failure_is_pre_submit(smtp):
    notifier, _connection, server, _factory = smtp
    notifier.enable_ssl = False
    notifier.enable_tls = True
    server.starttls.side_effect = smtplib.SMTPException("starttls failed")
    receipt = notifier._send_email("controlled", "body")
    assert receipt["status"] == "failed"
    server.send_message.assert_not_called()




@pytest.mark.parametrize(
    "method",
    ["send_from_session", "send_daily_report_from_session", "send_brief_report"],
)
def test_email_public_reports_propagate_receipt(smtp, monkeypatch, method):
    notifier, _connection, _server, _factory = smtp
    receipt = {
        "status": "partial",
        "pieces": [{"recipient": "recipient", "status": "sent"}],
    }
    monkeypatch.setattr(notifier, "_send_email", Mock(return_value=receipt))
    monkeypatch.setattr(notifier, "_prepare_brief_report", lambda *_: ("title", "body"))
    monkeypatch.setattr(notifier, "_generate_daily_pdf", lambda *_: None)
    monkeypatch.setattr(notifier, "_build_email_body", lambda *_a, **_k: "body")
    monkeypatch.setattr(
        email_notifier, "generate_combined_chart", lambda **_k: (None, None)
    )
    session = SimpleNamespace(
        get_all_dataframe=lambda: pd.DataFrame(),
        get_alerts_as_dicts=list,
        announcements={},
        evaluation_reports=None,
    )
    arguments = (session, {}) if method == "send_brief_report" else (session,)
    assert getattr(notifier, method)(*arguments) is receipt


@pytest.mark.parametrize("channel", ["feishu", "telegram"])
@pytest.mark.parametrize(
    "failure,status",
    [
        (requests.exceptions.ConnectTimeout("connect"), "failed"),
        (requests.exceptions.InvalidURL("url"), "failed"),
        (requests.exceptions.ReadTimeout("after submission"), "unknown"),
        (requests.exceptions.ConnectionError("lost socket"), "unknown"),
        (RuntimeError("uncertain phase"), "unknown"),
    ],
)
def test_http_classifies_pre_submit_vs_uncertainty(
    config, monkeypatch, channel, failure, status
):
    post = Mock(side_effect=failure)
    monkeypatch.setattr(requests, "post", post)
    notifier = (
        FeishuNotifier(config) if channel == "feishu" else TelegramNotifier(config)
    )
    receipt = notifier._send("controlled", "body", classified=True)
    assert receipt["status"] == status
    post.assert_called_once()
    assert post.call_args.kwargs["allow_redirects"] is False


@pytest.mark.parametrize("channel", ["feishu", "telegram"])
@pytest.mark.parametrize(
    "http_status,expected",
    [
        (403, "failed"),
        (429, "failed"),
        (500, "unknown"),
        (503, "unknown"),
        (302, "unknown"),
    ],
)
def test_http_status_without_acknowledgement(
    config, monkeypatch, channel, http_status, expected
):
    post = Mock(return_value=response({}, status=http_status))
    monkeypatch.setattr(requests, "post", post)
    notifier = (
        FeishuNotifier(config) if channel == "feishu" else TelegramNotifier(config)
    )
    receipt = notifier._send("controlled", "body", classified=True)
    assert receipt["status"] == expected
    post.assert_called_once()


@pytest.mark.parametrize("channel", ["feishu", "telegram"])
@pytest.mark.parametrize("body", [{}, None, [], {"ok": 1}, {"code": False}])
def test_http_missing_or_malformed_api_ack_is_unknown(
    config, monkeypatch, channel, body
):
    monkeypatch.setattr(requests, "post", Mock(return_value=response(body)))
    notifier = (
        FeishuNotifier(config) if channel == "feishu" else TelegramNotifier(config)
    )
    assert notifier._send("controlled", "body", classified=True)["status"] == "unknown"


@pytest.mark.parametrize("channel", ["feishu", "telegram"])
def test_invalid_json_response_is_unknown(config, monkeypatch, channel):
    invalid = SimpleNamespace(
        status_code=200, json=Mock(side_effect=ValueError("invalid JSON"))
    )
    monkeypatch.setattr(requests, "post", Mock(return_value=invalid))
    notifier = (
        FeishuNotifier(config) if channel == "feishu" else TelegramNotifier(config)
    )
    assert notifier._send("controlled", "body", classified=True)["status"] == "unknown"


@pytest.mark.parametrize(
    "channel,body", [("feishu", {"code": 10001}), ("telegram", {"ok": False})]
)
def test_explicit_provider_rejection_is_failed(config, monkeypatch, channel, body):
    monkeypatch.setattr(requests, "post", Mock(return_value=response(body)))
    notifier = (
        FeishuNotifier(config) if channel == "feishu" else TelegramNotifier(config)
    )
    assert notifier._send("controlled", "body", classified=True)["status"] == "failed"


def test_feishu_report_records_each_card_and_stops_after_unknown(config, monkeypatch):
    notifier = FeishuNotifier(config)
    monkeypatch.setattr(
        notifier,
        "_build_report_sections",
        lambda *_a, **_k: [
            ("price", "body1", None),
            ("fundamental", "body2", None),
            ("technical", "body3", None),
        ],
    )
    monkeypatch.setattr(
        email_notifier, "build_strategy_text_summary", lambda *_a, **_k: ""
    )
    monkeypatch.setattr(feishu_notifier, "_build_ref_portfolio_markdown", lambda *_: "")
    post = Mock(
        side_effect=[response({"code": 0}), requests.exceptions.ReadTimeout("unknown")]
    )
    monkeypatch.setattr(requests, "post", post)
    receipt = notifier.send_daily_report_from_session(
        SimpleNamespace(get_all_dataframe=lambda: None)
    )
    assert receipt["status"] == "partial"
    assert [piece["status"] for piece in receipt["pieces"]] == [
        "sent",
        "unknown",
        "skipped",
    ]
    assert post.call_count == 2


def test_telegram_chunks_keep_partial_delivery_and_message_ids(config, monkeypatch):
    notifier = TelegramNotifier(config)
    post = Mock(
        side_effect=[
            response({"ok": True, "result": {"message_id": 123}}),
            requests.exceptions.ReadTimeout("unknown"),
        ]
    )
    monkeypatch.setattr(requests, "post", post)
    receipt = notifier._send("controlled", "a" * 12_000, classified=True)
    assert receipt["status"] == "partial"
    assert receipt["pieces"][0]["message_id"] == 123
    assert receipt["pieces"][1]["status"] == "unknown"
    assert all(piece["status"] == "skipped" for piece in receipt["pieces"][2:])
    assert post.call_count == 2


def test_telegram_report_stops_remaining_sections_after_partial_chunk(
    config, monkeypatch
):
    notifier = TelegramNotifier(config)
    monkeypatch.setattr(
        notifier,
        "_build_daily_sections",
        lambda *_: [
            ("price", "a" * 9000),
            ("fundamental", "body2"),
            ("technical", "body3"),
        ],
    )
    monkeypatch.setattr(
        email_notifier, "build_strategy_text_summary", lambda *_a, **_k: ""
    )
    monkeypatch.setattr(notifier, "_build_ref_portfolio_text", lambda *_: "")
    post = Mock(
        side_effect=[response({"ok": True}), requests.exceptions.ReadTimeout("unknown")]
    )
    monkeypatch.setattr(requests, "post", post)
    receipt = notifier.send_daily_report_from_session(
        SimpleNamespace(get_all_dataframe=lambda: None)
    )
    assert receipt["status"] == "partial"
    assert receipt["pieces"][0]["status"] == "partial"
    assert [piece["status"] for piece in receipt["pieces"][1:]] == [
        "skipped",
        "skipped",
    ]
    assert post.call_count == 2


def test_telegram_legacy_tuple_and_receipt_never_leak_token(
    config, monkeypatch, caplog
):
    token = config["notification"]["telegram"]["bot_token"]
    monkeypatch.setattr(
        requests,
        "post",
        Mock(
            side_effect=RuntimeError(
                f"connection https://api.telegram.org/bot{token}/sendMessage"
            )
        ),
    )
    notifier = TelegramNotifier(config)
    ok, detail = notifier._send("controlled", "body")
    assert ok is False
    assert "<redacted>" in detail
    assert token not in detail + caplog.text
    receipt = notifier._send("controlled", "body", classified=True)
    assert token not in json.dumps(receipt)


@pytest.mark.parametrize("channel", ["feishu", "telegram"])
def test_report_preparation_failure_never_starts_transport(
    config, monkeypatch, channel
):
    notifier = (
        FeishuNotifier(config) if channel == "feishu" else TelegramNotifier(config)
    )
    prepare = Mock(side_effect=ValueError("broken source"))
    monkeypatch.setattr(notifier, "_prepare_brief_report", prepare)
    receipt = notifier.send_brief_report(object(), {})
    assert receipt["status"] == "failed"
    requests.post.assert_not_called()


def test_manager_preserves_partial_recipient_and_piece_details(smtp):
    notifier, _connection, server, _factory = smtp
    server.send_message.return_value = {"bad@example.invalid": (550, b"refused")}
    manager = NotifierManager({})
    manager.email = notifier
    manager.feishu = SimpleNamespace(send_brief_report=lambda *_: {"status": "sent"})
    notifier._prepare_brief_report = lambda *_: ("controlled", "body")
    receipt = manager.send_brief_report(object(), {})
    assert receipt["status"] == "partial"
    assert receipt["deliveries"]["email"]["pieces"][1]["smtp_code"] == 550
    server.send_message.assert_called_once()


@pytest.mark.parametrize("status", ["failed", "unknown", "partial", "skipped"])
def test_legacy_email_deployment_tuple_cannot_claim_non_sent(smtp, monkeypatch, status):
    notifier, _connection, _server, _factory = smtp
    monkeypatch.setattr(
        notifier,
        "_get_server_info",
        lambda: {
            "hostname": "test",
            "ip_address": "127.0.0.1",
            "system": "test",
            "machine": "test",
            "kernel_version": "test",
        },
    )
    monkeypatch.setattr(notifier, "_send_email", lambda *_: {"status": status})
    assert notifier.send_deployment_notification()[0] is False


@pytest.mark.parametrize(
    "states,status",
    [
        (["sent", "failed"], "partial"),
        (["sent", "unknown"], "partial"),
        (["unknown", "skipped"], "unknown"),
        (["failed", "skipped"], "failed"),
        (["partial", "skipped"], "partial"),
        ([], "skipped"),
    ],
)
def test_combined_receipts_do_not_turn_uncertainty_into_failure(states, status):
    assert combine_receipts({"status": state} for state in states)["status"] == status
