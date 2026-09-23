"""Service lifecycle, CLI and local readiness; no external transports run."""

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import main
from src.core import runtime_service as runtime
from src.core.process_lock import exclusive_process_lock


@pytest.fixture
def service_config(tmp_path):
    return {"storage": {"data_dir": str(tmp_path)}, "interactive": {}}


@pytest.fixture
def scheduler(monkeypatch):
    scheduler = Mock()
    monkeypatch.setattr(runtime, "ScheduleManager", lambda config: scheduler)
    return scheduler


def test_lifecycle_publishes_status_after_start_and_releases_lock(
    service_config, scheduler
):
    service = runtime.RuntimeService(service_config)

    def on_started():
        scheduler.start.assert_called_once()
        status = runtime.read_service_status(service_config)
        assert status["ready"]
        assert status["scheduler_enabled"]
        with exclusive_process_lock(service.status_path.parent / ".service.lock") as ok:
            assert not ok
        service.stop()

    service.run(on_started=on_started)
    scheduler.stop.assert_called_once()
    assert runtime.read_service_status(service_config)["state"] == "stopped"
    assert not runtime.read_service_status(service_config)["ready"]
    with exclusive_process_lock(service.status_path.parent / ".service.lock") as ok:
        assert ok


def test_second_instance_cannot_overwrite_running_status(service_config, scheduler):
    first = runtime.RuntimeService(service_config)

    def on_started():
        before = first.status_path.read_bytes()
        with pytest.raises(RuntimeError, match="already running"):
            runtime.RuntimeService(service_config).run()
        assert first.status_path.read_bytes() == before
        first.stop()

    first.run(on_started=on_started)


