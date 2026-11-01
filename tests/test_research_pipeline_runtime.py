"""Offline acceptance of the real shared runtime and durable model accounting."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import requests
from tokenizers import Tokenizer
from tokenizers.models import WordLevel
from tokenizers.pre_tokenizers import Whitespace

from src.research_pipeline.common import atomic_write, file_sha
from src.research_pipeline.models import ResearchSpec
from src.research_pipeline.runtime import ModelExecutor
from src.research_pipeline.store import Store


@pytest.fixture
def setup(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline test attempted provider access")

    monkeypatch.setattr(requests, "get", forbidden)
    monkeypatch.setattr(requests.Session, "post", forbidden)
    tokenizer = Tokenizer(WordLevel({"[UNK]": 0, "body": 1}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = Whitespace()
    tokenizer.save(str(tmp_path / "tokenizer.json"))
    config = {
        "tokenizer": str(tmp_path / "tokenizer.json"),
        "execution_rates": {"input": 0.15, "output": 0.6, "cached_input": 0.003},
        "peak_rates": {"input": ".3", "output": "1.2"},
        "pricing_utc_dates": [datetime.now(timezone.utc).date().isoformat()],
        "balance_wait_seconds": 0,
    }
    atomic_write(tmp_path / "config.json", config)
    store = Store(tmp_path / "shared")

    def make(name, budget="5", paid=True, **kwargs):
        output = tmp_path / name
        output.mkdir()
        spec = ResearchSpec(
            ("a_share:600348",),
            output,
            budget_limit_usd=Decimal(budget),
            paid=paid,
            config=tmp_path / "config.json",
            store_root=store.root,
        )
        run = store.create_run(spec.to_dict())
        executor = ModelExecutor(store, spec, run["id"], **kwargs)
        return executor, run

    return store, make


def request():
    return {
        "model": "deepseek-flash",
        "max_tokens": 100,
        "stream": True,
        "messages": [{"role": "user", "content": "body evidence"}],
    }


class Response:
    status_code = 200

    def __init__(self, name="response", usage=True):
        self.name, self.usage = name, usage
        self.headers = {"Content-Type": "text/event-stream"}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_content(self, chunk_size):
        data = {
            "id": self.name,
            "model": "deepseek-flash",
            "choices": [{"delta": {"content": "body"}, "finish_reason": None}],
        }
        yield ("data: " + json.dumps(data) + "\n\n").encode()
        end = {"choices": [{"delta": {}, "finish_reason": "stop"}]}
        if self.usage:
            end["usage"] = {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_cache_hit_tokens": 40,
                "prompt_cache_miss_tokens": 60,
                "completion_tokens_details": {"reasoning_tokens": 5},
            }
        yield ("data: " + json.dumps(end) + "\n\ndata: [DONE]\n\n").encode()


@pytest.mark.parametrize("fault", ["disk", "serialized_request", "implementation"])
def test_pre_post_integrity_fault_fences_unsent_run(setup, monkeypatch, fault):
    from types import SimpleNamespace

    from src.research_pipeline import runtime as runtime_module

    store, make = setup
    posts = []
    executor, run = make(
        "integrity_" + fault, post=lambda *a, **k: posts.append(k) or Response()
    )
    if fault == "disk":
        monkeypatch.setattr(
            runtime_module.shutil, "disk_usage", lambda _: SimpleNamespace(free=0)
        )
    elif fault == "serialized_request":
        original = runtime_module.transport.perform

        def tamper(*args, post=None, **kwargs):
            def altered(url, **request_args):
                value = json.loads(request_args["data"])
                value["messages"][0]["content"] = "unapproved"
                request_args["data"] = json.dumps(value).encode()
                return post(url, **request_args)

            return original(*args, post=altered, **kwargs)

        monkeypatch.setattr(runtime_module.transport, "perform", tamper)
    else:
        original = executor._snapshot

        def tamper_after_snapshot(args):
            result = original(args)
            executor._import_hashes[str(executor._implementation[0].resolve())] = (
                "changed"
            )
            return result

        monkeypatch.setattr(executor, "_snapshot", tamper_after_snapshot)
    receipt = executor.call(
        "first", request(), executor.spec.output / "capture", "summary"
    )
    assert receipt["status"] == "not_sent"
    assert not store.get_attempt("first")["sent_intent"]
    assert not posts
    assert store.get_run(run["id"])["requires_resume"] is True
    assert store.get_run(run["id"])["pause_reason"] == "integrity_or_resource_error"
    second = executor.call(
        "second", request(), executor.spec.output / "next", "summary"
    )
    assert second["reason"] == "run_paused"
    assert store.get_attempt("second") is None
    executor.close()


def test_real_transport_cache_reuse_and_one_producer_charge(setup):
    store, make = setup
    calls = []

    def post(*args, **kwargs):
        calls.append(kwargs)
        return Response()

    executor, run = make("producer", post=post)
    try:
        receipt = executor.call(
            "same-key", request(), executor.spec.output, "summary", company="600348"
        )
        assert receipt["status"] == "complete" and not receipt["reused"]
        assert receipt["usage"]["completion_tokens"] == 20
        assert store.budget(run["id"])["known_usage_usd"] == "0.00002112"
        consumer, cached_run = make("consumer", budget="0", paid=False)
        try:
            cached = consumer.call(
                "same-key", request(), consumer.spec.output, "summary"
            )
            assert cached["reused"] and cached["capture_dir"] == receipt["capture_dir"]
            assert store.budget(cached_run["id"])["known_usage_usd"] == "0"
        finally:
            consumer.close()
        assert len(calls) == 1
        assert store.get_attempt("same-key")["sent_intent"] == 1
    finally:
        executor.close()


def test_failed_unknown_usage_never_reposts_and_not_counted_as_spend(setup):
    store, make = setup
    calls = []

    def failed(*args, **kwargs):
        calls.append(1)
        raise requests.ConnectionError("synthetic")

    executor, run = make("failed", post=failed)
    try:
        result = executor.call("failed-key", request(), executor.spec.output, "extract")
        assert result["status"] == "failed" and result["cost"]["usd"] is None
        other, other_run = make("never-retry", post=failed)
        try:
            assert other.call("failed-key", request(), other.spec.output, "extract")[
                "reused"
            ]
        finally:
            other.close()
        budget = store.budget(run["id"])
        assert budget["known_usage_usd"] == "0"
        assert Decimal(budget["unknown_liability_reserved_usd_not_spend"]) > 0
        assert store.budget(other_run["id"])["known_usage_usd"] == "0"
        assert calls == [1]
    finally:
        executor.close()


def test_history_import_is_evidence_not_new_authorization_cost(setup):
    store, make = setup
    store.import_attempt(
        "historical",
        {
            "status": "partial",
            "capture_dir": "archive",
            "cost": {"usd": "2.5", "kind": "usage_at_published_rates"},
        },
    )
    executor, run = make("history", paid=False)
    try:
        result = executor.call("historical", request(), executor.spec.output, "summary")
        assert result["historical"] and result["capture_dir"] == "archive"
        assert store.budget(run["id"])["known_usage_usd"] == "0"
    finally:
        executor.close()


def test_budget_and_repair_ringfence_are_hard_pre_post_guards(setup):
    store, make = setup
    calls = []
    executor, run = make("budget", budget=".001", post=lambda *a, **k: calls.append(1))
    try:
        body = request()
        body["max_tokens"] = 1000
        assert (
            executor.call("research-too-large", body, executor.spec.output, "draft")[
                "reason"
            ]
            == "budget_stop"
        )
        assert (
            executor.call(
                "repair-too-large",
                body,
                executor.spec.output,
                "repair_model",
                repair=True,
            )["reason"]
            == "repair_budget_stop"
        )
        assert not calls
        assert store.budget(run["id"])["known_usage_usd"] == "0"
        assert store.budget(run["id"])["inflight_reserved_usd_not_spend"] == "0"
    finally:
        executor.close()


def test_balance_failure_blocks_only_new_paid_request(setup):
    store, make = setup
    calls = []

    def account():
        raise requests.ReadTimeout("synthetic")

    executor, run = make(
        "balance", post=lambda *a, **k: calls.append(1), account_reader=account
    )
    try:
        result = executor.call(
            "balance-key", request(), executor.spec.output, "summary"
        )
        assert result["status"] == "not_sent" and result["reason"] == "waiting_balance"
        assert store.get_attempt("balance-key")["sent_intent"] == 0
        assert not calls and store.budget(run["id"])["known_usage_usd"] == "0"
    finally:
        executor.close()


def test_optional_event_callback_failure_cannot_abort_success(setup):
    store, make = setup

    def callback(event):
        raise PermissionError("synthetic page unavailable")

    executor, run = make(
        "events", post=lambda *a, **k: Response(), event_callback=callback
    )
    try:
        assert (
            executor.call("event-key", request(), executor.spec.output, "summary")[
                "status"
            ]
            == "complete"
        )
        assert any(
            e["event"] == "event_callback_failed" for e in store.events(run["id"])
        )
    finally:
        executor.close()


def test_dead_owner_fences_run_unknown_sent_and_resume_only_unsent(setup):
    store, make = setup
    executor, run = make("recovery", post=lambda *a, **k: Response())
    assert store.claim_task("sent", run["id"], executor.spec.output / "sent")[0]
    assert store.reserve("sent", ".01") == ""
    store.mark_sent("sent")
    assert store.claim_task("queued", run["id"], executor.spec.output / "queued")[0]
    with store.connection() as db:
        db.execute(
            "UPDATE research_attempts SET owner_pid=2147483647 WHERE key IN ('sent','queued')"
        )
    assert store.reconcile() == [run["id"]]
    assert store.get_run(run["id"])["requires_resume"]
    assert store.get_attempt("sent")["status"] == "unknown"
    assert store.get_attempt("queued")["status"] == "not_sent"
    assert (
        executor.call("new", request(), executor.spec.output, "summary")["reason"]
        == "run_paused"
    )
    store.update_run(run["id"], state="running", requires_resume=False)
    try:
        assert executor.call("sent", request(), executor.spec.output, "summary")[
            "reused"
        ]
        assert (
            executor.call("queued", request(), executor.spec.output, "summary")[
                "status"
            ]
            == "complete"
        )
    finally:
        executor.close()


def test_concurrent_consumers_wait_for_single_producer(setup):
    store, make = setup
    started, release = threading.Event(), threading.Event()
    calls = []

    def post(*args, **kwargs):
        calls.append(1)
        started.set()
        assert release.wait(10)
        return Response()

    first, _producer = make("first", post=post)
    second, consumer = make("second", post=post)
    try:
        with ThreadPoolExecutor(2) as pool:
            a = pool.submit(
                first.call, "one-producer", request(), first.spec.output, "summary"
            )
            assert started.wait(10)
            b = pool.submit(
                second.call, "one-producer", request(), second.spec.output, "summary"
            )
            time.sleep(0.1)
            release.set()
            aa, bb = a.result(15), b.result(15)
        assert len(calls) == 1 and aa["capture_dir"] == bb["capture_dir"]
        assert bb["reused"] and store.budget(consumer["id"])["known_usage_usd"] == "0"
    finally:
        release.set()
        first.close()
        second.close()


def test_sqlite_lane_caps_two_executors_to_twenty_inflight(setup):
    _store, make = setup
    lock, release = threading.Lock(), threading.Event()
    active = peak = calls = 0

    def post(*args, **kwargs):
        nonlocal active, peak, calls
        with lock:
            active += 1
            calls += 1
            peak = max(peak, active)
        assert release.wait(60)
        with lock:
            active -= 1
        return Response(str(calls))

    first, _ = make("lane-first", post=post)
    second, _ = make("lane-second", post=post)
    try:
        with ThreadPoolExecutor(28) as pool:
            futures = [
                pool.submit(
                    (first if i % 2 else second).call,
                    f"parallel-{i}",
                    request(),
                    (first if i % 2 else second).spec.output,
                    "summary",
                )
                for i in range(28)
            ]
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and active < 20:
                time.sleep(0.05)
            assert active == 20 and peak == 20
            release.set()
            assert all(f.result(30)["status"] == "complete" for f in futures)
        assert calls == 28 and peak == 20
    finally:
        release.set()
        first.close()
        second.close()


def test_artifact_identity_verifies_bytes_and_preserves_old_revision(setup, tmp_path):
    store, _ = setup
    original, replacement = tmp_path / "original.md", tmp_path / "replacement.md"
    original.write_text("original", encoding="utf-8")
    replacement.write_text("replacement", encoding="utf-8")
    store.put_artifact("artifact", "summary", original)
    assert store.get_artifact("artifact")["path"] == str(original)
    original.write_text("changed externally", encoding="utf-8")
    assert store.get_artifact("artifact") is None
    store.put_artifact("artifact", "summary", replacement)
    assert store.get_artifact("artifact")["path"] == str(replacement)
    with store.connection() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM research_artifact_history").fetchone()[0]
            == 1
        )


def test_requests_share_frozen_code_but_recheck_tampering(setup):
    _, make = setup
    posts = []
    executor, _ = make("code-image", post=lambda *a, **k: posts.append(k) or Response())
    try:
        captures = []
        for key in ("image-one", "image-two"):
            receipt = executor.call(key, request(), executor.spec.output, "summary")
            assert receipt["status"] == "complete"
            capture = Path(receipt["capture_dir"])
            frozen = json.loads((capture.parents[1] / "freeze.json").read_text())
            captures.append({p: h for p, h in frozen["files"].items() if "execution_code" in p})
        assert captures[0] and captures[0] == captures[1]
        image = Path(next(iter(captures[0])))
        image.write_bytes(b"tampered audit image")
        blocked = executor.call("image-three", request(), executor.spec.output, "summary")
        assert blocked["status"] == "not_sent"
        assert len(posts) == 2
    finally:
        executor.close()


def test_dead_run_between_phases_is_paused_without_pausing_planned_runs(setup):
    store, make = setup
    executor, running = make("between-phases", post=lambda *a, **k: Response())
    other, planned = make("never-started", paid=False)
    store.update_run(
        running["id"], state="running", owner_pid=2147483647, owner_birth=1
    )
    assert store.reconcile() == [running["id"]]
    assert store.get_run(running["id"])["requires_resume"]
    assert store.get_run(planned["id"])["state"] == "created"
    assert not store.get_run(planned["id"])["requires_resume"]
    assert (
        executor.call("not-yet-sent", request(), executor.spec.output, "draft")[
            "reason"
        ]
        == "run_paused"
    )
    executor.close()
    other.close()


def test_live_pid_without_known_birth_is_not_declared_dead(setup):
    import os

    store, make = setup
    executor, run = make("live-unknown-birth", paid=False)
    store.update_run(
        run["id"], state="running", owner_pid=os.getpid(), owner_birth=None
    )
    assert store.reconcile() == []
    assert not store.get_run(run["id"])["requires_resume"]
    executor.close()


def test_aggregate_revision_preserves_previous_immutable_files(setup, tmp_path):
    store, _make = setup
    first = tmp_path / "first-context.md"
    second = tmp_path / "later-context.md"
    first.write_text("earlier retained body", encoding="utf-8")
    second.write_text("later body with previously unsent blocks", encoding="utf-8")
    store.put_artifact("annual-versions", "context", first, status="partial")
    store.put_artifact("annual-versions", "context", second)
    assert store.get_artifact("annual-versions")["path"] == str(second)
    assert first.read_text("utf-8") == "earlier retained body"
    with store.connection() as db:
        prior = json.loads(
            db.execute("SELECT data FROM research_artifact_history").fetchone()[0]
        )
    assert prior["path"] == str(first) and prior["status"] == "partial"
    second.write_text("illegal mutation", encoding="utf-8")
    with pytest.raises(ValueError, match="changed_in_place"):
        store.put_artifact("annual-versions", "context", second)


def test_verified_peak_rate_override_is_part_of_reservation(setup):
    store, make = setup
    executor, run = make("peak")
    executor.rates = {
        "input": Decimal(".15"),
        "cached_input": Decimal(".003"),
        "output": Decimal(".6"),
    }
    executor._peak_rates = {"input": "2", "output": "4"}
    _tokenizer, _count, reservation = executor._estimate(request())
    assert reservation == Decimal("2.0004")
    assert store.budget(run["id"])["known_usage_usd"] == "0"
    executor.close()


def test_repair_can_diagnose_code_pause_but_cannot_restart_research(setup):
    store, make = setup
    executor, run = make("repair-pause", post=lambda *a, **k: Response())
    store.update_run(
        run["id"], state="paused", requires_resume=True, pause_reason="code_fault"
    )
    try:
        assert (
            executor.call("research-paused", request(), executor.spec.output, "draft")[
                "reason"
            ]
            == "run_paused"
        )
        assert (
            executor.call(
                "diagnosis",
                request(),
                executor.spec.output,
                "repair_model",
                repair=True,
            )["status"]
            == "complete"
        )
        assert store.get_run(run["id"])["requires_resume"]
        store.update_run(run["id"], state="user_stopped")
        assert (
            executor.call(
                "not-after-stop",
                request(),
                executor.spec.output,
                "repair_model",
                repair=True,
            )["reason"]
            == "run_paused"
        )
    finally:
        executor.close()


def test_expired_repair_deadline_does_not_create_attempt(setup):
    store, make = setup
    executor, _run = make("repair-deadline", post=lambda *a, **k: Response())
    try:
        result = executor.call(
            "expired",
            request(),
            executor.spec.output,
            "repair_model",
            repair=True,
            deadline=time.monotonic() - 1,
        )
        assert result["reason"] == "deadline_before_send"
        assert store.get_attempt("expired") is None
    finally:
        executor.close()


def test_deadline_is_forwarded_to_real_transport_timeout(setup):
    _store, make = setup
    observed = []

    def post(*args, **kwargs):
        observed.append(kwargs["timeout"])
        return Response()

    executor, _run = make("timeout-clamp", post=post)
    try:
        assert (
            executor.call(
                "bounded",
                request(),
                executor.spec.output,
                "repair_model",
                repair=True,
                deadline=time.monotonic() + 10,
            )["status"]
            == "complete"
        )
        assert 0 < observed[0][0] <= 10 and 0 < observed[0][1] <= 10
    finally:
        executor.close()


def test_claim_run_rejects_duplicate_controller_even_same_process(setup):
    import os

    import psutil

    store, make = setup
    executor, run = make("one-owner", paid=False)
    birth = psutil.Process(os.getpid()).create_time()
    assert store.claim_run(run["id"], os.getpid(), birth)
    assert not store.claim_run(run["id"], os.getpid(), birth)
    executor.close()


def test_unused_repair_hold_can_be_released_without_new_budget(setup):
    store, make = setup
    executor, run = make("release-hold", paid=False)
    assert store.budget(run["id"])["research_remaining_usd"] == "4.5"
    store.update_run(run["id"], repair_hold_released=True)
    budget = store.budget(run["id"])
    assert budget["research_remaining_usd"] == "5"
    assert budget["repair_limit_usd"] == ".5" or budget["repair_limit_usd"] == "0.5"
    assert budget["authorized_usd"] == "5" and budget["known_usage_usd"] == "0"
    executor.close()


def test_tail_events_shows_recent_errors_not_oldest_page(setup):
    store, make = setup
    executor, run = make("tail", paid=False)
    for i in range(6):
        store.event(run["id"], "progress", number=i)
    store.event(run["id"], "last_error", reason="synthetic")
    tail = store.tail_events(run["id"], 2)
    assert [e["event"] for e in tail] == ["progress", "last_error"]
    assert tail[0]["seq"] < tail[1]["seq"]
    executor.close()


def test_unsent_balance_failure_can_be_explicitly_called_without_reposting_old_attempt(
    setup,
):
    store, make = setup
    checks, calls = [], []

    def account():
        checks.append(1)
        if len(checks) == 1:
            raise requests.ReadTimeout("synthetic")
        return Decimal(20)

    def post(*args, **kwargs):
        calls.append(1)
        return Response()

    executor, _run = make("unsent-again", post=post, account_reader=account)
    try:
        assert (
            executor.call("unsent-key", request(), executor.spec.output, "summary")[
                "status"
            ]
            == "not_sent"
        )
        assert (
            executor.call("unsent-key", request(), executor.spec.output, "summary")[
                "status"
            ]
            == "complete"
        )
        assert calls == [1] and store.get_attempt("unsent-key")["sent_intent"] == 1
    finally:
        executor.close()


def test_provider_403_is_persisted_across_runs_without_storing_credential(setup):
    store, make = setup
    calls = []
    secret = "credential-that-must-not-be-persisted"

    def post(*args, **kwargs):
        calls.append(1)
        response = Response()
        response.status_code = 403
        return response

    first, _run = make("forbidden-first", post=post, key=secret)
    second, other = make("forbidden-other", post=post, key=secret)
    try:
        assert (
            first.call("forbidden", request(), first.spec.output, "extract")[
                "http_status"
            ]
            == 403
        )
        blocked = second.call("never-sent", request(), second.spec.output, "summary")
        assert blocked["reason"] == "provider_blocked"
        assert store.get_attempt("never-sent") is None and calls == [1]
        assert store.budget(other["id"])["known_usage_usd"] == "0"
        assert secret not in str(store.provider_block(first._provider_id()))
        assert secret.encode() not in store.db_path.read_bytes()
    finally:
        first.close()
        second.close()


def test_provider_429_retry_after_only_releases_unsent_nodes(setup, monkeypatch):
    store, make = setup
    calls = []

    def post(*args, **kwargs):
        calls.append(1)
        response = Response()
        if len(calls) == 1:
            response.status_code = 429
            response.headers["Retry-After"] = "5"
        return response

    first, _run = make("rate-limited", post=post)
    second, _other = make("rate-other", post=post)
    try:
        assert (
            first.call("rate-failed", request(), first.spec.output, "summary")[
                "http_status"
            ]
            == 429
        )
        block = store.provider_block(first._provider_id())
        assert 0 < block["blocked_until"] - time.time() <= 5
        assert (
            second.call("next-unsent", request(), second.spec.output, "summary")[
                "reason"
            ]
            == "provider_blocked"
        )
        # Source access now has one cross-project authority as well as this
        # legacy projection. Advance actual policy time; never clear one table
        # to bypass the shared account's Retry-After.
        real_time = time.time
        monkeypatch.setattr(time, "time", lambda: real_time() + 6)
        assert (
            second.call("next-unsent", request(), second.spec.output, "summary")[
                "status"
            ]
            == "complete"
        )
        assert second.call("rate-failed", request(), second.spec.output, "summary")[
            "reused"
        ]
        assert calls == [1, 1]
    finally:
        first.close()
        second.close()


def test_provider_key_scope_does_not_block_explicit_different_key(setup):
    store, make = setup
    first, _ = make("bad-key", post=lambda *a, **k: Response(), key="one-explicit-key")
    second, _ = make(
        "good-key", post=lambda *a, **k: Response(), key="second-explicit-key"
    )
    store.block_provider(first._provider_id(), 401)
    try:
        assert (
            second.call("different-key", request(), second.spec.output, "summary")[
                "status"
            ]
            == "complete"
        )
        assert store.provider_block(first._provider_id())["blocked_until"] is None
    finally:
        first.close()
        second.close()


def test_balance_auth_failure_is_a_shared_pre_post_block(setup, monkeypatch):
    store, make = setup
    gets = []

    class BalanceDenied:
        status_code = 403

        def __init__(self):
            self.headers = {}

        def raise_for_status(self):
            raise requests.HTTPError("synthetic account denied", response=self)

    def get(*args, **kwargs):
        gets.append(1)
        return BalanceDenied()

    monkeypatch.setattr(requests, "get", get)
    first, _ = make("account-denied", key="same-account-key")
    second, _ = make("account-denied-other", key="same-account-key")
    try:
        assert (
            first.call("account-first", request(), first.spec.output, "summary")[
                "reason"
            ]
            == "provider_blocked"
        )
        assert (
            second.call("account-other", request(), second.spec.output, "summary")[
                "reason"
            ]
            == "provider_blocked"
        )
        assert gets == [1]
        assert store.get_attempt("account-first")["sent_intent"] == 0
        assert store.get_attempt("account-other") is None
    finally:
        first.close()
        second.close()


def test_attempt_listing_reports_active_and_terminal_separately(setup):
    store, make = setup
    executor, run = make("attempt-listing", paid=False)
    store.claim_task(
        "list-key",
        run["id"],
        executor.spec.output / "capture",
        phase="summary",
        company="600348",
    )
    assert store.active_attempts(run["id"])[0]["company"] == "600348"
    store.settle("list-key", {"status": "not_sent", "cost": {"usd": None}})
    assert store.active_attempts(run["id"]) == []
    assert store.list_attempts(run["id"])[0]["status"] == "not_sent"
    executor.close()


def test_live_tariff_phase_mismatch_blocks_before_attempt(setup, monkeypatch):
    from src.research_pipeline import pricing

    store, make = setup
    executor, _run = make(
        "phase-boundary", key="phase-offline-key", account_reader=lambda: Decimal(20)
    )
    executor._tariff_phase = "off_peak"
    monkeypatch.setattr(pricing, "tariff_phase", lambda: "peak")
    try:
        receipt = executor.call("phase-key", request(), executor.spec.output, "summary")
        assert receipt["reason"] == "pricing_refresh_needed"
        assert store.get_attempt("phase-key") is None
    finally:
        executor.close()


def test_live_utc_date_crossing_at_post_boundary_keeps_sent_intent_false(
    setup, monkeypatch
):
    from datetime import timedelta
    from types import SimpleNamespace

    from src.research_pipeline import runtime as runtime_module

    store, make = setup
    clock = [datetime.now(timezone.utc)]
    monkeypatch.setattr(
        runtime_module, "datetime", SimpleNamespace(now=lambda tz: clock[0])
    )
    executor, _run = make(
        "utc-boundary", key="date-offline-key", account_reader=lambda: Decimal(20)
    )
    original = executor._observed_post

    def switching(*args, **kwargs):
        send = original(*args, **kwargs)

        def boundary(*a, **k):
            clock[0] += timedelta(days=1)
            return send(*a, **k)

        return boundary

    monkeypatch.setattr(executor, "_observed_post", switching)
    try:
        receipt = executor.call("utc-key", request(), executor.spec.output, "summary")
        assert (
            receipt["status"] == "not_sent"
            and receipt["reason"] == "pricing_refresh_needed"
        )
        assert store.get_attempt("utc-key")["sent_intent"] == 0
    finally:
        executor.close()


def test_free_balance_backoff_recovers_without_retrying_model_post(setup):
    store, make = setup
    queries, posts = [], []

    def account():
        queries.append(1)
        if len(queries) < 3:
            raise requests.ReadTimeout("synthetic temporary balance failure")
        return Decimal(20)

    def post(*args, **kwargs):
        posts.append(1)
        return Response()

    executor, run = make("balance-auto-recover", post=post, account_reader=account)
    executor.config.update(balance_wait_seconds=1, balance_backoff_seconds=[0])
    try:
        receipt = executor.call(
            "balance-recover", request(), executor.spec.output, "summary"
        )
        assert receipt["status"] == "complete" and posts == [1] and queries == [1, 1, 1]
        assert any(e["event"] == "balance_ready" for e in store.events(run["id"]))
        assert store.get_attempt("balance-recover")["sent_intent"] == 1
    finally:
        executor.close()


def test_live_balance_success_cache_avoids_per_task_gets(setup, monkeypatch):
    _store, make = setup
    queries, posts = [], []

    class Balance:
        status_code = 200

        def __init__(self):
            self.headers = {}

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "is_available": True,
                "balance_infos": [{"currency": "USD", "total_balance": "20"}],
            }

    def get(*args, **kwargs):
        queries.append(1)
        return Balance()

    def post(*args, **kwargs):
        posts.append(1)
        return Response()

    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(requests.Session, "post", post)
    executor, _run = make("live-balance-cache", key="cache-offline-key")
    executor.config["balance_cache_seconds"] = 60
    try:
        assert (
            executor.call("cache-first", request(), executor.spec.output, "summary")[
                "status"
            ]
            == "complete"
        )
        assert (
            executor.call("cache-next", request(), executor.spec.output, "summary")[
                "status"
            ]
            == "complete"
        )
        assert queries == [1] and posts == [1, 1]
    finally:
        executor.close()


def test_verified_account_tariff_ratio_overrides_legacy_config(setup):
    _store, make = setup
    executor, _run = make(
        "official-tariff-ratio",
        post=lambda *a, **k: Response(),
        rates={
            "input": ".15",
            "output": ".6",
            "cached_input": ".003",
            "_account_tariff_ratio": ".12",
        },
        account_reader=lambda: {
            "is_available": True,
            "balance_infos": [{"currency": "CNY", "total_balance": "100"}],
        },
    )
    executor.config["account_tariff_ratio"] = ".99"
    assert executor._read_funds("offline-key") == Decimal(12)
    assert "_account_tariff_ratio" not in executor.rates
    executor.close()


def test_live_cny_account_without_verified_tariff_ratio_is_not_guessed(
    setup, monkeypatch
):
    _store, make = setup

    class Balance:
        status_code, headers = 200, {}

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "is_available": True,
                "balance_infos": [{"currency": "CNY", "total_balance": "100"}],
            }

    monkeypatch.setattr(requests, "get", lambda *a, **k: Balance())
    executor, _run = make("unverified-cny", key="offline-key")
    executor.config["account_tariff_ratio"] = ".15"
    with pytest.raises(ValueError, match="account_tariff_ratio_unverified"):
        executor._read_funds("offline-key")
    executor.close()


def test_converted_balance_cache_is_scoped_to_verified_tariff_ratio(setup, monkeypatch):
    _store, make = setup
    queries = []

    class Balance:
        status_code, headers = 200, {}

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "is_available": True,
                "balance_infos": [{"currency": "CNY", "total_balance": "100"}],
            }

    def get(*args, **kwargs):
        queries.append(1)
        return Balance()

    monkeypatch.setattr(requests, "get", get)
    rates = {"input": ".15", "output": ".6", "cached_input": ".003"}
    first, _ = make(
        "ratio-cache-first",
        key="same-offline-key",
        rates={**rates, "_account_tariff_ratio": ".12"},
    )
    second, _ = make(
        "ratio-cache-next",
        key="same-offline-key",
        rates={**rates, "_account_tariff_ratio": ".13"},
    )
    assert first._funds("same-offline-key") == Decimal(12)
    assert second._funds("same-offline-key") == Decimal(13)
    assert len(queries) == 2
    first.close()
    second.close()


def test_input_manifest_tamper_fences_before_new_provider_post(setup):
    store, make = setup
    posts = []
    executor, run = make(
        "input-manifest", post=lambda *a, **k: posts.append(1) or Response()
    )
    manifest = executor.spec.output / "inputs.freeze.json"
    atomic_write(manifest, {"files": {}})
    store.update_run(
        run["id"],
        integrity_manifests=[{"path": str(manifest), "sha256": file_sha(manifest)}],
    )
    atomic_write(manifest, {"files": {}, "tampered": True})
    receipt = executor.call("input-tamper", request(), executor.spec.output, "summary")
    assert receipt["status"] == "not_sent"
    assert not posts
    assert not store.get_attempt("input-tamper")["sent_intent"]
    assert store.get_run(run["id"])["requires_resume"] is True
    assert "input_integrity_changed" in store.get_run(run["id"])["error"]
    executor.close()


def test_queue_submit_failure_fences_and_settles_unsubmitted_claim(setup, monkeypatch):
    store, make = setup
    posts = []
    executor, run = make(
        "queue-fault", post=lambda *a, **k: posts.append(1) or Response()
    )
    runtime = executor._start_runtime()

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic queue admission fault")

    monkeypatch.setattr(runtime, "submit", fail)
    receipt = executor.call(
        "never-submitted", request(), executor.spec.output, "summary"
    )
    assert receipt["status"] == "not_sent"
    assert receipt["reason"] == "queue_submission_failed"
    assert not store.get_attempt("never-submitted")["sent_intent"]
    assert not store.active_attempts(run["id"])
    assert not executor._arguments
    assert store.get_run(run["id"])["pause_reason"] == "ledger_error"
    assert not posts
    second = executor.call("next-unsent", request(), executor.spec.output, "summary")
    assert second["reason"] == "run_paused"
    assert store.get_attempt("next-unsent") is None
    executor.close()


def test_settlement_sql_fault_pauses_before_any_further_post(setup, monkeypatch):
    store, make = setup
    posts, settlements = [], []
    executor, run = make(
        "settlement-fault", post=lambda *a, **k: posts.append(1) or Response()
    )
    original = store.settle

    def fail_once(*args, **kwargs):
        settlements.append(1)
        if len(settlements) == 1:
            raise sqlite3.OperationalError("synthetic settlement failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "settle", fail_once)
    result = executor.call("sent-once", request(), executor.spec.output, "summary")
    assert result["status"] == "unknown"
    assert store.get_run(run["id"])["pause_reason"] == "ledger_error"
    assert len(posts) == 1
    assert store.get_attempt("sent-once")["status"] == "unknown"
    assert result["provider_receipt_status"] == "complete"
    assert result["usage"]["prompt_tokens"] == 100
    assert Decimal(store.budget(run["id"])["known_usage_usd"]) > 0
    assert store.budget(run["id"])["unknown_liability_reserved_usd_not_spend"] == "0"
    assert not store.active_attempts(run["id"])
    assert (
        executor.call("another", request(), executor.spec.output, "summary")["reason"]
        == "run_paused"
    )
    assert executor.call("sent-once", request(), executor.spec.output, "summary")[
        "reused"
    ]
    assert len(posts) == 1
    executor.close()


def test_submit_fault_after_queue_commit_cancels_unsent_admission(setup, monkeypatch):
    store, make = setup
    posts = []
    executor, run = make(
        "queue-after-commit", post=lambda *a, **k: posts.append(1) or Response()
    )
    runtime = executor._start_runtime()

    def commit_then_fail(*args, **kwargs):
        runtime.store.submit(*args, **kwargs)
        raise RuntimeError("synthetic failure after admission commit")

    monkeypatch.setattr(runtime, "submit", commit_then_fail)
    # The dispatcher cannot claim the deliberately committed row before the
    # submit wrapper raises. This exercises the actual ambiguous commit edge.
    with runtime._tick_lock:
        result = executor.call(
            "committed-unsent", request(), executor.spec.output, "summary"
        )
    assert result["status"] == "not_sent"
    assert not posts
    assert not store.active_attempts(run["id"])
    with store.execution.connection() as db:
        rows = db.execute(
            "SELECT status FROM execution_runs WHERE job_id=?", (executor._job,)
        ).fetchall()
    assert rows and all(row["status"] not in {"queued", "running"} for row in rows)
    assert store.get_run(run["id"])["requires_resume"] is True
    executor.close()


def test_research_execution_connection_policy_shared_across_store_instances(setup):
    store, _make = setup
    other = Store(store.root)
    from src.research_pipeline.store import _database_gate

    assert _database_gate(store.db_path) is _database_gate(other.db_path)
    with store.execution.connection() as db:
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    with store.connection() as db:
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
