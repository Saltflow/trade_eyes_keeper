"""飞书开放平台应用客户端测试。"""

import time
from unittest.mock import Mock, patch

import pytest

from src.interactive.feishu_app import FeishuApp


def _make_config(extra=None):
    return {
        "interactive": {
            "feishu": {
                "app_id": "test-app-id",
                "app_secret": "test-secret",
                "verification_token": "test-verify-token",
                "allowed_chat_ids": ["oc_test"],
                "rate_limit_per_minute": 10,
                **(extra or {}),
            }
        }
    }


class TestFeishuAppToken:
    def test_fetch_tenant_token(self):
        app = FeishuApp(_make_config())
        fake_resp = {
            "code": 0,
            "tenant_access_token": "tok-123",
            "expire": 7200,
        }
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = fake_resp
            token = app.get_tenant_token()
            assert token == "tok-123"
            mock_post.assert_called_once()

    def test_cache_token_within_ttl(self):
        app = FeishuApp(_make_config())
        fake_resp = {
            "code": 0,
            "tenant_access_token": "tok-first",
            "expire": 7200,
        }
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = fake_resp
            t1 = app.get_tenant_token()
            t2 = app.get_tenant_token()
            assert t1 == t2 == "tok-first"
            assert mock_post.call_count == 1  # cached, no second call

    def test_refresh_on_token_expiry(self):
        app = FeishuApp(_make_config())
        app._token_expires_at = time.time() - 10  # already expired
        resp1 = {"code": 0, "tenant_access_token": "tok-new", "expire": 7200}
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = resp1
            token = app.get_tenant_token()
            assert token == "tok-new"
            mock_post.assert_called_once()


class TestFeishuAppEvents:
    def test_challenge_verification(self):
        app = FeishuApp(_make_config())
        body = {
            "challenge": "abc-challenge-123",
            "token": "test-verify-token",
            "type": "url_verification",
        }
        result = app.verify_event(body)
        assert result == {"challenge": "abc-challenge-123"}

    def test_signature_check_invalid(self):
        app = FeishuApp(_make_config({"encrypt_key": "test-encrypt-key"}))
        headers = {"X-Lark-Signature": "bad-sig"}
        body = {"event_type": "im.message.receive_v1"}
        assert app.verify_signature(headers, body) is False

    def test_non_challenge_event(self):
        app = FeishuApp(_make_config())
        body = {"event_type": "im.message.receive_v1"}
        result = app.verify_event(body)
        assert result is True  # non-challenge events pass through


class TestFeishuAppMessages:
    def test_send_text_message(self):
        app = FeishuApp(_make_config())
        with patch.object(app, "get_tenant_token", return_value="tok-abc"):
            with patch("requests.post") as mock_post:
                mock_post.return_value.status_code = 200
                mock_post.return_value.json.return_value = {"code": 0}
                ok, msg = app.send_message("oc_test", "hello")
                assert ok
                assert msg == "ok"
                _, kwargs = mock_post.call_args
                body = kwargs["json"]
                assert body["receive_id"] == "oc_test"
                assert body["msg_type"] == "interactive"
                assert body["content"] is not None

    def test_send_message_failure(self):
        app = FeishuApp(_make_config())
        with patch.object(app, "get_tenant_token", return_value="tok-abc"):
            with patch("requests.post") as mock_post:
                mock_post.return_value.status_code = 200
                mock_post.return_value.json.return_value = {"code": 10001, "msg": "err"}
                ok, msg = app.send_message("oc_test", "hello")
                assert not ok
                assert "10001" in msg
