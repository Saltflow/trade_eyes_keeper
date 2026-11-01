"""Actual shared source admission and provider-neutral execution boundaries."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.shared_contracts import AuthorityRef
from src.task_runtime.authority import bind_authority
from src.task_runtime.request_policy import (
    RequestPolicy,
    RequestPolicyBlocked,
    source_policy_id,
)


def test_provider_aliases_share_one_policy():
    assert source_policy_id("https://www.cninfo.com.cn/query") == "cninfo"
    assert source_policy_id("https://static.cninfo.com.cn/report.pdf") == "cninfo"
    assert source_policy_id("https://www.sec.gov/Archives/x") == "sec"
    assert source_policy_id("https://data.sec.gov/submissions/x") == "sec"
    with pytest.raises(ValueError):
        source_policy_id("https://secret:credential@www.sec.gov/x")


def test_independent_consumers_cannot_loosen_interval(tmp_path):
    authority = AuthorityRef(tmp_path / "authority")
    first, second = RequestPolicy(authority), RequestPolicy(authority)
    first.wait("https://www.cninfo.com.cn/query", min_interval=0.03)
    start = time.monotonic()
    second.wait("https://static.cninfo.com.cn/report.pdf", min_interval=0)
    assert time.monotonic() - start >= 0.025
    source = second.snapshot()["sources"][0]
    assert source["requests"] == 2
    assert source["interval"] == 0.03


def test_shared_atomic_slots_not_independent_sleepers(tmp_path):
    authority = AuthorityRef(tmp_path / "authority")
    ready = threading.Barrier(4)

    def acquire(index):
        policy = RequestPolicy(authority)
        ready.wait(timeout=5)
        policy.wait("https://data.sec.gov/a", min_interval=0.03)
        return index

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sorted(pool.map(acquire, range(4))) == [0, 1, 2, 3]
    assert time.monotonic() - started >= 0.08
    assert RequestPolicy(authority).snapshot()["sources"][0]["requests"] == 4


def test_new_project_cannot_clear_permanent_block(tmp_path):
    authority = AuthorityRef(tmp_path / "authority")
    policy = RequestPolicy(authority)
    policy.block("https://www.sec.gov/x", "http_403")
    another = RequestPolicy(authority)
    another.block("https://data.sec.gov/y", "temporary", retry_after=0)
    another.adopt_legacy("https://data.sec.gov/y", blocked=False)
    with pytest.raises(RequestPolicyBlocked, match="source_access_blocked"):
        another.wait("https://data.sec.gov/y", min_interval=0)
    assert another.snapshot()["sources"][0]["requests"] == 0


def test_cancellation_and_deadline_do_not_claim(tmp_path):
    policy = RequestPolicy(AuthorityRef(tmp_path / "authority"))
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(RequestPolicyBlocked, match="cancelled"):
        policy.wait("source", min_interval=0, cancel=cancelled)
    with pytest.raises(TimeoutError):
        policy.wait("source", deadline=time.monotonic() - 1)
    assert policy.snapshot()["sources"] == []


def test_temporary_retry_after_and_legacy_state_are_preserved(tmp_path):
    policy = RequestPolicy(AuthorityRef(tmp_path / "authority"))
    future = time.time() + 1
    policy.adopt_legacy("cninfo", next_at=future)
    policy.adopt_legacy("cninfo", next_at=0)
    policy.block("cninfo", "http_429", retry_after=1)
    with pytest.raises(TimeoutError):
        policy.wait("cninfo", deadline=time.monotonic() + 0.01)
    policy.wait("cninfo", min_interval=0, deadline=time.monotonic() + 2)
    assert policy.snapshot()["sources"][0]["requests"] == 1


def test_authority_identity_is_not_overwritten_or_inferred(tmp_path):
    authority = AuthorityRef(tmp_path / "authority")
    bound = bind_authority(authority)
    assert bind_authority(authority) == bound
    with pytest.raises(ValueError, match="identity_mismatch"):
        bind_authority(authority, expected_id="another-environment")
    path = authority.root / "authority.json"
    value = json.loads(path.read_text())
    value["authority_id"] = "another-environment"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="manifest_mismatch"):
        RequestPolicy(authority)
