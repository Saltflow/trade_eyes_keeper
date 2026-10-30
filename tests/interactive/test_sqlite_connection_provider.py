"""Consumer connection reuse retains real transactions; all SDKs here are local stubs."""

import asyncio
import json
import multiprocessing
import os
import queue
import sqlite3
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.core.execution import ExecutionRuntime, ExecutionStore, JobSpec, ModuleSpec
from src.interactive.assistant.service import FeishuAssistant
from src.interactive.assistant.store import ProposalStore
from src.interactive.feishu_bot import FeishuBot, _run_sdk
from src.interactive.ingress import DurableIngress


@pytest.fixture(autouse=True)
def no_http(monkeypatch):
    monkeypatch.setattr(
        "requests.adapters.HTTPAdapter.send",
        Mock(side_effect=AssertionError("HTTP forbidden in provider tests")),
    )


def proposal(tmp_path, injected=True):
    owner = ExecutionStore(tmp_path / "profile.sqlite3")
    store = ProposalStore(owner.path)
    if injected:
        store.set_connection_provider(owner.connection)
    return store, owner


@pytest.mark.parametrize("injected", [False, True])
@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_proposal_provider_preserves_implicit_commit_and_baseexception_rollback(
    tmp_path, injected, failure
):
    store, owner = proposal(tmp_path, injected)
    try:
        with store.connection() as db:
            assert db.isolation_level == ""
            assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
            assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
            assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert isinstance(db.execute("SELECT 1").fetchone(), sqlite3.Row)
            db.execute("INSERT INTO receipts VALUES('committed',0)")
        with pytest.raises(failure), store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO receipts VALUES('rolled-back',0)")
            raise failure("fixture failure")
        with store.connection() as db:
            assert not db.in_transaction
            assert [r[0] for r in db.execute("SELECT id FROM receipts")] == [
                "committed"
            ]
    finally:
        owner.close_idle_connections()


def test_proposal_foreign_keys_apply_to_reused_replaced_and_nested_handles(tmp_path):
    store, owner = proposal(tmp_path)
    try:
        turn = store.begin_turn("chat", "owner", "one", "question")
        store.record_turn_event(turn, "evidence", {"ok": True})
        for reopen in (False, True):
            if reopen:
                owner.close_idle_connections()
            with owner.connection() as raw:
                raw.execute("PRAGMA foreign_keys=OFF")
            with store.connection() as outer:
                assert outer.execute("PRAGMA foreign_keys").fetchone()[0] == 1
                with store.connection() as inner:
                    assert inner is not outer
                    assert inner.execute("PRAGMA foreign_keys").fetchone()[0] == 1
                    with pytest.raises(sqlite3.IntegrityError):
                        inner.execute(
                            "INSERT INTO conversation_events(turn_id,at,kind,data) "
                            "VALUES('missing',0,'fixture','{}')"
                        )
        with store.connection() as db:
            db.execute("DELETE FROM conversation_turns WHERE id=?", (turn,))
        with store.connection() as db:
            assert (
                db.execute("SELECT COUNT(*) FROM conversation_events").fetchone()[0]
                == 0
            )
    finally:
        owner.close_idle_connections()


def test_proposal_nested_read_is_independent_and_never_commits_outer_admission(
    tmp_path,
):
    store, owner = proposal(tmp_path)
    try:
        with store.connection() as outer:
            outer.execute("BEGIN IMMEDIATE")
            outer.execute("INSERT INTO receipts VALUES('outer',0)")
            with store.connection() as inner:
                assert inner is not outer
                assert inner.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
            assert outer.in_transaction
            outer.rollback()
        with store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
    finally:
        owner.close_idle_connections()


def test_proposal_provider_keeps_cross_database_rollback_and_cas_results(tmp_path):
    store, owner = proposal(tmp_path)
    other = ProposalStore(tmp_path / "other.sqlite3")
    other.set_connection_provider(owner.connection)
    try:
        first = store.begin_turn("chat", "owner", "one", "first")
        second = other.begin_turn("chat", "owner", "two", "second")
        with pytest.raises(RuntimeError), other.connection() as db:
            db.execute("INSERT INTO receipts VALUES('no',0)")
            raise RuntimeError("rollback")
        assert store.turn(first) and store.turn(second) is None
        assert other.turn(second) and other.turn(first) is None
        identity, changed, _state = store.prepare_reply(first, "piece", "chat", "text")
        assert changed
        with store.connection() as db:
            assert db.total_changes > 0
        assert store.prepare_reply(first, "piece", "chat", "text") == (
            identity,
            False,
            "submitted",
        )
        with other.connection() as db:
            assert not db.execute("SELECT 1 FROM receipts WHERE id='no'").fetchone()
    finally:
        owner.close_idle_connections()


