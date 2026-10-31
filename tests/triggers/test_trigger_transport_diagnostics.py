"""Schema/transport diagnostics contain support codes, never response bodies."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.notification.feishu_notifier import _build_interactive_card
from src.triggers import delivery


def notifier(monkeypatch):
    result = delivery.TriggerChatNotifier(
        {
            "_trigger_context": {
                "chat_id": "synthetic-chat",
                "number": 3,
                "reason": "合成原因",
            },
        }
    )
    result._token = "synthetic-token"
    monkeypatch.setattr(result, "_guard", lambda: None)
    return result


@pytest.mark.parametrize(
    "card",
    [
        {"schema": "2.0", "elements": []},
        {"schema": "2.0", "body": {"elements": []}, "elements": []},
        {"schema": "2.0", "body": {"elements": "invalid"}},
        {"schema": "1.0", "elements": []},
    ],
)
def test_invalid_envelope_is_rejected_before_auth_or_submission(monkeypatch, card):
    post = Mock(side_effect=AssertionError("invalid card must not cross HTTP boundary"))
    monkeypatch.setattr(delivery.requests, "post", post)
    result = notifier(monkeypatch)._post_card({"card": card})
    assert result == {"status": "failed", "reason": "payload_invalid"}
    post.assert_not_called()


@pytest.mark.parametrize("status", [200, 400])
def test_platform_error_code_and_log_id_survive_without_sensitive_body(
    monkeypatch, status
):
    body = {
        "code": 230099,
        "msg": "secret-token and private-chat must never be stored",
        "error": {"log_id": "SYNTHETIC_LOG_001", "details": "private report body"},
    }
    monkeypatch.setattr(
        delivery.requests,
        "post",
        lambda *args, **kwargs: SimpleNamespace(status_code=status, json=lambda: body),
    )
    result = notifier(monkeypatch)._post_card(
        {"card": _build_interactive_card("合成标题", "合成正文")}
    )
    assert result["status"] == "failed"
    assert result["code"] == 230099
    assert result["platform_log_id"] == "SYNTHETIC_LOG_001"
    assert "secret-token" not in json.dumps(result)
    assert "private-chat" not in json.dumps(result)
    assert "private report body" not in json.dumps(result)
    assert "msg" not in result and "error" not in result


def test_malformed_error_body_keeps_definite_http_rejection(monkeypatch):
    def invalid():
        raise ValueError("synthetic non-JSON response")

    monkeypatch.setattr(
        delivery.requests,
        "post",
        lambda *args, **kwargs: SimpleNamespace(status_code=400, json=invalid),
    )
    result = notifier(monkeypatch)._post_card(
        {"card": _build_interactive_card("合成标题", "合成正文")}
    )
    assert result == {"status": "failed", "reason": "http_rejected", "http_status": 400}
