"""Image routing, bounded authenticated transport and snapshot fact checks."""

import copy
import json
from datetime import datetime
from unittest.mock import Mock, patch

import pytest
import requests

from src.interactive.assistant.client import AssistantAPIError, DeepSeekAssistantClient
from src.interactive.assistant.position_images import (
    SHANGHAI,
    confirm_unscaled_quantity_units,
    parse_position_document,
    validate_position_document,
)
from src.interactive.assistant.settings import AssistantSettings
from src.interactive.feishu_app import FeishuApp
from src.interactive.feishu_handler import handle_feishu_message

PNG = b"\x89PNG\r\n\x1a\n" + b"test transport fixture"
NOW = datetime(2026, 10, 21, 14, 30, tzinfo=SHANGHAI)


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


def event(kind="image", content=None, **extra):
    return {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": "owner"}},
            "message": {
                "chat_id": "chat",
                "chat_type": "p2p",
                "message_id": "om_1",
                "message_type": kind,
                "create_time": "1792564200000",
                "update_time": "1792564201000",
                "content": json.dumps(
                    content if content is not None else {"image_key": "img_v3_test"}
                ),
                **extra,
            },
        },
    }


def document():
    return {
        "account_hint": "测试账户",
        "as_of": "2026-10-21T14:30:00+08:00",
        "positions": [
            {
                "code": "601088",
                "name": "中国神华",
                "quantity": 1000,
                "price": 40,
                "market_value": 40000,
                "currency": "CNY",
                "evidence": "1000股 40.00 40000.00",
            }
        ],
        "reminders": [],
        "ambiguities": [],
    }


def quote(code, as_of):
    return {
        "price": 40,
        "currency": "CNY",
        "time": "20261021142900",
        "source": "fixture",
    }


def response(payload=PNG, mime="image/png", size=None):
    result = Mock(status_code=200)
    result.headers = {"Content-Type": mime}
    if size is not None:
        result.headers["Content-Length"] = str(size)
    result.iter_content.return_value = [payload]
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    return result


def test_pure_image_reaches_assistant_with_platform_timestamps():
    instance = app()
    assert handle_feishu_message(instance, event()) == "accepted"
    args, kwargs = instance._assistant.submit.call_args
    assert args == ("chat", "owner", "", "om_1")
    assert kwargs["attachments"] == [
        {"type": "image", "image_key": "img_v3_test", "message_id": "om_1"}
    ]
    assert kwargs["message_time"]["create_time"] == "1792564200000"
    assert kwargs["message_time"]["update_time"] == "1792564201000"


def test_rich_post_retains_text_images_and_bot_mention_gate():
    instance = app()
    payload = event(
        "post",
        {
            "zh_cn": {
                "content": [
                    [
                        {"tag": "at", "user_id": "bot"},
                        {"tag": "text", "text": "导入持仓"},
                        {"tag": "img", "image_key": "img_test"},
                        {"tag": "img", "image_key": "img_test"},
                    ]
                ]
            }
        },
        chat_type="group",
        mentions=[{"id": {"open_id": "bot"}, "key": "@_bot"}],
    )
    with patch.object(instance, "get_bot_open_id", return_value="bot"):
        assert handle_feishu_message(instance, payload) == "accepted"
    assert instance._assistant.submit.call_args.args[2] == "导入持仓"
    assert len(instance._assistant.submit.call_args.kwargs["attachments"]) == 1


@pytest.mark.parametrize("change", ["group", "other_chat", "non_user", "disabled"])
def test_images_keep_existing_access_boundaries(change):
    instance, payload = app(), event()
    if change == "group":
        payload["event"]["message"]["chat_type"] = "group"
    elif change == "other_chat":
        payload["event"]["message"]["chat_id"] = "other"
    elif change == "non_user":
        payload["event"]["sender"]["sender_type"] = "app"
    else:
        instance.config["interactive"]["feishu"]["assistant"]["enabled"] = False
    assert handle_feishu_message(instance, payload) in {"ignored", "unauthorized"}
    instance._assistant.submit.assert_not_called()


def test_image_download_is_authenticated_fixed_endpoint_and_bytes_only():
    instance = app()
    with patch.object(instance, "get_tenant_token", return_value="token"), patch(
        "src.interactive.feishu_app.requests.get", return_value=response()
    ) as request:
        image = instance.download_message_image("om_1", "img_test")
    assert image["image"] == PNG and image["mime_type"] == "image/png"
    assert len(image["sha256"]) == 64
    assert request.call_args.args[0].endswith("/messages/om_1/resources/img_test")
    assert request.call_args.kwargs["headers"] == {"Authorization": "Bearer token"}
    assert request.call_args.kwargs["allow_redirects"] is False