def test_repeated_turn_event_finish_and_read_reuse_one_genuine_connection(
    tmp_path, monkeypatch
):
    store, owner = proposal(tmp_path)
    actual = sqlite3.connect
    opened = []

    def connect(*args, **kwargs):
        opened.append(args[0])
        return actual(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    try:
        turn = store.begin_turn("chat", "owner", "one", "first question")
        for index in range(5):
            assert store.begin_turn("chat", "owner", "one", "duplicate") == turn
            store.record_turn_event(turn, "fixture", {"rank": index})
            store.finish_turn(turn, "completed", answer="reply")
            assert store.turn(turn)["question"] == "first question"
            assert len(store.recent_turns("chat", "owner")[0]["events"]) == index + 1
        action = store.create("fixture", "chat", "owner", {})
        store.update(action["id"], "completed")
        assert len(opened) == 1
        assert os.path.normcase(str(opened[0])) == os.path.normcase(str(owner.path))
    finally:
        owner.close_idle_connections()


def test_proposal_admission_provider_keeps_receipt_turn_and_job_atomic(tmp_path):
    store, owner = proposal(tmp_path)
    owner.apply(
        ModuleSpec("conversation", (JobSpec("conversation.turn", lambda *_: None),))
    )
    envelope = {"chat": "chat", "sender": "owner", "message_id": "one", "text": "hello"}
    try:
        with store.connection() as db:
            db.execute(
                "CREATE TRIGGER reject_work BEFORE INSERT ON execution_runs "
                "BEGIN SELECT RAISE(ABORT,'injected'); END"
            )
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            store.admit_turn(envelope, owner)
        with store.connection() as db:
            for table in ("receipts", "conversation_turns", "execution_runs"):
                assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            db.execute("DROP TRIGGER reject_work")
        assert store.admit_turn(envelope, owner)["accepted"]
        assert store.admit_turn(envelope, owner)["duplicate"]
        with pytest.raises(ValueError, match="another owner"):
            store.admit_turn(dict(envelope, sender="foreign"), owner)
    finally:
        owner.close_idle_connections()


def test_assistant_binds_its_store_after_matching_runtime_identity(tmp_path):
    runtime = ExecutionRuntime(ExecutionStore(tmp_path / "profile.sqlite3"))
    store = ProposalStore(runtime.store.path)
    assistant = FeishuAssistant(
        {"interactive": {"feishu": {"assistant": {"enabled": True}}}},
        Mock(),
        project_root=tmp_path,
        client=Mock(),
        store=store,
        execution_runtime=runtime,
        configuration=Mock(),
        research=Mock(),
        capabilities={"personal"},
        start_workers=False,
    )
    try:
        assert store._connection_provider == runtime.store.connection
        with store.connection() as first:
            pass
        with store.connection() as second:
            assert first is second
    finally:
        assistant.stop()
        runtime.stop()


def raw_event(message_id="message", text="holdings"):
    return {
        "header": {"event_type": "im.message.receive_v1", "app_id": "fixture"},
        "event": {
            "message": {
                "chat_id": "chat",
                "message_id": message_id,
                "content": json.dumps({"text": text}),
            }
        },
    }


@pytest.mark.parametrize("injected", [False, True])
def test_ingress_provider_keeps_commit_access_rollback_and_restart(tmp_path, injected):
    owner = ExecutionStore(tmp_path / "profile.sqlite3")
    options = {"connection_provider": owner.connection} if injected else {}
    inbox = DurableIngress(owner.path, "fixture", ["chat"], **options)
    try:
        identity = inbox.accept(raw_event())
        assert inbox.accept(raw_event()) == identity
        with inbox.connection() as db:
            assert db.isolation_level == ""
            assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
            assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
        with pytest.raises(RuntimeError):
            inbox.check_access(identity, Mock(side_effect=RuntimeError("retry")))
        checker = Mock(return_value="allowed")
        assert inbox.check_access(identity, checker) == "allowed"
        assert inbox.check_access(identity, checker) == "allowed"
        checker.assert_called_once()
        owner.close_idle_connections()
        restarted = DurableIngress(owner.path, "fixture", ["chat"], **options)
        assert len(restarted.pending()) == 1
        handler = Mock(side_effect=["unavailable", "accepted"])
        restarted.dispatch(None, handler)
        assert len(restarted.pending()) == 1
        restarted.dispatch(None, handler)
        assert not restarted.pending()
        assert not restarted.accept(
            raw_event("other") | {"header": {"app_id": "fixture"}}
        )
    finally:
        owner.close_idle_connections()


def test_shared_provider_close_never_closes_active_ingress_borrow(tmp_path):
    owner = ExecutionStore(tmp_path / "profile.sqlite3")
    inbox = DurableIngress(
        owner.path, "fixture", ["chat"], connection_provider=owner.connection
    )
    entered, release = threading.Event(), threading.Event()
    failures = []

    def work():
        try:
            with inbox.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                entered.set()
                assert release.wait(3)
                assert db.execute("SELECT 1").fetchone()[0] == 1
        except BaseException as error:  # noqa: BLE001 -- surface background assertions
            failures.append(error)

    thread = threading.Thread(target=work)
    thread.start()
    try:
        assert entered.wait(2)
        owner.close_idle_connections()
    finally:
        release.set()
        thread.join(4)
    assert not failures and not thread.is_alive()
    assert inbox.accept(raw_event())
    owner.close_idle_connections()


def fake_sdk(monkeypatch, start):
    builder = Mock()
    builder.register_p2_im_message_receive_v1.return_value = builder
    client = Mock()
    client.start.side_effect = lambda: start(
        builder.register_p2_im_message_receive_v1.call_args.args[0]
    )
    sdk = SimpleNamespace(
        EventDispatcherHandler=SimpleNamespace(builder=Mock(return_value=builder)),
        JSON=SimpleNamespace(marshal=json.dumps),
        LogLevel=SimpleNamespace(WARNING="warning"),
        ws=SimpleNamespace(Client=Mock(return_value=client)),
    )
    monkeypatch.setitem(sys.modules, "lark_oapi", sdk)


def sdk_options(tmp_path):
    return {
        "path": str(tmp_path / "sdk.sqlite3"),
        "app_id": "fixture",
        "allowed_chat_ids": ["chat"],
    }


def test_sdk_owns_one_local_provider_for_multiple_events_and_closes_it(
    tmp_path, monkeypatch
):
    stores, opens, closed = [], [], []
    actual = ExecutionStore

    def construct(path):
        store = actual(path)
        original_open, original_close = (
            store._open_connection,
            store.close_idle_connections,
        )

        def open_connection(*args):
            opens.append(os.getpid())
            return original_open(*args)

        def close_connection():
            closed.append(os.getpid())
            return original_close()

        store._open_connection = open_connection
        store.close_idle_connections = close_connection
        stores.append(store)
        return store

    monkeypatch.setattr("src.core.execution.ExecutionStore", construct)
    monkeypatch.setattr(
        "src.core.execution.get_runtime", Mock(side_effect=AssertionError("no runtime"))
    )
    fake_sdk(
        monkeypatch,
        lambda callback: (callback(raw_event("one")), callback(raw_event("two"))),
    )
    events = queue.Queue()
    _run_sdk("fixture", "secret", events, sdk_options(tmp_path))
    assert len(stores) == 1 and len(opens) == 1 and closed == [os.getpid()]
    assert stores[0]._connections == {}
    assert [events.get_nowait()[0] for _ in range(2)] == ["wake", "wake"]
    with stores[0].connection() as db:
        assert db.execute("SELECT COUNT(*) FROM execution_owners").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM execution_modules").fetchone()[0] == 0
    stores[0].close_idle_connections()


@pytest.mark.parametrize("stage", ["construct", "cleanup", "sdk_and_cleanup"])
def test_sdk_provider_failures_close_loop_and_preserve_original_safe_class(
    tmp_path, monkeypatch, stage
):
    actual = ExecutionStore
    loop = asyncio.new_event_loop()
    monkeypatch.setattr(
        "src.interactive.feishu_bot.asyncio.new_event_loop", lambda: loop
    )

    def construct(path):
        if stage == "construct":
            raise PermissionError("private constructor detail")
        store = actual(path)
        original = store.close_idle_connections

        def close():
            original()
            raise sqlite3.OperationalError("private cleanup detail")

        store.close_idle_connections = close
        return store

    def start(_callback):
        loop.create_task(asyncio.sleep(3600))
        if stage == "sdk_and_cleanup":
            raise ValueError("private SDK detail")

    fake_sdk(monkeypatch, start)
    monkeypatch.setattr("src.core.execution.ExecutionStore", construct)
    events = queue.Queue()
    with pytest.raises(RuntimeError) as error:
        _run_sdk("fixture", "secret", events, sdk_options(tmp_path))
    expected = {
        "construct": "PermissionError",
        "cleanup": "OperationalError",
        "sdk_and_cleanup": "ValueError",
    }[stage]
    assert events.get_nowait() == ("error", expected)
    assert events.empty()
    assert loop.is_closed()
    assert "private" not in str(error.value)
    assert ("receiver failed" in str(error.value)) == (stage != "cleanup")


def test_parent_ingress_borrows_host_provider_without_serializing_or_closing_it(
    tmp_path, monkeypatch
):
    path = tmp_path / "profile.sqlite3"
    runtime = ExecutionRuntime(ExecutionStore(path))
    monkeypatch.setattr("src.core.report_execution.runtime_path", lambda _: path)
    bot = FeishuBot(
        {
            "interactive": {
                "feishu": {
                    "enabled": True,
                    "app_id": "fixture",
                    "app_secret": "secret",
                    "allowed_chat_ids": ["chat"],
                }
            }
        },
        execution_runtime=runtime,
    )
    actual = DurableIngress
    providers = []

    def ingress(*args, **kwargs):
        providers.append(kwargs.get("connection_provider"))
        return actual(*args, **kwargs)

    monkeypatch.setattr("src.interactive.ingress.DurableIngress", ingress)
    monkeypatch.setattr(bot, "validate_config", Mock())
    context = Mock()
    context.Queue.return_value.get.return_value = ("error", "FixtureStop")
    context.Process.return_value.is_alive.return_value = False
    bot._context = context
    close = Mock(wraps=runtime.store.close_idle_connections)
    monkeypatch.setattr(runtime.store, "close_idle_connections", close)
    with pytest.raises(RuntimeError, match="FixtureStop"):
        bot.run()
    assert providers == [runtime.store.connection]
    close.assert_not_called()
    child_settings = context.Process.call_args.kwargs["args"][3]
    assert set(child_settings) == {"path", "app_id", "allowed_chat_ids"}
    with runtime.store.connection() as db:
        assert db.execute("SELECT 1").fetchone()[0] == 1
    runtime.store.close_idle_connections()


@pytest.mark.parametrize("terminal_unknown", [False, True])
def test_abrupt_stub_sdk_restart_keeps_committed_pending_and_unknown(
    tmp_path, monkeypatch, terminal_unknown
):
    (tmp_path / "lark_oapi.py").write_text(
        """import json, os, time
from types import SimpleNamespace
class EventDispatcherHandler:
    @staticmethod
    def builder(*args): return EventDispatcherHandler()
    def register_p2_im_message_receive_v1(self, callback):
        self.callback = callback
        return self
    def build(self): return self
class Client:
    def __init__(self, *args, event_handler, **kwargs): self.handler = event_handler
    def start(self):
        self.handler.callback({"header":{"event_type":"im.message.receive_v1","app_id":"fixture"},"event":{"message":{"chat_id":"chat","message_id":"restart-message","content":json.dumps({"text":"holdings","sdk_pid":os.getpid()})}}})
        while True: time.sleep(.05)
ws=SimpleNamespace(Client=Client)
LogLevel=SimpleNamespace(WARNING="warning")
JSON=SimpleNamespace(marshal=json.dumps)
""",
        encoding="utf8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    options = sdk_options(tmp_path)
    parent = ExecutionStore(options["path"])
    context = multiprocessing.get_context("spawn")
    inbox = DurableIngress(**options, connection_provider=parent.connection)
    original_pid = None
    try:
        for attempt in range(2):
            events = context.Queue()
            process = context.Process(
                target=_run_sdk, args=("fixture", "secret", events, options)
            )
            process.start()
            try:
                kind, identity = events.get(timeout=12)
                assert kind == "wake"
                if attempt == 0:
                    original_pid = process.pid
                    assert len(inbox.pending()) == 1
                    if terminal_unknown:
                        inbox.finish(
                            identity, "legacy_dispatch_started", state="unknown"
                        )
                else:
                    assert process.pid != original_pid
            finally:
                process.terminate()
                process.join(5)
                if process.is_alive():
                    process.kill()
                    process.join(3)
                assert not process.is_alive()
                process.close()
                events.close()
                events.cancel_join_thread()
        fresh = DurableIngress(**options, connection_provider=parent.connection)
        assert len(fresh.pending()) == (0 if terminal_unknown else 1)
        handler = Mock(return_value="accepted")
        fresh.dispatch(None, handler)
        assert handler.call_count == (0 if terminal_unknown else 1)
        with parent.connection() as db:
            row = db.execute("SELECT * FROM execution_ingress").fetchone()
            assert row["state"] == ("unknown" if terminal_unknown else "done")
            payload = json.loads(row["payload"])
            assert (
                json.loads(payload["event"]["message"]["content"])["sdk_pid"]
                == original_pid
            )
            assert (
                db.execute("SELECT COUNT(*) FROM execution_owners").fetchone()[0] == 0
            )
            assert (
                db.execute("SELECT COUNT(*) FROM execution_modules").fetchone()[0] == 0
            )
    finally:
        parent.close_idle_connections()
