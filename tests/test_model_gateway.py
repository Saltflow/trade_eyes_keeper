"""Cross-application model authority acceptance; all network boundaries mocked."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import requests

from src.model_gateway.json_client import request_json
from src.model_gateway.ledger import ExistingAttempt, GatewayBlocked, GatewayLedger
from src.shared_contracts import AuthorityRef


def claim(ledger, identity, *, producer="project-a", budget="1", reserve=".1"):
    auth = ledger.authorize(producer, budget)
    return ledger.claim(
        identity,
        authorization=auth,
        producer=producer,
        request_sha256=hashlib.sha256(identity.encode()).hexdigest(),
        account="account-hash",
        reservation=reserve,
        capture=Path("capture"),
        deadline=time.monotonic() + 5,
    )


def test_two_projects_share_capacity_and_live_handler_holds_until_exit(tmp_path):
    authority = AuthorityRef(tmp_path / "shared")
    first = GatewayLedger(authority, capacity=1)
    # A second caller cannot widen the already configured shared cap.
    second = GatewayLedger(authority, capacity=20)
    entered, exit_first, second_entered = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )

    def one():
        with claim(first, "one") as lease:
            lease.mark_sent()
            entered.set()
            assert exit_first.wait(5)
            lease.finish({"status": "complete", "cost": {"usd": ".02"}})

    def two():
        assert entered.wait(5)
        with claim(second, "two", producer="project-b") as lease:
            second_entered.set()
            lease.mark_sent()
            lease.finish({"status": "complete", "cost": {"usd": ".03"}})

    with ThreadPoolExecutor(2) as pool:
        future1, future2 = pool.submit(one), pool.submit(two)
        assert entered.wait(5)
        assert not second_entered.wait(0.1)
        assert first.totals()["active"] == 1
        exit_first.set()
        future1.result(5)
        future2.result(5)
    totals = second.totals()
    assert totals["known_usage_usd"] == "0.05"
    assert totals["sent"] == 2 and totals["active"] == 0


def test_unknown_is_not_zero_spend_and_never_reposted(tmp_path):
    ledger = GatewayLedger(AuthorityRef(tmp_path / "shared"))
    with claim(ledger, "unknown", budget=".15") as lease:
        lease.mark_sent()
    assert ledger.totals()["known_usage_usd"] == "0"
    assert ledger.totals()["unknown_reserved_usd_not_spend"] == "0.1"
    with pytest.raises(ExistingAttempt):
        claim(ledger, "unknown", budget=".15")
    with pytest.raises(GatewayBlocked, match="budget_exhausted"):
        claim(ledger, "next", budget=".15")


def test_not_sent_can_resume_and_budget_identity_cannot_change(tmp_path):
    ledger = GatewayLedger(AuthorityRef(tmp_path / "shared"))
    with claim(ledger, "unsent"):
        pass
    with claim(ledger, "unsent") as lease:
        lease.mark_sent()
        lease.finish({"status": "failed", "cost": {"usd": ".01"}})
    with pytest.raises(GatewayBlocked, match="authorization_changed"):
        ledger.authorize("project-a", "2")
    assert ledger.totals()["known_usage_usd"] == "0.01"


def test_scope_cannot_access_other_scope_capture(tmp_path):
    root = tmp_path / "authority"
    public = GatewayLedger(AuthorityRef(root, scope="public"))
    private = GatewayLedger(AuthorityRef(root, scope="private"))
    with claim(public, "same") as lease:
        lease.mark_sent()
    assert private.record("same") is None


def test_ledger_failure_fences_all_later_projects_before_post(tmp_path, monkeypatch):
    ledger = GatewayLedger(AuthorityRef(tmp_path / "shared"))
    auth = ledger.authorize("one", "1")
    original = sqlite3.connect

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic write failure")

    monkeypatch.setattr(sqlite3, "connect", fail)
    with pytest.raises(sqlite3.OperationalError):
        ledger.claim(
            "one",
            authorization=auth,
            producer="one",
            request_sha256="a",
            account="account",
            reservation=".1",
            capture=tmp_path / "capture",
        )
    assert ledger.fence_path.is_file()
    monkeypatch.setattr(sqlite3, "connect", original)
    other = GatewayLedger(ledger.authority)
    with pytest.raises(GatewayBlocked, match="ledger_fenced"):
        claim(other, "another", producer="other")
    assert other.totals()["sent"] == 0


class JSONResponse:
    status_code = 200

    def __init__(self):
        self.headers = {"Content-Type": "application/json"}
        self.content = json.dumps(
            {
                "choices": [{"message": {"content": "body"}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "prompt_cache_hit_tokens": 20,
                    "prompt_cache_miss_tokens": 80,
                    "completion_tokens_details": {"reasoning_tokens": 5},
                },
            }
        ).encode()


def test_json_gateway_preserves_response_and_reuses_without_new_charge(tmp_path):
    authority = AuthorityRef(tmp_path / "shared")
    posts = []

    def post(*args, **kwargs):
        posts.append(kwargs)
        return JSONResponse()

    kwargs = {
        "authority": authority,
        "intent": "financial-block-1",
        "producer": "run-a",
        "authorization": "run-a",
        "budget": "1",
        "reservation": ".1",
        "endpoint": "https://provider.example/chat/completions",
        "payload": {"messages": []},
        "key": "secret-not-recorded",
        "rates": {"input": ".15", "output": ".6", "cached_input": ".003"},
        "capture": tmp_path / "a",
        "timeout": (1, 2),
        "post": post,
    }
    response, first = request_json(**kwargs)
    again, reused = request_json(
        **{**kwargs, "producer": "run-b", "authorization": "run-b"}
    )
    assert response.content == again.content
    assert len(posts) == 1 and reused["charged_to_current_run"] is False
    assert reused["cost"]["usd"] == "0"
    assert reused["historical_usage"]["completion_tokens"] == 20
    ledger = GatewayLedger(authority)
    assert ledger.totals()["sent"] == 1
    assert ledger.totals()["known_usage_usd"] == first["cost"]["usd"]
    assert "secret-not-recorded" not in ledger.path.read_bytes().decode("latin-1")


def test_transport_exception_keeps_unknown_and_does_not_retry(tmp_path):
    authority = AuthorityRef(tmp_path / "shared")
    posts = []

    def fail(*args, **kwargs):
        posts.append(1)
        raise requests.ConnectionError("synthetic")

    kwargs = {
        "authority": authority,
        "intent": "unknown",
        "producer": "a",
        "authorization": "a",
        "budget": "1",
        "reservation": ".1",
        "endpoint": "https://provider.example/chat",
        "payload": {"messages": []},
        "key": "fake",
        "rates": {"input": 1, "output": 1, "cached_input": 1},
        "capture": tmp_path / "capture",
        "timeout": 1,
        "post": fail,
    }
    with pytest.raises(requests.ConnectionError):
        request_json(**kwargs)
    with pytest.raises(GatewayBlocked, match="unknown_or_artifact_unavailable"):
        request_json(**kwargs)
    assert posts == [1]
    assert GatewayLedger(authority).totals()["unknown_reserved_usd_not_spend"] == "0.1"


def test_research_stores_reuse_shared_success_without_model_key(tmp_path):
    from decimal import Decimal

    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel

    from src.research_pipeline.models import ResearchSpec
    from src.research_pipeline.runtime import ModelExecutor
    from src.research_pipeline.store import Store

    tokenizer = tmp_path / "tokenizer.json"
    Tokenizer(WordLevel({"[UNK]": 0}, unk_token="[UNK]")).save(str(tokenizer))
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "tokenizer": str(tokenizer),
                "balance_wait_seconds": 0,
                "execution_rates": {
                    "input": ".15",
                    "output": ".6",
                    "cached_input": ".003",
                },
                "peak_rates": {"input": ".3", "output": "1.2"},
            }
        )
    )
    authority = tmp_path / "shared_authority"
    posts = []

    class StreamResponse(JSONResponse):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_content(self, chunk_size):
            yield self.content

    def post(*args, **kwargs):
        posts.append(1)
        return StreamResponse()

    executors = []
    try:
        for name, paid in [("project-a", True), ("project-b", False)]:
            output = tmp_path / name
            output.mkdir()
            store = Store(output / "store")
            spec = ResearchSpec(
                ("a_share:600938",),
                output / "run",
                store_root=store.root,
                authority_root=authority,
                authority_isolated=True,
                budget_limit_usd=Decimal(1),
                paid=paid,
                config=config,
            )
            spec.output.mkdir()
            run = store.create_run(spec.to_dict())
            executor = ModelExecutor(
                store,
                spec,
                run["id"],
                post=post if paid else None,
                account_reader=None
                if paid
                else lambda: pytest.fail("cached account read"),
            )
            executors.append(executor)
            receipt = executor.call(
                "one-declared-financial-block",
                {
                    "model": "test",
                    "max_tokens": 20,
                    "messages": [{"role": "user", "content": "same material"}],
                },
                spec.output,
                "summary",
            )
            assert receipt["status"] == "complete"
            assert receipt["reused"] is not paid
            assert receipt["charged_to_current_run"] is paid
        assert posts == [1]
        assert (
            GatewayLedger(AuthorityRef(authority, isolated=True)).totals()["sent"] == 1
        )
    finally:
        for executor in executors:
            executor.close()