@pytest.mark.parametrize(
    "key", ["https://evil.test/a", "../other", "a?x=1", "", "a" * 257]
)
def test_invalid_image_resource_never_networks(key):
    with patch("src.interactive.feishu_app.requests.get") as request, pytest.raises(
        ValueError
    ):
        app().download_message_image("om_1", key)
    request.assert_not_called()


@pytest.mark.parametrize(
    "payload,mime,size",
    [
        (b"{}", "application/json", None),
        (PNG, "image/jpeg", None),
        (PNG, "image/png", 10 * 1024 * 1024 + 1),
        (b"x" * (10 * 1024 * 1024 + 1), "image/png", None),
    ],
    ids=["json", "mime_mismatch", "declared_oversize", "stream_oversize"],
)
def test_download_rejects_wrong_type_mismatch_and_size(payload, mime, size):
    instance = app()
    with patch.object(instance, "get_tenant_token", return_value="token"), patch(
        "src.interactive.feishu_app.requests.get",
        return_value=response(payload, mime, size),
    ), pytest.raises(ValueError):
        instance.download_message_image("om_1", "img_test")


def test_dedicated_vision_json_prompt_does_not_claim_online_verification(monkeypatch):
    client = DeepSeekAssistantClient(
        {"llm": {"api_key": "test"}}, AssistantSettings(request_retries=0)
    )
    result = response()
    result.json.return_value = {
        "choices": [
            {"finish_reason": "stop", "message": {"content": json.dumps(document())}}
        ]
    }
    request = Mock(return_value=result)
    monkeypatch.setattr("src.interactive.assistant.client.requests.post", request)
    output = client.read_position_images([{"image": PNG}], context="这是我的持仓")
    assert output["positions"][0]["quantity"] == 1000
    payload = request.call_args.kwargs["json"]
    assert payload["response_format"] == {"type": "json_object"}
    content = payload["messages"][0]["content"]
    assert "禁止声称已核对行情" in content[0]["text"]
    assert "[PAGE" not in content[0]["text"]
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.parametrize(
    "images",
    [
        [],
        [{"url": "https://evil.test"}],
        [{"image": b"not-image"}],
        [{"image": PNG}] * 6,
    ],
)
def test_vision_rejects_external_urls_bad_bytes_and_unbounded_batches(images):
    client = DeepSeekAssistantClient(
        {"llm": {"api_key": "test"}}, AssistantSettings(request_retries=0)
    )
    with patch(
        "src.interactive.assistant.client.requests.post"
    ) as request, pytest.raises(AssistantAPIError):
        client.read_position_images(images)
    request.assert_not_called()


def test_valid_row_needs_timestamped_currency_matched_quote():
    validated = validate_position_document(document(), quote_loader=quote, now=NOW)
    assert validated["positions"][0]["importable"]
    assert validated["positions"][0]["quote_evidence"]["source"] == "fixture"
    assert "issues" not in document()["positions"][0]


@pytest.mark.parametrize(
    "field,value,issue",
    [
        ("quantity", None, "invalid_quantity"),
        ("quantity", -1, "invalid_quantity"),
        ("quantity", float("nan"), "invalid_quantity"),
        ("price", float("inf"), "invalid_price"),
        ("market_value", 4000, "quantity_price_value_mismatch"),
        ("currency", None, "currency_required"),
        ("code", None, "instrument_code_required"),
        ("ambiguities", ["quantity unclear"], "ambiguous_fields"),
    ],
)
def test_ambiguous_and_invalid_rows_are_not_importable(field, value, issue):
    payload = document()
    payload["positions"][0][field] = value
    row = validate_position_document(payload, quote_loader=quote, now=NOW)["positions"][
        0
    ]
    assert not row["importable"] and issue in row["issues"]


def test_display_rounding_is_allowed_without_fudging_large_mismatch():
    payload = document()
    payload["positions"][0]["market_value"] = 40004.99
    assert validate_position_document(payload, quote_loader=quote, now=NOW)[
        "positions"
    ][0]["importable"]
    payload["positions"][0]["market_value"] = 40005.02
    assert not validate_position_document(payload, quote_loader=quote, now=NOW)[
        "positions"
    ][0]["importable"]


def test_explicit_screenshot_units_are_normalized_once_with_original_evidence():
    payload = document()
    payload["positions"][0].update(
        quantity=0.1,
        quantity_unit="万股",
        price=4000,
        price_unit="分",
        price_decimals=0,
        market_value=4,
        market_value_unit="万元",
    )
    validated = validate_position_document(payload, quote_loader=quote, now=NOW)
    row = validated["positions"][0]
    assert (
        row["importable"]
        and row["quantity"] == 1000
        and row["price"] == 40
        and row["market_value"] == 40000
    )
    assert row["raw_values"]["market_value"] == {"value": 4, "unit": "万元"}
    twice = validate_position_document(validated, quote_loader=quote, now=NOW)[
        "positions"
    ][0]
    assert twice["importable"] and twice["market_value"] == 40000


