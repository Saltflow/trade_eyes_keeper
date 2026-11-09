"""Shared market collection clocks; no network or production data."""

from datetime import datetime

from src.personal_monitor.tasks import market_deadlines


def clock(text="2026-10-21T14:31:00+08:00"):
    return datetime.fromisoformat(text)


def test_catchup_within_existing_grace_and_future_deadline():
    config = {
        "scheduler": {
            "run_time": "19:00",
            "daily_enabled": False,
            "brief_reports": [
                {"run_time": "14:30", "enabled": False, "skip_weekends": True}
            ],
        }
    }
    catchup, future = market_deadlines(config, clock())
    assert catchup == clock("2026-10-21T14:30:00+08:00")
    assert future == clock("2026-10-21T19:00:00+08:00")
    assert market_deadlines(config, clock("2026-10-21T14:46:00+08:00"))[0] is None
    assert market_deadlines(config, clock("2026-10-24T14:31:00+08:00"))[0] is None
    assert market_deadlines(config, clock("2026-10-24T19:01:00+08:00"))[0]