@pytest.mark.parametrize("age", [61, -10])
def test_stale_or_future_status_is_not_ready(service_config, age):
    service = runtime.RuntimeService(service_config, scheduling=False)
    service.status_path.parent.mkdir(parents=True)
    service.status_path.write_text(
        json.dumps(
            {
                "state": "running",
                "updated_at": (
                    datetime.now(timezone.utc) - timedelta(seconds=age)
                ).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    assert not runtime.read_service_status(service_config)["ready"]


@pytest.mark.parametrize(
    "contents", ["{", "[]", '{"updated_at": "invalid"}', '{"bot_threads_alive": []}']
)
def test_corrupt_status_is_unavailable(service_config, contents):
    service = runtime.RuntimeService(service_config, scheduling=False)
    service.status_path.parent.mkdir(parents=True)
    service.status_path.write_text(contents, encoding="utf-8")
    assert runtime.read_service_status(service_config)["state"] == "unavailable"


def test_interactive_mode_requires_an_enabled_bot(service_config):
    with pytest.raises(ValueError, match="at least one enabled Bot"):
        runtime.RuntimeService(service_config, scheduling=False).run()
    assert runtime.read_service_status(service_config)["state"] == "failed"


def test_bot_validation_precedes_scheduling_and_startup_notification(
    service_config, scheduler, monkeypatch
):
    from src.interactive import telegram_bot

    bot = Mock()
    bot.validate_config.side_effect = ValueError("allowed_chat_ids is required")
    monkeypatch.setattr(telegram_bot, "TelegramBot", lambda config: bot)
    service_config["interactive"] = {"telegram": {"enabled": True}}
    callback = Mock()
    with pytest.raises(ValueError, match="allowed_chat_ids"):
        runtime.RuntimeService(service_config).run(on_started=callback)
    scheduler.start.assert_not_called()
    callback.assert_not_called()
    bot.stop.assert_called_once()


def test_bot_failure_stops_scheduler_and_produces_failed_status(
    service_config, scheduler, monkeypatch
):
    bot = Mock()
    bot.run.side_effect = RuntimeError("test transport failed")
    service = runtime.RuntimeService(service_config)
    monkeypatch.setattr(service, "_configure_bots", lambda: service.bots.update(tg=bot))
    with pytest.raises(RuntimeError, match="tg receiver failed"):
        service.run()
    scheduler.stop.assert_called_once()
    bot.stop.assert_called_once()
    assert not service.threads["tg"].is_alive()
    assert runtime.read_service_status(service_config)["state"] == "failed"


def test_normal_stop_joins_receiver(service_config, scheduler, monkeypatch):
    bot = Mock()
    exited = threading.Event()
    bot.run.side_effect = exited.wait
    bot.stop.side_effect = exited.set
    service = runtime.RuntimeService(service_config)
    monkeypatch.setattr(service, "_configure_bots", lambda: service.bots.update(tg=bot))
    service.run(on_started=service.stop)
    assert not service.threads["tg"].is_alive()
    assert runtime.read_service_status(service_config)["state"] == "stopped"


def test_shutdown_failure_does_not_skip_remaining_cleanup(
    service_config, scheduler, monkeypatch
):
    stopped = threading.Event()
    first, second = Mock(), Mock()
    first.run.side_effect = stopped.wait
    second.run.side_effect = stopped.wait
    first.stop.side_effect = RuntimeError("test stop failure")
    second.stop.side_effect = stopped.set
    service = runtime.RuntimeService(service_config)
    monkeypatch.setattr(
        service, "_configure_bots", lambda: service.bots.update(a=first, b=second)
    )
    with pytest.raises(RuntimeError, match="a receiver shutdown failed"):
        service.run(on_started=service.stop)
    second.stop.assert_called_once()
    scheduler.stop.assert_called_once()
    assert all(not thread.is_alive() for thread in service.threads.values())
    assert runtime.read_service_status(service_config)["state"] == "failed"


@pytest.mark.parametrize("args,scheduling", [([], True), (["--service"], True),
                                           (["--interactive"], False)])
def test_default_and_explicit_service_use_same_host(
    args, scheduling, service_config, monkeypatch
):
    service = Mock()
    service_factory = Mock(return_value=service)
    monkeypatch.setattr(main, "load_config", lambda: service_config)
    monkeypatch.setattr(main, "setup_logging", lambda config: logging.getLogger(__name__))
    monkeypatch.setattr(runtime, "RuntimeService", service_factory)
    assert main.main(args) == 0
    service_factory.assert_called_once_with(service_config, scheduling=scheduling)
    service.run.assert_called_once()


def test_status_cli_does_not_start_service_or_send_notifications(
    service_config, monkeypatch, capsys
):
    monkeypatch.setattr(main, "load_config", lambda: service_config)
    setup = Mock()
    monkeypatch.setattr(main, "setup_logging", setup)
    assert main.main(["--status"]) == 1
    assert json.loads(capsys.readouterr().out)["ready"] is False
    setup.assert_not_called()


def test_retired_health_cli_is_rejected():
    with pytest.raises(SystemExit) as error:
        main._build_argument_parser().parse_args(["--health-server"])
    assert error.value.code == 2


def test_benchmark_config_error_logs_original_problem(monkeypatch, caplog):
    monkeypatch.setattr(
        main, "get_market_optimizer_configs", Mock(side_effect=ValueError("bad profile"))
    )
    assert main._fetch_benchmarks({}, SimpleNamespace()) == {}
    assert "bad profile" in caplog.text


@pytest.mark.parametrize("notify", [False, True])
def test_startup_notification_requires_explicit_option(
    notify, service_config, monkeypatch
):
    service = Mock()
    notification = Mock()
    monkeypatch.setattr(main, "load_config", lambda: service_config)
    monkeypatch.setattr(main, "setup_logging", lambda config: logging.getLogger(__name__))
    monkeypatch.setattr(runtime, "RuntimeService", lambda *args, **kwargs: service)
    monkeypatch.setattr(main, "_send_restart_notification", notification)
    assert main.main(["--service"] + (["--notify-start"] if notify else [])) == 0
    callback = service.run.call_args.kwargs["on_started"]
    if notify:
        callback()
        notification.assert_called_once_with(service_config, service)
    else:
        assert callback is None
        notification.assert_not_called()