def test_unknown_lot_size_or_conflicting_currency_unit_is_not_guessed():
    payload = document()
    payload["positions"][0]["quantity_unit"] = "手"
    assert (
        "unsupported_quantity_unit"
        in validate_position_document(payload, quote_loader=quote, now=NOW)[
            "positions"
        ][0]["issues"]
    )
    payload["positions"][0].pop("quantity_unit")
    payload["positions"][0]["market_value_unit"] = "万港元"
    assert (
        "unit_currency_mismatch"
        in validate_position_document(payload, quote_loader=quote, now=NOW)[
            "positions"
        ][0]["issues"]
    )


@pytest.mark.parametrize(
    "change,issue",
    [
        ({"currency": "HKD"}, "quote_currency_mismatch"),
        ({"time": "20261021140000"}, "quote_stale"),
        ({"time": "20261022140000"}, "quote_timestamp_invalid"),
        ({"time": None}, "quote_timestamp_invalid"),
        ({"price": 4}, "quote_price_mismatch"),
        ({"price": 0}, "quote_price_invalid"),
    ],
)
def test_quote_plausibility_uses_provider_fields(change, issue):
    row = validate_position_document(
        document(), quote_loader=lambda *args: {**quote(*args), **change}, now=NOW
    )["positions"][0]
    assert not row["importable"] and issue in row["issues"]


def test_missing_asof_not_assumed_upload_time_and_no_quote_not_assumed_verified():
    payload = document()
    payload["as_of"] = None
    assert (
        "snapshot_time_required"
        in validate_position_document(payload, quote_loader=quote, now=NOW)[
            "positions"
        ][0]["issues"]
    )
    assert (
        "quote_verification_unavailable"
        in validate_position_document(document(), now=NOW)["positions"][0]["issues"]
    )


def test_historical_screenshot_uses_its_date_not_today_price():
    payload = document()
    payload["as_of"] = "2026-10-20"
    row = validate_position_document(payload, quote_loader=quote, now=NOW)["positions"][
        0
    ]
    assert "closed_market_quote_date_unverified" in row["issues"]
    row = validate_position_document(
        payload,
        quote_loader=lambda *args: {
            **quote(*args),
            "time": "20261020150000",
            "market_closed": True,
            "market_timezone": "Asia/Shanghai",
            "latest_trading_date": "2026-10-20",
        },
        now=NOW,
    )["positions"][0]
    assert row["importable"]


def test_closed_market_quote_requires_explicit_latest_trading_date():
    payload = document()
    payload["as_of"] = "2026-10-20"
    closed = {**quote("", ""), "time": "20261020150000", "market_closed": True}
    row = validate_position_document(
        payload, quote_loader=lambda *args: closed, now=NOW
    )["positions"][0]
    assert "closed_market_quote_date_unverified" in row["issues"]
    closed["latest_trading_date"] = "2026-10-20"
    closed["market_timezone"] = "Asia/Shanghai"
    assert validate_position_document(
        payload, quote_loader=lambda *args: closed, now=NOW
    )["positions"][0]["importable"]


@pytest.mark.parametrize(
    "code,currency,as_of,session",
    [
        ("601088", "CNY", "2026-10-18", "2026-10-16"),
        ("AAPL", "USD", "2026-10-16", "2026-10-16"),
        ("AAPL", "USD", "2026-10-18", "2026-10-16"),
        ("601088", "CNY", "2026-10-18T14:30:00+08:00", "2026-10-16"),
    ],
)
def test_real_screenshot_provider_and_validator_agree_across_market_dates(
    code, currency, as_of, session
):
    from src.personal_monitor_runtime import SourcedObservationProvider

    payload = document()
    payload["as_of"] = as_of
    payload["positions"][0].update(code=code, currency=currency)
    history = Mock(
        return_value={"price": 40, "currency": currency, "source": "raw_daily_fixture"}
    )
    realtime = Mock(
        side_effect=AssertionError("old screenshots must not use realtime quotes")
    )
    provider = SourcedObservationProvider(
        {}, quote_fetcher=realtime, history_fetcher=history, clock=lambda: NOW
    )
    validated = validate_position_document(
        payload, quote_loader=provider.screenshot_quote, now=NOW
    )
    row = validated["positions"][0]
    assert row["importable"], row["issues"]
    assert row["quote_evidence"]["latest_trading_date"] == session
    assert row["quote_evidence"]["market_timezone"] == (
        "America/New_York" if currency == "USD" else "Asia/Shanghai"
    )
    history.assert_called_once_with(code, session)
    realtime.assert_not_called()
    if code == "AAPL":
        quoted = datetime.fromisoformat(row["quote_evidence"]["provider_time"])
        assert quoted.astimezone(SHANGHAI).date().isoformat() == "2026-10-17"


