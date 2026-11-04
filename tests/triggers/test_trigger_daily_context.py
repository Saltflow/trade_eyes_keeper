"""Offline disclosure-to-daily contracts; no business report or transport runs.

Events and sessions are synthetic data fixtures. The actual evaluator, binding
projection and event-text builder run; only the notification decision AST is
isolated from main.py so no data preparation, optimizer, HTML or sender starts.
"""

import ast
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

from src.notification.report_builders import build_daily_events_text
from src.triggers.evaluator import event_occurrences
from src.triggers.facts import AnnouncementEvent, FactSnapshot
from src.triggers.models import normalize_spec
from src.triggers.report_context import is_managed_daily, populate_trigger_announcements
from src.triggers.runtime import TriggerRuntime

PUBLISHED = datetime(2026, 10, 31, 18, 20, tzinfo=timezone.utc)
NEXT_DAY = PUBLISHED + timedelta(days=1, hours=8)


def event(code="601088", suffix="full"):
    """A synthetic source published November 1 in the source's UTC+8 session."""
    title = f"合成{code}股份有限公司2026年第三季度报告"
    url = f"https://disclosure.example.test/{code}/2026Q3-{suffix}.pdf"
    return AnnouncementEvent(
        id=f"fixture-{code}-{suffix}",
        kind="announcement.q3_report",
        symbol=code,
        occurred_at=PUBLISHED,
        published_at=PUBLISHED,
        report_period="2026Q3",
        source="synthetic_official_fixture",
        title=title,
        url=url,
        publication_date=(PUBLISHED + timedelta(hours=8)).date(),
        observed_at=PUBLISHED + timedelta(minutes=1),
        source_documents=(
            {
                "title": title,
                "url": url,
                "published_at": PUBLISHED.isoformat(),
                "source_id": f"fixture-doc-{code}",
            },
        ),
    )


def occurrence(source=None, now=PUBLISHED):
    source = source or event()
    rule = normalize_spec(
        {
            "label": "合成Q3",
            "event": {
                "kind": "announcement.q3_report",
                "report_period": "Q3",
                "day_offsets": [0, 1],
            },
            "actions": [{"kind": "daily"}],
        }
    )["event"]
    snapshot = FactSnapshot(as_of=now, observed_at=now, events=[source])
    return next(event_occurrences(rule, snapshot, now))


def binding(tmp_path, contexts):
    """_binding is a projection; authentication/run guards have separate tests."""
    runtime = object.__new__(TriggerRuntime)
    runtime.config_path = tmp_path / "synthetic-profile.yaml"
    actions = [
        {
            "id": f"fixture-action-{index}",
            "owner": "fixture-owner",
            "number": 3,
            "revision": 1,
            "kind": "daily",
            "action_index": 0,
            "payload": {"chat_id": "fixture-chat"},
            "context": context,
        }
        for index, context in enumerate(contexts)
    ]
    return runtime._binding(actions[0], "合成三季报日报", actions)


def test_occurrence_preserves_original_formal_source_and_local_publication_date():
    source = event()
    result = occurrence(source)
    context = result["context"]
    assert context["title"] == source.title
    assert context["url"] == source.url
    assert datetime.fromisoformat(context["published_at"]) == source.published_at
    assert context["publication_date"] == "2026-11-01"
    assert context["source"] == source.source
    assert context["source_documents"] == list(source.source_documents)
    assert json.loads(json.dumps(context, allow_nan=False)) == context


def test_next_day_occurrence_keeps_original_publication_and_document_evidence():
    source = event()
    context = occurrence(source, now=NEXT_DAY)["context"]
    assert context["day_offset"] == 1 and context["date"] == "2026-11-02"
    assert context["publication_date"] == "2026-11-01"
    assert datetime.fromisoformat(context["published_at"]) == PUBLISHED
    assert context["source_documents"][0]["url"] == source.url


@pytest.mark.parametrize("count", [1, 2])
def test_binding_retains_each_event_context_when_reports_are_coalesced(tmp_path, count):
    sources = [event("601088"), event("600036")][:count]
    contexts = [occurrence(item)["context"] for item in sources]
    before = deepcopy(contexts)
    result = binding(tmp_path, contexts)
    assert result["events"] == contexts
    assert len(result["events"]) == count
    assert result["owner"] == "fixture-owner" and result["kind"] == "daily"
    if count == 2:
        assert result["action_ids"] == ["fixture-action-0", "fixture-action-1"]
    result["events"][0]["source_documents"][0]["title"] = "仅修改拷贝"
    assert contexts == before
    json.dumps(result, allow_nan=False)


def test_context_merge_keeps_existing_announcements_and_never_mutates_sources(tmp_path):
    sources = [event("601088"), event("600036")]
    routed = {
        "_trigger_context": binding(
            tmp_path, [occurrence(item)["context"] for item in sources]
        )
    }
    previous = {
        "00700": [
            {
                "title": "原有合成公告",
                "date": "2026-10-30",
                "url": "https://disclosure.example.test/previous.pdf",
            }
        ]
    }
    old, config_before = deepcopy(previous), deepcopy(routed)
    session = SimpleNamespace(
        announcements=previous,
        placements={},
        dividend_events=[],
    )
    populate_trigger_announcements(session, routed)
    assert previous == old and routed == config_before
    assert session.announcements is not previous
    assert session.announcements["00700"] == previous["00700"]
    for source in sources:
        item = session.announcements[source.symbol][0]
        assert (
            item["title"],
            item["url"],
            item["date"],
            item["exchange"],
            item["report_period"],
        ) == (source.title, source.url, "2026-11-01", source.source, "2026Q3")
        assert datetime.fromisoformat(item["published_at"]) == source.published_at
    text = build_daily_events_text(session, pd.DataFrame())
    assert "原有合成公告" in text
    assert all(source.title in text and source.url in text for source in sources)
    assert "2026-11-01" in text


