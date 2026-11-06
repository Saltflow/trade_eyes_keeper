"""Prepared quote snapshot deduplication."""

from datetime import datetime

import pandas as pd
import pytest

from src.core.report_quotes import prepare_report_quotes
from src.models.schemas import SessionContext
from src.utils.market_status import is_unchanged_report, mark_pushed


def _prepared_quotes(day="2026-11-06", *, recover=False, observed="09:55:00", price=10):
    """Use the real quality assessment and Session dataframe, including gaps."""
    session = SessionContext(
        session_id="offline-fingerprint", config={"stocks": ["601088", "00883"]}
    )
    quote = {
        "quote_time": day + "T09:54:00+08:00",
        "open": 10,
        "close": price,
        "high": 12,
        "low": 9,
        "prev_close": 10,
    }
    prepare_report_quotes(
        session,
        lambda code: quote if code == "601088" or recover else None,
        clock=lambda: datetime.fromisoformat(day + "T" + observed + "+08:00"),
    )
    return session.get_all_dataframe()


def test_quote_gap_cannot_hide_the_next_trading_days_changed_snapshot(tmp_path):
    path = tmp_path / "last_pushed.txt"
    first = _prepared_quotes()
    mark_pushed(path, first)
    assert path.read_text().startswith("quote-v1:")
    assert path.read_text() not in {"None", "NaT", "nan"}
    assert is_unchanged_report(first, path)
    next_day = _prepared_quotes("2026-11-09", price=11)
    assert next_day.loc[next_day.stock_code == "00883", "date"].isna().all()
    assert not is_unchanged_report(next_day, path)


def test_same_day_quote_recovery_is_not_hidden_by_date_fingerprint(tmp_path):
    path = tmp_path / "last_pushed.txt"
    mark_pushed(path, _prepared_quotes())
    recovered = _prepared_quotes(recover=True)
    assert not is_unchanged_report(recovered, path)
    mark_pushed(path, recovered)
    assert is_unchanged_report(recovered, path)


def test_new_source_time_price_quality_or_gap_reason_all_change_fingerprint(tmp_path):
    path = tmp_path / "last_pushed.txt"
    first = _prepared_quotes()
    mark_pushed(path, first)
    assert not is_unchanged_report(_prepared_quotes(price=11), path)
    delayed = _prepared_quotes(observed="10:00:00")
    assert (
        delayed.loc[delayed.stock_code == "601088", "quote_status"].iloc[0] == "delayed"
    )
    assert not is_unchanged_report(delayed, path)
    for field, value in (
        ("quote_as_of", "2026-11-06T01:54:30+00:00"),
        ("quote_reason", "报价源时间未知"),
    ):
        changed = first.copy()
        changed.loc[changed.stock_code == "00883", field] = value
        assert not is_unchanged_report(changed, path)


def test_recorded_attempt_is_not_replayed_for_new_observation_clock(tmp_path):
    # The caller records sent/unknown/partial attempts; fingerprinting must not
    # turn another collection clock into permission to resend that observation.
    path = tmp_path / "last_attempt.txt"
    mark_pushed(path, _prepared_quotes())
    observed_again = _prepared_quotes(observed="09:55:10").iloc[::-1].copy()
    observed_again["created_at"] = "2026-11-06T09:55:11+08:00"
    assert is_unchanged_report(observed_again, path)


def test_quote_fingerprint_replaces_legacy_marker_and_normalizes_missing(tmp_path):
    path = tmp_path / "last_pushed.txt"
    path.write_text("2026-11-06")
    frame = _prepared_quotes()
    assert not is_unchanged_report(frame, path)
    mark_pushed(path, frame)
    absent = frame.loc[frame.stock_code == "00883"].index
    frame.loc[absent, "close"] = float("nan")
    assert is_unchanged_report(frame, path)


@pytest.mark.parametrize("frame", [None, pd.DataFrame()])
def test_empty_snapshot_does_not_send_or_overwrite_attempt(tmp_path, frame):
    path = tmp_path / "last_attempt.txt"
    path.write_text("existing-receipt")
    assert is_unchanged_report(frame, path)
    mark_pushed(path, frame)
    assert path.read_text() == "existing-receipt"


def test_first_snapshot_records_attempt_and_rejects_unassessed_bars(tmp_path):
    path = tmp_path / "last_attempt.txt"
    snapshot = _prepared_quotes()
    assert not is_unchanged_report(snapshot, path)
    mark_pushed(path, snapshot)
    assert is_unchanged_report(snapshot, path)
    with pytest.raises(ValueError, match="assessed quotes"):
        is_unchanged_report(snapshot.drop(columns="quote_status"), path)