def test_real_screenshot_provider_and_validator_reject_old_intraday_without_minute_data():
    from src.personal_monitor_runtime import SourcedObservationProvider

    payload = document()
    payload["as_of"] = "2026-10-20T10:00:00+08:00"
    realtime, history = Mock(), Mock()
    provider = SourcedObservationProvider(
        {}, quote_fetcher=realtime, history_fetcher=history, clock=lambda: NOW
    )
    row = validate_position_document(
        payload, quote_loader=provider.screenshot_quote, now=NOW
    )["positions"][0]
    assert not row["importable"] and "quote_verification_unavailable" in row["issues"]
    realtime.assert_not_called()
    history.assert_not_called()


def test_forged_closed_session_cannot_gain_trust_by_matching_its_own_timestamp():
    payload = document()
    payload["as_of"] = "2026-10-18"
    forged = {
        **quote("", ""),
        "market_closed": True,
        "market_timezone": "Asia/Shanghai",
        "time": "20261015150000",
        "latest_trading_date": "2026-10-15",
        "session_verified": True,
    }
    row = validate_position_document(
        payload, quote_loader=lambda *args: forged, now=NOW
    )["positions"][0]
    assert (
        not row["importable"] and "closed_market_quote_date_unverified" in row["issues"]
    )


def test_one_bad_row_does_not_poison_good_row():
    payload = document()
    bad = copy.deepcopy(payload["positions"][0])
    bad["quantity"] = None
    payload["positions"].append(bad)
    rows = validate_position_document(payload, quote_loader=quote, now=NOW)["positions"]
    assert [row["importable"] for row in rows] == [True, False]


def test_explicit_zero_holding_does_not_require_a_current_price():
    payload = document()
    payload["positions"][0].update(quantity=0, market_value=0, price=None)
    loader = Mock()
    row = validate_position_document(payload, quote_loader=loader, now=NOW)[
        "positions"
    ][0]
    assert row["importable"]
    loader.assert_not_called()


def test_same_day_old_screenshot_requires_matching_historical_time():
    payload = document()
    payload["as_of"] = "2026-10-21T09:50:00+08:00"
    row = validate_position_document(payload, quote_loader=quote, now=NOW)["positions"][
        0
    ]
    assert "historical_quote_time_mismatch" in row["issues"]
    row = validate_position_document(
        payload,
        quote_loader=lambda *args: {**quote(*args), "time": "20261021095000"},
        now=NOW,
    )["positions"][0]
    assert row["importable"]


def test_quote_source_required_and_loader_errors_reject_without_guessing():
    payload = document()
    row = validate_position_document(
        payload, quote_loader=lambda *args: {**quote(*args), "source": None}, now=NOW
    )["positions"][0]
    assert "quote_source_required" in row["issues"]
    row = validate_position_document(
        payload, quote_loader=Mock(side_effect=OSError("offline")), now=NOW
    )["positions"][0]
    assert "quote_verification_unavailable" in row["issues"]


def test_truncated_or_non_json_vision_output_is_rejected(monkeypatch):
    client = DeepSeekAssistantClient(
        {"llm": {"api_key": "test"}}, AssistantSettings(request_retries=0)
    )
    result = response()
    request = Mock(return_value=result)
    monkeypatch.setattr("src.interactive.assistant.client.requests.post", request)
    for reason, content in [
        ("length", json.dumps(document())),
        ("stop", "I think there are 100 shares"),
    ]:
        result.json.return_value = {
            "choices": [{"finish_reason": reason, "message": {"content": content}}]
        }
        with pytest.raises(AssistantAPIError):
            client.read_position_images([{"image": PNG}])


@pytest.mark.parametrize(
    "value", ["not JSON", "[]", {"positions": [42]}, {"reminders": ["x"]}]
)
def test_invalid_structured_document_is_rejected(value):
    with pytest.raises(ValueError):
        parse_position_document(value)


