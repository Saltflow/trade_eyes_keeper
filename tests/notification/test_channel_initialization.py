"""Channel construction cannot suppress other configured deliveries."""

import importlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.notification import manager as managers
from src.notification.email_notifier import EmailNotifier
from src.triggers.email_delivery import TriggerEmailNotifier

CHANNELS = ("email", "feishu", "telegram")
TARGETS = {
    False: (
        ("src.notification.email_notifier", "EmailNotifier"),
        ("src.notification.feishu_notifier", "FeishuNotifier"),
        ("src.notification.telegram_notifier", "TelegramNotifier"),
    ),
    True: (
        ("src.triggers.email_delivery", "TriggerEmailNotifier"),
        ("src.triggers.delivery", "TriggerChatNotifier"),
        ("src.triggers.telegram_delivery", "TriggerTelegramNotifier"),
    ),
}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    blocked = Mock(side_effect=AssertionError("real transport forbidden"))
    for target in (
        "requests.sessions.Session.request",
        "smtplib.SMTP",
        "smtplib.SMTP_SSL",
        "socket.create_connection",
        "socket.socket.connect",
    ):
        monkeypatch.setattr(target, blocked)
    for name in ("SKIP_NOTIFICATIONS", "SKIP_EMAIL", "SKIP_FEISHU", "SKIP_TELEGRAM"):
        monkeypatch.delenv(name, raising=False)
    yield
    blocked.assert_not_called()


@pytest.fixture(params=[False, True], ids=["ordinary", "trigger"])
def route(request, monkeypatch):
    triggered = request.param
    config = {"notification": {name: {"enabled": True} for name in CHANNELS}}
    if triggered:
        config["_trigger_context"] = {}
    factories = {}
    for name, (module, cls) in zip(CHANNELS, TARGETS[triggered]):
        notifier = Mock()
        for method in (
            "send_from_session",
            "send_daily_report_from_session",
            "send_brief_report",
        ):
            getattr(notifier, method).return_value = {"status": "sent"}
        notifier.send_deployment_notification.return_value = (True, "sent")
        factory = Mock(return_value=notifier)
        monkeypatch.setattr(importlib.import_module(module), cls, factory)
        factories[name] = factory
    return SimpleNamespace(config=config, factories=factories, triggered=triggered)


@pytest.mark.parametrize("failed", CHANNELS)
@pytest.mark.parametrize(
    "method",
    ["send_from_session", "send_daily_report_from_session", "send_brief_report"],
)
def test_initialization_failure_preserves_other_channel_receipts(
    route, failed, method, caplog
):
    route.factories[failed].side_effect = OSError("credential=DO-NOT-LEAK")
    manager = managers.NotifierManager(route.config)
    args = (object(), {}) if method == "send_brief_report" else (object(),)
    receipt = getattr(manager, method)(*args)
    assert receipt["status"] == "partial"
    assert receipt["deliveries"][failed] == {
        "status": "failed",
        "reason": "channel_initialization_failed",
        "error": "OSError",
    }
    assert "DO-NOT-LEAK" not in json.dumps(receipt) + caplog.text
    for name, factory in route.factories.items():
        factory.assert_called_once_with(route.config)
        if name == failed:
            assert getattr(manager, name) is None
            getattr(factory.return_value, method).assert_not_called()
        else:
            assert receipt["deliveries"][name]["status"] == "sent"
            getattr(factory.return_value, method).assert_called_once_with(*args)


def test_all_constructors_fail_is_failed_not_disabled(route):
    for factory in route.factories.values():
        factory.side_effect = RuntimeError("hidden")
    manager = managers.NotifierManager(route.config)
    receipt = manager.send_from_session(object())
    assert receipt["status"] == "failed"
    assert receipt["reason"] == "delivery_failed"
    assert set(receipt["deliveries"]) == set(CHANNELS)
    assert all(item["status"] == "failed" for item in receipt["deliveries"].values())
    assert manager.send_deployment_notification("done") == dict.fromkeys(
        CHANNELS, False
    )
    ok, detail = manager.send_test_email()
    assert not ok and "初始化失败" in detail and "未启用" not in detail


