"""Feishu outbound messages and persistent access checks; no live API calls."""

import time
from unittest.mock import patch

import pytest

from src.interactive.feishu_app import FeishuApp


def _make_config(extra=None):
    return {
        "interactive": {
            "feishu": {
                "enabled": True,
                "app_id": "test-app-id",
                "app_secret": "test-secret",
                "allowed_chat_ids": ["oc_test"],
                "rate_limit_per_minute": 10,
                **(extra or {}),
            }
        }
    }


@pytest.fixture(autouse=True)
def no_delivery_environment(monkeypatch):
    for name in (
        "SKIP_NOTIFICATIONS",
        "SKIP_FEISHU",
        "FEISHU_APP_ID",
        "FEISHU_APP_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)


class TestFeishuAppToken:
    def test_fetch_and_cache_tenant_token(self):
        app = FeishuApp(_make_config())
        with patch("requests.post") as post:
            post.return_value.json.return_value = {
                "code": 0,
                "tenant_access_token": "tok-first",
                "expire": 7200,
            }
            assert app.get_tenant_token() == "tok-first"
            assert app.get_tenant_token() == "tok-first"
            assert post.call_count == 1
            app._token_expires_at = time.time() - 10
            post.return_value.json.return_value["tenant_access_token"] = "tok-new"
            assert app.get_tenant_token() == "tok-new"
            assert post.call_count == 2


class TestFeishuAppAccess:
    @pytest.mark.parametrize(
        "extra",
        [
            {"enabled": False},
            {"allowed_chat_ids": []},
            {"allowed_chat_ids": "oc_test"},
            {"app_id": ""},
            {"app_secret": ""},
        ],
    )
    def test_invalid_configuration_fails_before_network(self, extra):
        app = FeishuApp(_make_config(extra))
        with pytest.raises(ValueError), patch("requests.post") as post:
            app.validate_config()
        post.assert_not_called()
        assert not app.enabled

    def test_http_callback_authentication_api_was_removed(self):
        app = FeishuApp(_make_config())
        assert not hasattr(app, "verify_event")
        assert not hasattr(app, "verify_signature")

    def test_explicit_wildcard_preserves_feishu_group_access(self):
        app = FeishuApp(_make_config({"allowed_chat_ids": ["*"]}))
        app.validate_config()
        assert app.enabled
        assert app.gate.is_allowed("another-feishu-chat")
        assert not app.gate.is_allowed("")

    @pytest.mark.parametrize("flag", ["SKIP_NOTIFICATIONS", "SKIP_FEISHU"])
    def test_skip_flag_blocks_token_and_message_requests(self, monkeypatch, flag):
        monkeypatch.setenv(flag, "1")
        app = FeishuApp(_make_config())
        with patch("requests.post") as post:
            assert app.get_tenant_token() == ""
            assert app.send_message("oc_test", "hello")[0] is False
        post.assert_not_called()

    def test_unauthorized_chat_and_stop_block_send(self):
        app = FeishuApp(_make_config())
        with patch("requests.post") as post:
            assert app.send_message("other", "hello")[0] is False
            app.stop()
            app.stop()
            assert app.send_message("oc_test", "hello")[0] is False
        post.assert_not_called()


class TestFeishuAppMessages:
    def test_send_card_message(self):
        app = FeishuApp(_make_config())
        with patch.object(app, "get_tenant_token", return_value="tok-abc"), patch(
            "requests.post"
        ) as post:
            post.return_value.json.return_value = {"code": 0}
            assert app.send_message("oc_test", "hello") == (True, "ok")
            payload = post.call_args.kwargs["json"]
            assert payload["receive_id"] == "oc_test"
            assert payload["msg_type"] == "interactive"

    def test_send_message_failure(self):
        app = FeishuApp(_make_config())
        with patch.object(app, "get_tenant_token", return_value="tok-abc"), patch(
            "requests.post"
        ) as post:
            post.return_value.json.return_value = {"code": 10001}
            ok, message = app.send_message("oc_test", "hello")
            assert not ok
            assert "10001" in message