def test_personal_send_persists_platform_id_and_reuses_legacy_payload(caplog):
    instance = app()
    result = Mock(status_code=200)
    result.json.return_value = {"code": 0, "data": {"message_id": "om_received"}}
    with patch.object(
        instance, "get_tenant_token", return_value="private-token"
    ), patch(
        "src.interactive.feishu_app.requests.post", return_value=result
    ) as request:
        outcome = instance.send_personal_message({"chat_id": "chat"}, "private-content")
        assert outcome == {
            "outcome": "sent",
            "detail": "ok",
            "platform_message_id": "om_received",
        }
        assert instance.send_message("chat", "private-content") == (True, "ok")
    kwargs = request.call_args.kwargs
    assert kwargs["json"]["receive_id"] == "chat"
    assert kwargs["json"]["msg_type"] == "interactive"
    assert kwargs["headers"]["Authorization"] == "Bearer private-token"
    assert (
        request.call_args.args[0]
        == "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
    )
    for sensitive in ("private-token", "private-content", "om_received"):
        assert sensitive not in caplog.text


@pytest.mark.parametrize(
    "error", [requests.Timeout, requests.ConnectionError, requests.HTTPError]
)
def test_personal_send_network_uncertainty_is_not_retried(error):
    instance = app()
    with patch.object(instance, "get_tenant_token", return_value="token"), patch(
        "src.interactive.feishu_app.requests.post", side_effect=error
    ) as request:
        assert instance.send_personal_message("chat", "x")["outcome"] == "unknown"
    request.assert_called_once()


@pytest.mark.parametrize("body", [None, [], {}, {"code": "0"}, {"code": False}])
def test_personal_send_malformed_result_is_unknown(body):
    instance = app()
    result = Mock(status_code=200)
    result.json.return_value = body
    with patch.object(instance, "get_tenant_token", return_value="token"), patch(
        "src.interactive.feishu_app.requests.post", return_value=result
    ) as request:
        assert instance.send_personal_message("chat", "x")["outcome"] == "unknown"
    request.assert_called_once()


def test_personal_send_invalid_json_and_unexpected_http_are_unknown():
    instance = app()
    result = Mock(status_code=200)
    result.json.side_effect = ValueError("private response")
    with patch.object(instance, "get_tenant_token", return_value="token"), patch(
        "src.interactive.feishu_app.requests.post", return_value=result
    ):
        assert instance.send_personal_message("chat", "x")["outcome"] == "unknown"
        result.status_code = 204
        result.json.side_effect = None
        result.json.return_value = {"code": 0}
        assert instance.send_personal_message("chat", "x")["outcome"] == "unknown"


def test_personal_send_explicit_platform_rejection_is_failed():
    instance = app()
    result = Mock(status_code=200)
    result.json.return_value = {"code": 10001, "msg": "private-platform-body"}
    with patch.object(instance, "get_tenant_token", return_value="token"), patch(
        "src.interactive.feishu_app.requests.post", return_value=result
    ) as request:
        assert instance.send_personal_message("chat", "x") == {
            "outcome": "failed",
            "detail": "Feishu code=10001",
        }
    request.assert_called_once()


def test_personal_send_preflight_failures_never_attempt_delivery(monkeypatch):
    instance = app()
    with patch("src.interactive.feishu_app.requests.post") as request:
        for route in ("other", "", {"chat_id": "other"}, None):
            assert instance.send_personal_message(route, "x")["outcome"] == "failed"
        monkeypatch.setenv("SKIP_FEISHU", "true")
        assert instance.send_personal_message("chat", "x")["outcome"] == "failed"
    request.assert_not_called()
    monkeypatch.delenv("SKIP_FEISHU")
    with patch.object(instance, "get_tenant_token", return_value=""), patch(
        "src.interactive.feishu_app.requests.post"
    ) as request:
        assert instance.send_personal_message("chat", "x")["outcome"] == "failed"
    request.assert_not_called()


@pytest.mark.parametrize(
    "asset,issue",
    [
        ("reverse_repo", "non_equity_asset_pending"),
        ("mixed", "mixed_price_value_currencies"),
    ],
)
def test_non_equity_and_mixed_currency_values_do_not_pass_stock_verification(
    asset, issue
):
    payload = document()
    row = payload["positions"][0]
    if asset == "reverse_repo":
        row["asset_type"] = "reverse_repo"
    else:
        row["market_value_currency"] = "HKD"
    quote_reader = Mock(side_effect=AssertionError("must not query an equity quote"))
    validated = validate_position_document(payload, quote_loader=quote_reader, now=NOW)
    assert issue in validated["positions"][0]["issues"]
    assert not validated["positions"][0]["importable"]
    quote_reader.assert_not_called()