def test_duplicate_url_preserves_the_preexisting_source_record(tmp_path):
    source = event()
    original = {
        "title": source.title,
        "url": source.url,
        "date": "2026-11-01",
        "existing_provenance": "fixture",
    }
    session = SimpleNamespace(announcements={source.symbol: [original]})
    routed = {"_trigger_context": binding(tmp_path, [occurrence(source)["context"]])}
    populate_trigger_announcements(session, routed)
    populate_trigger_announcements(session, routed)
    assert session.announcements[source.symbol] == [original]


@pytest.mark.parametrize(
    "patch",
    [
        {"title": ""},
        {"url": ""},
        {"url": "javascript:alert(1)"},
        {"url": "file:///fixture/private"},
        {"url": "//example.test/file.pdf"},
        {"url": "https://user:password@example.test/file.pdf"},
        {"url": "https://example.test/file\n.pdf"},
    ],
)
def test_incomplete_or_unsafe_source_is_not_fabricated_into_a_report(tmp_path, patch):
    context = {**occurrence()["context"], **patch}
    session = SimpleNamespace(announcements={})
    populate_trigger_announcements(
        session, {"_trigger_context": binding(tmp_path, [context])}
    )
    assert session.announcements == {}


def daily_notification_decision():
    """Compile only the real notification branch, without importing main.py."""
    path = Path(__file__).resolve().parents[2] / "main.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    daily = next(
        item
        for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == "run_daily_task"
    )
    candidates = [
        node
        for node in ast.walk(daily)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "managed_daily"
        and any(
            isinstance(item, ast.Attribute)
            and item.attr == "send_daily_report_from_session"
            for item in ast.walk(node)
        )
    ]
    assert len(candidates) == 1, (
        "managed daily must own a full-report branch"
    )
    module = ast.parse(
        "def decide(config, session, notifier, managed_daily):\n    delivery = None\n    trigger_mode = False\n    force = True\n    daily_triggers = []\n    skip_reason = ''\n"
    )
    function = module.body[0]
    function.body.extend(
        [
            deepcopy(candidates[0]),
            ast.Return(value=ast.Name(id="delivery", ctx=ast.Load())),
        ]
    )
    namespace = {
        "logger": Mock(),
        "should_send_daily_report": lambda config, force=False: True,
    }
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)  # noqa: S102 - trusted local branch, never external text
    return namespace["decide"], daily


def test_managed_daily_uses_full_report_and_all_source_content(tmp_path):
    sources = [event("601088"), event("600036")]
    config = {
        "_trigger_context": binding(
            tmp_path, [occurrence(source)["context"] for source in sources]
        )
    }
    session = SimpleNamespace(
        announcements={},
        placements={},
        dividend_events=[],
    )
    populate_trigger_announcements(session, config)
    notifier = Mock(spec=["send_daily_report_from_session"])
    notifier.send_daily_report_from_session.side_effect = lambda supplied: {
        "status": "sent",
        "body": build_daily_events_text(supplied, pd.DataFrame()),
    }
    decide, daily = daily_notification_decision()
    result = decide(config, session, notifier, is_managed_daily(config))
    notifier.send_daily_report_from_session.assert_called_once_with(session)
    assert all(
        source.title in result["body"] and source.url in result["body"]
        for source in sources
    )
    # Lock the production wiring too: the merge belongs after disclosure fetch
    # and before dividend resolution/snapshot/delivery, not only in this fixture.
    calls = [node for node in ast.walk(daily) if isinstance(node, ast.Call)]
    merge = next(
        node
        for node in calls
        if isinstance(node.func, ast.Name)
        and node.func.id == "populate_trigger_announcements"
    )
    resolver = next(
        node
        for node in calls
        if isinstance(node.func, ast.Attribute)
        and node.func.attr == "public_schedule"
    )
    assert merge.lineno < resolver.lineno < candidates_delivery_line(daily)
    assert any(
        isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "managed_daily"
            for target in node.targets
        )
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "is_managed_daily"
        for node in ast.walk(daily)
    )


def candidates_delivery_line(daily):
    return min(
        node.lineno
        for node in ast.walk(daily)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "send_daily_report_from_session"
    )


def test_nonmanaged_daily_uses_the_same_full_report_entry():
    config = {"_trigger_context": {"kind": "brief"}}
    session = SimpleNamespace(announcements={}, placements={}, dividend_events=[])
    notifier = Mock(spec=["send_daily_report_from_session"])
    decide, _ = daily_notification_decision()
    decide(config, session, notifier, is_managed_daily(config))
    notifier.send_daily_report_from_session.assert_called_once_with(session)
