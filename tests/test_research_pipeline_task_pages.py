"""Origin-scoped research pages over the actual SQLite store, without workers."""

import pytest

from src.research_pipeline.models import ResearchSpec
from src.research_pipeline.store import Store
from src.research_pipeline.tools import ResearchTools


@pytest.fixture
def tasks(tmp_path, monkeypatch):
    monkeypatch.setenv("SKIP_EMAIL", "true")
    monkeypatch.setenv("SKIP_NOTIFICATIONS", "true")
    store = Store(tmp_path / "research")
    origins = [
        ("feishu", "alice", "chat-a"),
        ("feishu", "bob", "chat-a"),
        ("feishu", "alice", "chat-b"),
        ("feishu", "alice", "chat-a"),
        ("cli", "alice", "chat-a"),
        ("feishu", "alice", "chat-a"),
        (None, None, None),
        ("feishu", "", ""),
        ("feishu", "alice", "chat-a"),
    ]
    ids = {}
    for index, (channel, owner, chat) in enumerate(origins):
        run_id = f"run-{index}"
        origin = {"channel": channel, "sender": owner, "chat": chat} if channel else {}
        spec = ResearchSpec(
            companies=("us:TEST",),
            output=tmp_path / run_id,
            repo=tmp_path,
            store_root=store.root,
            origin=origin,
        )
        store.create_run(spec.to_dict(), run_id)
        ids.setdefault((channel, owner, chat), []).append(run_id)
    return ResearchTools(tmp_path, store), ids


def test_research_pages_filter_origin_before_offset_and_preserve_native_ids(tasks):
    tools, ids = tasks
    expected = list(reversed(ids["feishu", "alice", "chat-a"]))
    first = tools.call("research_pipeline_tasks", {"limit": 2}, "chat-a", "alice")
    assert [row["run_id"] for row in first["runs"]] == expected[:2]
    assert first["next_offset"] == 2 and first["origin_scoped"] is True
    second = tools.call(
        "research_pipeline_tasks",
        {"limit": 2, "offset": first["next_offset"]},
        "chat-a",
        "alice",
    )
    assert [row["id"] for row in second["runs"]] == expected[2:]
    assert second["next_offset"] is None
    assert all(row["state"] == "created" and "spec" not in row for row in first["runs"])
    exhausted = tools.call("research_pipeline_tasks", {"offset": 4}, "chat-a", "alice")
    assert exhausted["runs"] == [] and exhausted["next_offset"] is None
    for owner, chat in [("bob", "chat-a"), ("alice", "chat-b")]:
        page = tools.call("research_pipeline_tasks", {}, chat, owner)
        assert {row["id"] for row in page["runs"]} == set(ids["feishu", owner, chat])
    assert tools.call("research_pipeline_tasks", {}, "chat-a", "nobody")["runs"] == []
    with pytest.raises(ValueError, match="unknown_research_task_query_parameter"):
        tools.call("research_pipeline_tasks", {"sender": "bob"}, "chat-a", "alice")


def test_research_invalid_page_parameters_are_rejected(tasks):
    tools, _ = tasks
    for arguments in [
        {"limit": 0},
        {"limit": 21},
        {"limit": True},
        {"offset": -1},
        {"offset": True},
    ]:
        with pytest.raises(ValueError, match="invalid_research_task_"):
            tools.call("research_pipeline_tasks", arguments, "chat-a", "alice")


def test_missing_identity_cannot_list_incompletely_bound_history(tasks):
    tools, ids = tasks
    assert ids["feishu", "", ""]
    for chat, owner in [("", ""), (None, "alice"), ("chat-a", None)]:
        with pytest.raises(PermissionError, match="research_origin_not_authorized"):
            tools.call("research_pipeline_tasks", {}, chat, owner)