def test_vision_source_indices_bind_only_to_supplied_image_bytes(monkeypatch):
    import hashlib

    client = DeepSeekAssistantClient(
        {"llm": {"api_key": "test"}}, AssistantSettings(request_retries=0)
    )
    doc = document()
    doc["positions"][0]["field_image_indices"] = {
        "code": [2],
        "price": [1],
        "quantity": [99],
    }
    doc["positions"][0]["field_sources"] = {"quantity": ["forged"]}
    result = response()
    result.json.return_value = {
        "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(doc)}}]
    }
    monkeypatch.setattr(
        "src.interactive.assistant.client.requests.post", Mock(return_value=result)
    )
    other = PNG + b"second"
    row = client.read_position_images([{"image": PNG}, {"image": other}])["positions"][
        0
    ]
    assert row["field_sources"] == {
        "code": [hashlib.sha256(other).hexdigest()],
        "price": [hashlib.sha256(PNG).hexdigest()],
    }
    assert "field_image_indices" not in row


def vision_response(*, content=None, reason="stop", status=200, usage=None):
    result = response()
    result.status_code = status
    result.json.return_value = {
        "choices": [
            {
                "finish_reason": reason,
                "message": {
                    "content": json.dumps(document()) if content is None else content
                },
            }
        ],
        "usage": usage or {},
    }
    return result


def image_client(**overrides):
    client = DeepSeekAssistantClient(
        {"llm": {"api_key": "private-key"}},
        AssistantSettings(request_retries=0, **overrides),
    )
    client.stopped.wait = Mock(return_value=False)
    return client


def test_screenshot_budget_is_independent_of_chat_budget(monkeypatch):
    client = image_client(
        request_timeout_seconds=5,
        max_tokens=512,
        reasoning_effort="high",
        image_timeout_seconds=17,
        image_max_tokens=2048,
        image_reasoning_effort="low",
        image_request_retries=0,
    )
    request = Mock(return_value=vision_response())
    monkeypatch.setattr("src.interactive.assistant.client.requests.post", request)
    client.read_position_images([{"image": PNG}])
    payload = request.call_args.kwargs["json"]
    assert payload["max_tokens"] == 2048 and payload["reasoning_effort"] == "low"
    assert request.call_args.kwargs["timeout"] == (5, 17)
    assert client.last_image_diagnostics[0]["max_tokens"] == 2048


@pytest.mark.parametrize(
    "failure,outcome",
    [
        ("length", "incomplete_finish"),
        ("json", "invalid_json_or_schema"),
        ("schema", "invalid_json_or_schema"),
        ("429", "http_error"),
        ("503", "http_error"),
        ("timeout", "network_timeout"),
        ("connection", "network_timeout"),
    ],
)
def test_screenshot_failures_retry_once_and_diagnose_each_actual_attempt(
    monkeypatch, failure, outcome
):
    client = image_client(image_request_retries=1)
    failures = {
        "length": vision_response(reason="length"),
        "json": vision_response(content="bad private-key JSON"),
        "schema": vision_response(content='{"positions":[42]}'),
        "429": vision_response(status=429),
        "503": vision_response(status=503),
        "timeout": requests.Timeout("private-key"),
        "connection": requests.ConnectionError("private-key"),
    }
    request = Mock(side_effect=[failures[failure], vision_response()])
    monkeypatch.setattr("src.interactive.assistant.client.requests.post", request)
    result = client.read_position_images([{"image": PNG}])
    assert result["positions"][0]["code"] == "601088"
    assert request.call_count == 2
    first, second = client.last_image_diagnostics
    assert (
        first["attempt"] == 1
        and first["outcome"] == outcome
        and first["will_retry"] is True
    )
    assert second["attempt"] == 2 and second["outcome"] == "success"
    assert "private-key" not in json.dumps(client.last_image_diagnostics)


@pytest.mark.parametrize("failure", ["length", "json", "timeout", "503"])
def test_screenshot_retries_are_exhausted_at_configured_bound(monkeypatch, failure):
    client = image_client(image_request_retries=2)
    bad = {
        "length": vision_response(reason="length"),
        "json": vision_response(content="bad JSON"),
        "timeout": requests.Timeout("private-key"),
        "503": vision_response(status=503),
    }[failure]
    request = (
        Mock(side_effect=bad)
        if isinstance(bad, BaseException)
        else Mock(return_value=bad)
    )
    monkeypatch.setattr("src.interactive.assistant.client.requests.post", request)
    with pytest.raises(AssistantAPIError):
        client.read_position_images([{"image": PNG}])
    assert request.call_count == 3 and len(client.last_image_diagnostics) == 3
    assert client.last_image_diagnostics[-1]["retry_available"] is False
    assert all(
        item.get("outcome") != "success" for item in client.last_image_diagnostics
    )


@pytest.mark.parametrize(
    "status,reason", [(400, "stop"), (401, "stop"), (200, "content_filter")]
)
def test_screenshot_terminal_rejections_do_not_retry(monkeypatch, status, reason):
    client = image_client(image_request_retries=2)
    request = Mock(return_value=vision_response(status=status, reason=reason))
    monkeypatch.setattr("src.interactive.assistant.client.requests.post", request)
    with pytest.raises(AssistantAPIError):
        client.read_position_images([{"image": PNG}])
    request.assert_called_once()
    assert len(client.last_image_diagnostics) == 1