@pytest.mark.parametrize("disabled", ["global", "email", "config"])
def test_disabled_channel_does_not_import_or_construct(route, monkeypatch, disabled):
    if disabled == "config":
        route.config["notification"]["email"]["enabled"] = False
    else:
        monkeypatch.setenv(
            "SKIP_NOTIFICATIONS" if disabled == "global" else "SKIP_EMAIL", "true"
        )
    original = importlib.import_module
    forbidden = (
        {module for module, _ in TARGETS[route.triggered]}
        if disabled == "global"
        else {TARGETS[route.triggered][0][0]}
    )

    def guarded(name, package=None):
        resolved = (
            importlib.util.resolve_name(name, package) if name.startswith(".") else name
        )
        assert resolved not in forbidden
        return original(name, package)

    monkeypatch.setattr(managers, "import_module", guarded)
    manager = managers.NotifierManager(route.config)
    receipt = manager.send_from_session(object())
    assert "email" not in receipt["deliveries"]
    route.factories["email"].assert_not_called()
    assert manager.send_test_email() == (False, "Email 频道未启用")
    if disabled == "global":
        assert receipt["status"] == "skipped" and receipt["deliveries"] == {}
        assert all(getattr(manager, name) is None for name in CHANNELS)
        for factory in route.factories.values():
            factory.assert_not_called()


@pytest.mark.parametrize("failed", CHANNELS)
def test_import_failure_is_channel_local(route, monkeypatch, failed):
    original = importlib.import_module
    bad_module = TARGETS[route.triggered][CHANNELS.index(failed)][0]

    def importing(name, package=None):
        resolved = (
            importlib.util.resolve_name(name, package) if name.startswith(".") else name
        )
        if resolved == bad_module:
            raise ImportError("secret import detail")
        return original(name, package)

    monkeypatch.setattr(managers, "import_module", importing)
    receipt = managers.NotifierManager(route.config).send_from_session(object())
    assert receipt["status"] == "partial"
    assert receipt["deliveries"][failed]["error"] == "ImportError"
    route.factories[failed].assert_not_called()
    assert all(
        receipt["deliveries"][name]["status"] == "sent"
        for name in CHANNELS
        if name != failed
    )


def test_initialization_failure_keeps_uncertain_transport_unknown(route):
    route.factories["email"].side_effect = OSError("before transport")
    route.factories["feishu"].return_value.send_from_session.side_effect = TimeoutError(
        "after submission"
    )
    route.config["notification"]["telegram"]["enabled"] = False
    receipt = managers.NotifierManager(route.config).send_from_session(object())
    assert receipt["status"] == "unknown"
    assert receipt["deliveries"]["email"]["status"] == "failed"
    assert receipt["deliveries"]["feishu"]["status"] == "unknown"


def test_deployment_keeps_failed_channel_and_successful_others(route):
    route.factories["email"].side_effect = OSError("hidden")
    manager = managers.NotifierManager(route.config)
    assert manager.send_deployment_notification("done", "v1", "summary") == {
        "email": False,
        "feishu": True,
        "telegram": True,
    }
    for name in ("feishu", "telegram"):
        route.factories[
            name
        ].return_value.send_deployment_notification.assert_called_once_with(
            "done", "v1", "summary"
        )
    assert manager.send_test_email() == (False, "Email 频道初始化失败")


def test_injected_channels_without_constructor_keep_existing_receipt_contract():
    manager = managers.NotifierManager.__new__(managers.NotifierManager)
    manager.trigger_delivery = False
    manager.email = Mock()
    manager.email.send_from_session.return_value = {"status": "sent"}
    manager.email.send_deployment_notification.return_value = (True, "sent")
    manager.email.send_test_email.return_value = (True, "sent")
    manager.feishu = manager.telegram = None
    receipt = manager.send_from_session(object())
    assert receipt["status"] == "succeeded"
    assert receipt["deliveries"] == {"email": {"status": "sent"}}
    assert manager.send_deployment_notification("done") == {"email": True}
    assert manager.send_test_email() == (True, "sent")


def test_real_archive_path_failure_does_not_block_chat_channels(
    route, tmp_path, monkeypatch
):
    module, cls = TARGETS[route.triggered][0]
    # Restore the real constructor for this one channel, keeping transports fake.
    constructor = TriggerEmailNotifier if route.triggered else EmailNotifier
    blocked = tmp_path / "archive-file"
    blocked.write_text("synthetic", encoding="utf-8")
    route.config["email"] = {"archive_dir": str(blocked)}
    monkeypatch.setattr(importlib.import_module(module), cls, constructor)
    receipt = managers.NotifierManager(route.config).send_from_session(object())
    assert receipt["status"] == "partial"
    assert receipt["deliveries"]["email"]["error"] == "FileExistsError"
    assert receipt["deliveries"]["feishu"]["status"] == "sent"
    assert receipt["deliveries"]["telegram"]["status"] == "sent"
