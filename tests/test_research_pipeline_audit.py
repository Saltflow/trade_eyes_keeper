"""Delivery audits use offline durable receipts, never provider queries."""

from decimal import Decimal

from src.research_pipeline.audit import audit_run, hash_manifest
from src.research_pipeline.common import atomic_write, file_sha
from src.research_pipeline.store import Store


def make(tmp_path):
    store = Store(tmp_path / "store")
    run = store.create_run({"budget_limit_usd": "5", "repair_fraction": ".1"})
    return store, run["id"]


def add(
    store,
    run_id,
    key,
    *,
    company="a_share:600348",
    phase="summary",
    usage=True,
    sent=True,
    cost=".02",
):
    store.claim_task(key, run_id, "capture/" + key, phase=phase, company=company)
    if sent:
        assert store.reserve(key, ".1") == ""
        store.mark_sent(key)
    receipt = {
        "status": "complete" if usage else "failed",
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_cache_hit_tokens": 40,
            "completion_tokens_details": {"reasoning_tokens": 5},
        }
        if usage
        else None,
        "cost": {"usd": cost if usage else None, "kind": "usage"},
        "simulated": True,
    }
    store.settle(key, receipt)


def test_new_usage_reasoning_unknown_and_not_sent_stay_distinct(tmp_path):
    store, run_id = make(tmp_path)
    add(store, run_id, "summary")
    add(store, run_id, "peer", company="", phase="peer", usage=False)
    add(store, run_id, "unsent", sent=False, usage=False)
    store.event(
        run_id, "dispatch_blocked", task_key="never_admitted", reason="budget_stop"
    )
    report = audit_run(store, run_id, [])
    totals = report["totals"]
    assert totals["sent_count"] == 2
    assert totals["not_sent_count"] == 1
    assert Decimal(totals["known_usage_usd"]) == Decimal(".02")
    assert totals["unknown_cost_attempts"] == 1
    assert totals["tokens"]["cache_miss_tokens"] == {
        "known_subtotal": 60,
        "unknown_attempts": 1,
    }
    assert totals["total_tokens_known_subtotal"] == 120
    assert totals["tokens"]["reasoning_tokens"]["known_subtotal"] == 5
    assert report["concurrency"]["maximum_actual_concurrency"] is None
    assert report["unknown_liability_or_inflight_reservation_is_spend"] is False
    assert (
        report["blocked_admissions_without_provider_attempt"][0]["task_key"]
        == "never_admitted"
    )
    assert totals["active_attempt_count"] == 0
    assert {g["company"] for g in report["by_company_phase"]} == {
        "a_share:600348",
        "shared",
    }


def test_historical_and_other_producer_evidence_requires_reference(tmp_path):
    store, old_run = make(tmp_path)
    add(store, old_run, "old")
    run_id = store.create_run({"budget_limit_usd": "0"})["id"]
    store.import_attempt(
        "historic",
        {
            "capture_dir": "old/capture",
            "receipt": {
                "status": "complete",
                "usage": {"prompt_tokens": 7},
                "cost": {"usd": "8", "kind": "usage"},
            },
        },
    )
    store.import_attempt(
        "unrelated",
        {
            "capture_dir": "unrelated",
            "receipt": {
                "status": "complete",
                "cost": {"usd": "99", "kind": "usage"},
            },
        },
    )
    store.import_attempt(
        "historic_alias",
        {
            "capture_dir": "old/capture",
            "receipt": {
                "status": "complete",
                "usage": {"prompt_tokens": 7},
                "cost": {"usd": "8", "kind": "usage"},
            },
        },
    )
    store.event(run_id, "attempt_reused", task_key="old")
    report = audit_run(
        store,
        run_id,
        [
            {
                "path": str(tmp_path / "missing.md"),
                "metadata": {"receipt": {"capture_dir": "old/capture"}},
            }
        ],
    )
    assert report["totals"]["known_usage_usd"] == "0"
    assert report["budget_guard"]["known_usage_usd"] == "0"
    assert report["inherited_known_usage_usd_not_current_spend"] == "8.02"
    assert len(report["inherited_or_reused_attempts_not_charged_to_this_run"]) == 2
    assert report["concurrency"]["maximum_actual_concurrency"] == 0


