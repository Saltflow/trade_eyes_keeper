"""Atomic raw-config writes must never persist runtime secrets or lose edits."""

from __future__ import annotations

import multiprocessing
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from src.core.config_store import ConfigStore, load_config, runtime_config
from src.core.process_lock import exclusive_process_lock
from src.interactive.commands import handlers


def _config_file(tmp_path: Path, config: dict) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


def _increment_in_process(path: str, start_event) -> None:
    start_event.wait(10)
    store = ConfigStore(path)
    for _ in range(6):

        def increment(config: dict) -> None:
            value = config["counter"]
            time.sleep(0.01)
            config["counter"] = value + 1

        store.update(increment)


def test_runtime_overrides_never_change_raw_config(tmp_path, monkeypatch):
    raw = {
        "email": {"sender_password": "stored-placeholder"},
        "llm": {"api_key": "yaml-placeholder"},
        "stocks": ["600036"],
    }
    path = _config_file(tmp_path, raw)
    monkeypatch.setenv("EMAIL_PASSWORD", "runtime-only-password")
    monkeypatch.setenv("DEEPSEEK_API_KEY", " runtime-only-api-key ")

    runtime = load_config(path)
    assert runtime["email"]["sender_password"] == "runtime-only-password"
    assert runtime["llm"]["api_key"] == "runtime-only-api-key"
    runtime["stocks"].append("SHOULD_NOT_PERSIST")
    store = ConfigStore(path)
    store.update(lambda config: config["stocks"].append("GOOG"))

    saved = store.load_raw()
    assert saved["email"] == raw["email"]
    assert saved["llm"] == raw["llm"]
    assert saved["stocks"] == ["600036", "GOOG"]
    assert "runtime-only" not in path.read_text(encoding="utf-8")


def test_runtime_copy_preserves_empty_env_fallbacks():
    raw = {"email": {"sender_email": "yaml@example.test"}}
    resolved = runtime_config(raw, {"EMAIL_SENDER": "", "DEEPSEEK_API_KEY": "   "})
    assert resolved == raw
    resolved["email"]["sender_email"] = "changed@example.test"
    assert raw["email"]["sender_email"] == "yaml@example.test"


def test_selected_config_loads_its_dotenv_without_overriding_process_env(
    tmp_path, monkeypatch
):
    path = _config_file(tmp_path, {"stocks": []})
    monkeypatch.setenv("EMAIL_PASSWORD", "process-password")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    (tmp_path / ".env").write_text(
        "EMAIL_PASSWORD=dotenv-password\nDEEPSEEK_API_KEY=dotenv-api-key\n",
        encoding="utf-8",
    )
    resolved = load_config(path)
    assert resolved["email"]["sender_password"] == "process-password"
    assert resolved["llm"]["api_key"] == "dotenv-api-key"
    assert ConfigStore(path).load_raw() == {"stocks": []}


@pytest.mark.parametrize("contents", ["", "[]", "false", "scheduler: ["])
def test_invalid_yaml_is_not_overwritten(tmp_path, contents):
    path = tmp_path / "config.yaml"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises((TypeError, yaml.YAMLError)):
        ConfigStore(path).update(lambda config: config.update(stocks=[]))
    assert path.read_text(encoding="utf-8") == contents


def test_validation_failure_preserves_original_and_cleans_temp(tmp_path):
    path = _config_file(tmp_path, {"counter": 0})
    before = path.read_bytes()

    def reject(temporary_path: Path) -> None:
        assert ConfigStore(temporary_path).load_raw()["counter"] == 1
        raise ValueError("candidate is invalid")

    with pytest.raises(ValueError, match="candidate is invalid"):
        ConfigStore(path).update(
            lambda config: config.update(counter=1), validate=reject
        )
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


def test_replace_failure_preserves_original_and_cleans_temp(tmp_path, monkeypatch):
    path = _config_file(tmp_path, {"counter": 0})
    before = path.read_bytes()

    def fail_replace(*_args):
        raise OSError("read-only destination")

    monkeypatch.setattr("src.core.config_store.os.replace", fail_replace)
    with pytest.raises(OSError, match="read-only"):
        ConfigStore(path).update(lambda config: config.update(counter=1))
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


def test_lock_timeout_does_not_modify_file(tmp_path):
    path = _config_file(tmp_path, {"counter": 0})
    store = ConfigStore(path)
    with exclusive_process_lock(store.lock_path) as acquired:
        assert acquired
        with pytest.raises(TimeoutError, match="configuration is busy"):
            store.update(lambda config: config.update(counter=1), lock_timeout=0)
    assert store.load_raw()["counter"] == 0


def test_concurrent_processes_do_not_lose_updates(tmp_path):
    path = _config_file(tmp_path, {"counter": 0, "unrelated": "keep"})
    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    processes = [
        context.Process(target=_increment_in_process, args=(str(path), start_event))
        for _ in range(2)
    ]
    try:
        for process in processes:
            process.start()
        start_event.set()
        for process in processes:
            process.join(timeout=20)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
    assert ConfigStore(path).load_raw() == {"counter": 12, "unrelated": "keep"}


def test_bot_field_updates_preserve_concurrent_changes_and_secrets(
    tmp_path, monkeypatch
):
    path = _config_file(
        tmp_path,
        {"stocks": ["600036"], "scheduler": {"run_time": "19:00"}},
    )
    monkeypatch.setattr(handlers, "CONFIG_PATH", path)
    monkeypatch.setenv("EMAIL_PASSWORD", "runtime-secret")
    assert handlers._load_config()["email"]["sender_password"] == "runtime-secret"

    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(
            pool.map(handlers.handle_add, [["GOOG"], ["00883"], ["601985"]])
        )
    assert all(response.startswith("✅") for response in responses)
    handlers.handle_daily_report_frequency("weekly")
    handlers.handle_skip("search", ["GOOG"])
    handlers.handle_remove(["00883"])

    raw = ConfigStore(path).load_raw()
    assert set(raw["stocks"]) == {"600036", "GOOG", "601985"}
    assert raw["scheduler"] == {"run_time": "19:00", "daily_report_frequency": "weekly"}
    assert raw["skip_search"] == ["GOOG"]
    assert "email" not in raw
