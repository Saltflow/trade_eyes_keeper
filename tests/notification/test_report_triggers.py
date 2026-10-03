from types import SimpleNamespace

from src.notification.report_triggers import evaluate_report_triggers


def test_empty_trigger_list_preserves_existing_sending_behavior():
    result = evaluate_report_triggers(SimpleNamespace(), [])

    assert result.matched is True
    assert result.configured == {}
    assert result.matched_conditions == ()


def test_configured_paths_and_operators_use_any_semantics():
    session = SimpleNamespace(
        dividend_events=[{"code": "600036"}],
        placements={"600036": {"is_locked": True}},
        announcements={"600036": [{"title": "2025年年度报告"}]},
        signal_scan=SimpleNamespace(
            alerts=[{"stock_code": "600036", "current_value": 7.0}]
        ),
    )

    result = evaluate_report_triggers(
        session,
        {
            "mode": "any",
            "conditions": [
                {"path": "dividend_events", "operator": "non_empty"},
                {
                    "path": "announcements.*.*.title",
                    "operator": "contains",
                    "value": "年度报告",
                },
                {
                    "path": "signal_scan.alerts.*.current_value",
                    "operator": "gt",
                    "value": 5,
                },
            ],
        },
    )

    assert result.matched is True
    assert result.matched_conditions == (0, 1, 2)


def test_no_matching_trigger_fails_closed():
    session = SimpleNamespace(
        dividend_events=[],
        placements={},
        announcements={"600036": [{"title": "股东大会决议"}]},
        signal_scan=SimpleNamespace(alerts=[]),
    )

    result = evaluate_report_triggers(
        session,
        {
            "mode": "any",
            "conditions": [
                {"path": "dividend_events", "operator": "non_empty"},
                {
                    "path": "announcements.*.*.title",
                    "operator": "contains",
                    "value": "年度报告",
                },
            ],
        },
    )

    assert result.matched is False
    assert result.matched_conditions == ()


def test_malformed_trigger_configuration_fails_closed():
    import pytest

    with pytest.raises(ValueError, match="path"):
        evaluate_report_triggers(
            SimpleNamespace(),
            {
                "mode": "any",
                "conditions": [{"path": "unknown_event", "operator": "non_empty"}],
            },
        )


def test_all_semantics_and_report_data_roots():
    from src.notification.report_triggers import required_report_data_roots

    session = SimpleNamespace(signal_scan=SimpleNamespace(alerts=[1]), alerts=[])
    configured = {
        "mode": "all",
        "conditions": [
            {"path": "signal_scan.alerts", "operator": "non_empty"},
            {"path": "alerts", "operator": "non_empty"},
        ],
    }

    assert required_report_data_roots(configured) == {"signal_scan", "alerts"}
    assert evaluate_report_triggers(session, configured).matched is False