@pytest.mark.parametrize("state", ["stopped", "expired"])
def test_screenshot_cannot_start_after_stop_or_deadline(monkeypatch, state):
    client = image_client()
    if state == "stopped":
        client.stop()
    monkeypatch.setattr("src.interactive.assistant.client.time.monotonic", lambda: 100)
    request = Mock()
    monkeypatch.setattr("src.interactive.assistant.client.requests.post", request)
    with pytest.raises(AssistantAPIError):
        client.read_position_images(
            [{"image": PNG}], deadline=99 if state == "expired" else 110
        )
    request.assert_not_called()
    assert client.last_image_diagnostics == []


@pytest.mark.parametrize("state", ["stopped", "deadline"])
def test_screenshot_late_response_is_never_accepted(monkeypatch, state):
    client = image_client()
    clock = {"now": 100}
    monkeypatch.setattr(
        "src.interactive.assistant.client.time.monotonic", lambda: clock["now"]
    )

    def respond(*args, **kwargs):
        if state == "stopped":
            client.stop()
        else:
            clock["now"] = 108
        return vision_response()

    request = Mock(side_effect=respond)
    monkeypatch.setattr("src.interactive.assistant.client.requests.post", request)
    with pytest.raises(AssistantAPIError):
        client.read_position_images([{"image": PNG}], deadline=107)
    request.assert_called_once()
    assert request.call_args.kwargs["timeout"] == (5, 7)
    assert client.last_image_diagnostics[0]["abort_reason"] == state


@pytest.mark.parametrize("state", ["stopped", "deadline"])
def test_retry_backoff_honors_stop_and_remaining_deadline(monkeypatch, state):
    client = image_client(image_request_retries=2)
    clock = {"now": 100}
    monkeypatch.setattr(
        "src.interactive.assistant.client.time.monotonic", lambda: clock["now"]
    )

    def waiting(delay):
        if state == "stopped":
            client.stop()
            return True
        assert delay == 0.5
        clock["now"] += delay
        return False

    client.stopped.wait = Mock(side_effect=waiting)
    request = Mock(side_effect=requests.Timeout("private-key"))
    monkeypatch.setattr("src.interactive.assistant.client.requests.post", request)
    with pytest.raises(AssistantAPIError):
        client.read_position_images([{"image": PNG}], deadline=100.5)
    request.assert_called_once()
    assert client.last_image_diagnostics[0]["abort_reason"] == state


def test_diagnostics_include_only_safe_numeric_usage_and_reset_each_call(monkeypatch):
    client = image_client()
    request = Mock(
        return_value=vision_response(
            usage={
                "total_tokens": 20,
                "prompt_tokens": True,
                "api_key": "private-key",
                "completion_tokens_details": {
                    "reasoning_tokens": 7,
                    "secret": "private-key",
                },
            }
        )
    )
    monkeypatch.setattr("src.interactive.assistant.client.requests.post", request)
    client.read_position_images([{"image": PNG}])
    assert client.last_image_diagnostics[0]["usage"] == {
        "total_tokens": 20,
        "reasoning_tokens": 7,
    }
    assert "private-key" not in json.dumps(client.last_image_diagnostics)
    with pytest.raises(AssistantAPIError):
        client.read_position_images([])
    assert client.last_image_diagnostics == []


def unitless_document(asset="equity"):
    payload = document()
    payload["positions"][0].update(
        asset_type=asset,
        quantity_unit=None,
        uncertain_fields=["quantity_unit"],
        ambiguities=["quantity_unit:截图未印数量单位"],
        field_sources={"quantity": ["actual-source-hash"]},
    )
    return payload


@pytest.mark.parametrize(
    "asset,unit", [("equity", "股"), ("etf", "份"), ("reit", "份")]
)
def test_confirm_unscaled_units_preserves_all_visible_numbers_and_records_user_source(
    asset, unit
):
    payload = unitless_document(asset)
    before = copy.deepcopy(payload)
    text = "股票按股、ETF和REIT按份，没有万倍缩放"
    confirmed = confirm_unscaled_quantity_units(payload, text, "om_unit_confirmation")
    row = confirmed["positions"][0]
    assert payload == before
    assert row["quantity_unit"] == unit
    assert {
        key: row[key] for key in ("quantity", "price", "market_value", "field_sources")
    } == {
        key: before["positions"][0][key]
        for key in ("quantity", "price", "market_value", "field_sources")
    }
    assert row["quantity_unit_declaration"] == {
        "message_id": "om_unit_confirmation",
        "user_text": text,
        "source": "authenticated_user_confirmation",
    }
    assert row["uncertain_fields"] == [] and row["ambiguities"] == []


