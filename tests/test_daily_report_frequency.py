"""日报频次开关测试。"""

from datetime import datetime

import yaml


def test_should_send_daily_report_modes():
    from main import should_send_daily_report

    assert should_send_daily_report({"scheduler": {"daily_report_frequency": "daily"}})
    assert should_send_daily_report(
        {"scheduler": {"daily_report_frequency": "weekly"}},
        now=datetime(2026, 9, 4),  # Friday
    )
    assert not should_send_daily_report(
        {"scheduler": {"daily_report_frequency": "weekly"}},
        now=datetime(2026, 9, 3),  # Thursday
    )
    assert not should_send_daily_report(
        {"scheduler": {"daily_report_frequency": "off"}},
    )


def test_should_send_daily_report_force_overrides_frequency():
    from main import should_send_daily_report

    assert should_send_daily_report(
        {"scheduler": {"daily_report_frequency": "off"}},
        force=True,
    )


def test_frequency_handler_persists_selected_mode(tmp_path, monkeypatch):
    from src.interactive.commands import handlers

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({"scheduler": {"run_time": "19:00"}}, allow_unicode=True),
        encoding="utf-8",
    )
    monkeypatch.setattr(handlers, "CONFIG_PATH", config_path)

    response = handlers.handle_daily_report_frequency("weekly")

    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["scheduler"]["daily_report_frequency"] == "weekly"
    assert "每周五" in response