def test_precise_event_intervals_and_more_than_one_event_page(tmp_path):
    store, run_id = make(tmp_path)
    for key in ("one", "two"):
        add(store, run_id, key)
    for _ in range(510):
        store.event(run_id, "progress")
    store.event(
        run_id, "provider_post_started", task_key="one", at="2026-01-01T00:00:00+00:00"
    )
    store.event(
        run_id, "provider_post_started", task_key="two", at="2026-01-01T00:00:01+00:00"
    )
    store.event(
        run_id, "model_finished", task_key="one", at="2026-01-01T00:00:02+00:00"
    )
    store.event(
        run_id, "model_finished", task_key="two", at="2026-01-01T00:00:03+00:00"
    )
    report = audit_run(store, run_id, [])
    assert report["concurrency"]["maximum_actual_concurrency"] == 2
    # A duplicate start is incomplete evidence, never an invented extra call.
    store.event(run_id, "provider_post_started", task_key="one")
    assert (
        audit_run(store, run_id, [])["concurrency"]["maximum_actual_concurrency"]
        is None
    )


def test_local_tokens_dedupe_copies_and_unknown_not_character_counts(tmp_path):
    store, run_id = make(tmp_path)
    first, copy, unknown = (
        tmp_path / "first.md",
        tmp_path / "copy.md",
        tmp_path / "unknown.md",
    )
    atomic_write(first, "the same body")
    atomic_write(copy, "the same body")
    atomic_write(unknown, "unknown body")
    report = audit_run(
        store,
        run_id,
        [
            {"path": str(first), "stage": "context", "metadata": {"tokens_local": 4}},
            {"path": str(copy), "stage": "context", "metadata": {"tokens_local": 4}},
            {"path": str(unknown), "stage": "research", "metadata": {}},
        ],
    )
    bodies = report["local_bodies_not_api_usage"]
    assert len(bodies) == 2
    assert bodies[0]["tokens_local"] == 4
    assert bodies[1]["tokens_local"] is None
    assert report["totals"]["total_tokens_known_subtotal"] == 0


def test_upper_bound_cost_is_unknown_not_usage_spend(tmp_path):
    store, run_id = make(tmp_path)
    store.claim_task("unknown-cache", run_id, "capture", phase="summary")
    store.reserve("unknown-cache", "1")
    store.mark_sent("unknown-cache")
    store.settle(
        "unknown-cache",
        {
            "status": "complete",
            "usage": {"prompt_tokens": 10},
            "cost": {"usd": "1", "kind": "usage_cache_unknown_upper_bound"},
        },
    )
    report = audit_run(store, run_id, [])
    assert report["totals"]["known_usage_usd"] == "0"
    assert report["totals"]["unknown_cost_attempts"] == 1
    assert report["totals"]["tokens"]["completion_tokens"]["unknown_attempts"] == 1


def test_manifest_hashes_actual_bytes_and_excludes_private_or_outside(tmp_path):
    delivery = tmp_path / "delivery"
    public = delivery / "report.md"
    secret = delivery / ".env"
    outside = tmp_path / "outside.md"
    atomic_write(public, "report")
    atomic_write(secret, "credential")
    atomic_write(outside, "unrelated")
    manifest = hash_manifest([delivery, public, outside], root=delivery)
    assert manifest == [{"path": "report.md", "bytes": 6, "sha256": file_sha(public)}]
    atomic_write(public, "new report")
    assert hash_manifest(delivery, root=delivery)[0]["sha256"] != manifest[0]["sha256"]


def test_legacy_annual_scope_recovers_company_phase_and_excludes_other_sources(
    tmp_path,
):
    store, run_id = make(tmp_path)
    document = {
        "issuer_id": "a_share:600348",
        "instrument": "a_share:600348",
        "fiscal_year": 2025,
        "sha256": "a" * 64,
    }
    other = {**document, "issuer_id": "a_share:600546", "instrument": "a_share:600546"}
    wrong_source = {**document, "sha256": "b" * 64}
    receipt = {
        "status": "complete",
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        "cost": {"usd": ".02", "kind": "usage"},
    }
    store.import_attempt(
        "legacy-extract",
        {
            "path": "legacy/extract",
            "document": document,
            "block": "financial",
            "receipt": receipt,
        },
    )
    store.import_attempt(
        "extract-alias",
        {
            "path": "legacy/extract",
            "document": document,
            "block": "financial",
            "receipt": receipt,
        },
    )
    store.import_attempt(
        "legacy-summary",
        {
            "path": "legacy/summary",
            "documents": [document],
            "versions": {"2025": ["a" * 64]},
            "stage": "summary",
            "receipt": receipt,
        },
    )
    store.import_attempt(
        "legacy-unknown",
        {
            "path": "legacy/unknown",
            "document": document,
            "block": "capital",
            "receipt": {"status": "unknown", "usage": None, "cost": {"usd": None}},
        },
    )
    store.import_attempt(
        "unrelated-issuer",
        {
            "path": "600348/misleading-folder",
            "document": other,
            "stage": "summary",
            "receipt": receipt,
        },
    )
    store.import_attempt(
        "changed-annual",
        {
            "path": "legacy/changed",
            "document": wrong_source,
            "block": "financial",
            "receipt": receipt,
        },
    )
    body = tmp_path / "context.md"
    atomic_write(body, "delivered context")
    artifacts = [
        {
            "path": str(body),
            "kind": "context",
            "metadata": {"documents": [document], "versions": {"2025": ["a" * 64]}},
        }
    ]
    result = audit_run(store, run_id, artifacts)
    historical = result["inherited_or_reused_attempts_not_charged_to_this_run"]
    assert {item["task_key"] for item in historical} == {
        "legacy-extract",
        "legacy-summary",
        "legacy-unknown",
    }
    assert {item["company"] for item in historical} == {"a_share:600348"}
    assert {item["phase"] for item in historical} == {
        "extract:financial",
        "summary",
        "extract:capital",
    }
    assert len(result["inherited_by_company_phase_not_current_spend"]) == 3
    assert result["inherited_totals_not_current_spend"]["unknown_cost_attempts"] == 1
    assert result["inherited_known_usage_usd_not_current_spend"] == "0.04"
    assert result["totals"]["known_usage_usd"] == "0"
    assert result["budget_guard"]["known_usage_usd"] == "0"