def test_confirmation_never_replaces_a_visible_scaled_quantity_unit():
    payload = unitless_document()
    payload["positions"][0].update(quantity=0.1, quantity_unit="万股")
    confirmed = confirm_unscaled_quantity_units(
        payload, "确认其他未写单位的数量按股", "om_confirmation"
    )
    assert confirmed == payload
    assert confirmed["positions"][0]["quantity"] == 0.1
    assert "quantity_unit_declaration" not in confirmed["positions"][0]


@pytest.mark.parametrize("asset", ["reverse_repo", "unknown", "cash", "bond", None])
def test_confirmation_does_not_reinterpret_repo_or_unknown_assets(asset):
    payload = unitless_document(asset)
    assert (
        confirm_unscaled_quantity_units(
            payload, "股票按股，ETF和REIT按份", "om_confirmation"
        )
        == payload
    )


@pytest.mark.parametrize("text", [None, "", "  \t\n", 0])
def test_quantity_unit_confirmation_requires_nonempty_authenticated_user_text(text):
    payload = unitless_document()
    before = copy.deepcopy(payload)
    with pytest.raises(ValueError, match="用户消息"):
        confirm_unscaled_quantity_units(payload, text, "om_confirmation")
    assert payload == before


@pytest.mark.parametrize("message_id", [None, "", " \t", 123])
def test_quantity_unit_confirmation_requires_original_message_provenance(message_id):
    payload = unitless_document()
    with pytest.raises(ValueError, match="用户消息"):
        confirm_unscaled_quantity_units(payload, "股票按股", message_id)
    assert payload["positions"][0]["quantity_unit"] is None


@pytest.mark.parametrize("missing", ["quantity", "price", "market_value"])
def test_quantity_unit_confirmation_does_not_fill_other_missing_numbers(missing):
    payload = unitless_document()
    payload["positions"][0][missing] = None
    assert (
        confirm_unscaled_quantity_units(payload, "股票按股", "om_confirmation")
        == payload
    )


def test_unit_confirmation_retains_other_ambiguities_and_normal_checks():
    payload = unitless_document()
    payload["positions"][0].update(
        uncertain_fields=["quantity_unit", "code"],
        ambiguities=["quantity_unit：未印单位", "code:代码尚未确认"],
    )
    confirmed = confirm_unscaled_quantity_units(payload, "数量按股", "om_confirmation")
    row = confirmed["positions"][0]
    assert row["quantity_unit"] == "股"
    assert row["uncertain_fields"] == ["code"] and row["ambiguities"] == [
        "code:代码尚未确认"
    ]
    validated = validate_position_document(confirmed, quote_loader=quote, now=NOW)[
        "positions"
    ][0]
    assert not validated["importable"] and "ambiguous_fields" in validated["issues"]


def test_confirmed_units_still_require_consistent_quantity_price_and_value():
    payload = unitless_document()
    payload["positions"][0]["quantity"] = 100  # 100 * 40 is not 40000.
    confirmed = confirm_unscaled_quantity_units(
        payload, "数量按股，不放大缩小", "om_confirmation"
    )
    assert confirmed["positions"][0]["quantity"] == 100
    loader = Mock(side_effect=AssertionError("arithmetic failure must not fetch quote"))
    validated = validate_position_document(confirmed, quote_loader=loader, now=NOW)[
        "positions"
    ][0]
    assert (
        not validated["importable"]
        and "quantity_price_value_mismatch" in validated["issues"]
    )
    loader.assert_not_called()


def test_unit_only_ambiguity_can_pass_when_user_confirms_and_all_checks_pass():
    confirmed = confirm_unscaled_quantity_units(
        unitless_document(), "股票数量按股，原数字无缩放", "om_confirmation"
    )
    row = validate_position_document(confirmed, quote_loader=quote, now=NOW)[
        "positions"
    ][0]
    assert row["importable"], row["issues"]
    assert row["quantity"] == 1000 and row["market_value"] == 40000


def test_another_uncertain_field_cannot_pass_just_because_its_free_text_note_is_missing():
    payload = unitless_document()
    payload["positions"][0]["uncertain_fields"] = ["quantity_unit", "quantity"]
    confirmed = confirm_unscaled_quantity_units(
        payload, "数量单位按股", "om_confirmation"
    )
    assert confirmed["positions"][0]["uncertain_fields"] == ["quantity"]
    row = validate_position_document(confirmed, quote_loader=quote, now=NOW)[
        "positions"
    ][0]
    assert not row["importable"], (
        "Retaining an uncertain quantity marker must also prevent import."
    )