def test_explicit_artifact_and_part_keys_expand_only_proven_history(tmp_path):
    store, run_id = make(tmp_path)
    document = {
        "issuer_id": "us:XOM",
        "instrument": "us:XOM",
        "fiscal_year": 2025,
        "sha256": "c" * 64,
    }
    body = tmp_path / "summary.md"
    atomic_write(body, "summary")
    store.put_artifact(
        "used-summary",
        "summary",
        body,
        metadata={"documents": [document], "versions": {"2025": ["c" * 64]}},
    )
    receipt = {
        "status": "complete",
        "usage": {"prompt_tokens": 1},
        "cost": {"usd": ".01", "kind": "usage"},
    }
    store.import_attempt(
        "legacy-extract",
        {
            "path": "xom/extract",
            "market": "us",
            "code": "XOM",
            "versions": {"2025": ["c" * 64]},
            "block": "financial",
            "receipt": receipt,
        },
    )
    store.import_attempt(
        "native-part",
        {
            "path": "xom/part",
            "documents": [document],
            "stage": "summary_part:1",
            "receipt": receipt,
        },
    )
    store.import_attempt(
        "unproven-code",
        {
            "path": "a_share/600348/guess",
            "code": "600348",
            "versions": {"2025": ["c" * 64]},
            "stage": "summary",
            "receipt": receipt,
        },
    )
    artifacts = [
        {
            "path": str(body),
            "metadata": {
                "artifact_key": "used-summary",
                "part_attempt_keys": ["native-part"],
                "unrelated_text": "unproven-code",
            },
        }
    ]
    result = audit_run(store, run_id, artifacts)
    rows = result["inherited_or_reused_attempts_not_charged_to_this_run"]
    assert {row["task_key"] for row in rows} == {"legacy-extract", "native-part"}
    assert {row["company"] for row in rows} == {"us:XOM"}
    assert {row["phase"] for row in rows} == {"extract:financial", "summary_part:1"}
    assert result["totals"]["sent_count"] == 0
    assert result["inherited_totals_not_current_spend"]["sent_count"] == 2


def test_multi_issuer_history_stays_shared_and_requires_every_declared_source(tmp_path):
    store, run_id = make(tmp_path)
    first = {"issuer_id": "a_share:600348", "fiscal_year": 2025, "sha256": "a" * 64}
    second = {"issuer_id": "a_share:600546", "fiscal_year": 2025, "sha256": "b" * 64}
    body = tmp_path / "peer.md"
    atomic_write(body, "peer")
    receipt = {"status": "complete", "cost": {"usd": ".03", "kind": "usage"}}
    store.import_attempt(
        "old-peer",
        {
            "path": "peer/capture",
            "documents": [first, second],
            "stage": "peer",
            "receipt": receipt,
        },
    )
    one = audit_run(
        store, run_id, [{"path": str(body), "metadata": {"documents": [first]}}]
    )
    assert not one["inherited_or_reused_attempts_not_charged_to_this_run"]
    both = audit_run(
        store, run_id, [{"path": str(body), "metadata": {"documents": [first, second]}}]
    )
    assert (
        both["inherited_by_company_phase_not_current_spend"][0]["company"] == "shared"
    )
    assert both["inherited_by_company_phase_not_current_spend"][0]["phase"] == "peer"
