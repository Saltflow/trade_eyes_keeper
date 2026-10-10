"""Offline release controls, never investment or paid-model acceptance."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.fundamental_quant.contracts import fingerprint
from src.fundamental_quant.longitudinal_release import (
    deliver_group_notifications,
    get_group_report,
    group_bot_view,
    list_group_reports,
    publication_errors,
    publish_group_report,
    withdraw_group_report,
)
from src.fundamental_quant.longitudinal_workflow import AXES, KEYS
from src.fundamental_quant.store import ResearchStore


@pytest.fixture
def accepted():
    packet = {
        "spec": {
            "group_id": "test-peers",
            "as_of": "2026-10-10",
            "companies": [{"issuer_id": "a_share:600001"}],
        },
        "documents": [{"document_id": "D1", "pages": {"1": "fixture original"}}],
    }
    packet["packet_hash"] = fingerprint(packet)
    claim = {
        "status": "supported",
        "basis": "fixture judgement",
        "refs": ["D1:p1"],
        "counterevidence": ["limited history"],
    }
    result = {
        "title": "Fixture",
        "summary": "fixture, not real research",
        "final": True,
        "axes": {key: deepcopy(claim) for key in AXES},
        "claims": [dict(deepcopy(claim), id=key, key=key) for key in KEYS],
    }
    review = {
        "action": "review",
        "verdict": "pass",
        "issues": [],
        "checks": [
            {
                "claim_id": key,
                "result": "pass",
                "refs": ["D1:p1"],
                "reason": "fixture check",
            }
            for key in (*AXES, *KEYS)
        ],
    }
    state = {
        "status": "accepted",
        "packet_hash": fingerprint(packet),
        "deterministic_errors": [],
        "approved_result": result,
        "research_result": deepcopy(result),
        "usage": {"model_calls": 0},
    }
    state["report_version"] = fingerprint(
        {"packet": state["packet_hash"], "result": result, "review": review}
    )
    state["reviews"] = [
        dict(
            review,
            result_hash=fingerprint(result),
            validation_errors=[],
            independence="separate_context_same_model",
        )
    ]
    return packet, state


def _publish(store, accepted, **kwargs):
    return publish_group_report(store, *accepted, **kwargs)


def _config(monkeypatch):
    monkeypatch.delenv("SKIP_NOTIFICATIONS", raising=False)
    monkeypatch.delenv("SKIP_EMAIL", raising=False)
    return {
        "fundamental_quant": {"notifications": {"enabled": True}},
        "notification": {"email": {"enabled": True}},
    }


def test_publication_bot_email_same_version_and_no_candidate(
    tmp_path, accepted, monkeypatch
):
    store = ResearchStore(tmp_path)
    report = _publish(store, accepted, channels=["email"])
    assert report["candidate"] is False
    assert Path(report["html_path"]).read_text(encoding="utf8") == report["html"]
    assert group_bot_view(report)["report"] == accepted[1]["approved_result"]
    assert list_group_reports(store, "600001")[0]["version"] == report["version"]
    sent = []
    manager = SimpleNamespace(
        email=SimpleNamespace(
            _send_email=lambda title, body: sent.append(body) or {"status": "sent"}
        )
    )
    results = deliver_group_notifications(store, _config(monkeypatch), manager=manager)
    assert results[0]["status"] == "sent"
    assert sent == [report["email_html"]]
    assert report["version"] in sent[0]
    assert "fixture judgement" in sent[0]
    assert "fixture original" not in sent[0]
    assert (
        deliver_group_notifications(store, _config(monkeypatch), manager=manager) == []
    )
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM publications").fetchone()[0] == 0


@pytest.mark.parametrize(
    "mutation",
    ["status", "review", "hash", "deterministic", "citation", "checks", "newer"],
)
def test_publication_blocks_invalid_accepted_labels(tmp_path, accepted, mutation):
    packet, state = accepted
    if mutation == "status":
        state["status"] = "reviewing"
    elif mutation == "review":
        state["reviews"][-1]["independence"] = "self_review"
    elif mutation == "hash":
        packet["documents"][0]["pages"]["1"] = "changed evidence"
    elif mutation == "deterministic":
        state["deterministic_errors"] = ["wrong_currency"]
    elif mutation == "citation":
        state["approved_result"]["claims"][0]["refs"] = ["invented"]
    elif mutation == "checks":
        state["reviews"][-1]["checks"] = []
    else:
        state["research_result"]["summary"] = "new unchecked text"
    assert publication_errors(packet, state)
    with pytest.raises(ValueError):
        _publish(ResearchStore(tmp_path), accepted)


def test_withdrawal_cancels_queued_and_prevents_bot_retrieval(
    tmp_path, accepted, monkeypatch
):
    store = ResearchStore(tmp_path)
    report = _publish(store, accepted, channels=["email"])
    withdraw_group_report(store, report["version"], "material source correction")
    assert get_group_report(store, version=report["version"]) is None
    assert list_group_reports(store) == []
    assert "material source correction" in Path(report["html_path"]).read_text(
        encoding="utf8"
    )
    assert deliver_group_notifications(store, _config(monkeypatch)) == []
    with pytest.raises(ValueError, match="withdrawn version"):
        _publish(store, accepted)


def test_withdrawal_after_delivery_has_one_correction(tmp_path, accepted, monkeypatch):
    store = ResearchStore(tmp_path)
    report = _publish(store, accepted, channels=["email"])
    sent = []
    manager = SimpleNamespace(
        email=SimpleNamespace(
            _send_email=lambda title, body: (
                sent.append((title, body)) or {"status": "sent"}
            )
        )
    )
    config = _config(monkeypatch)
    deliver_group_notifications(store, config, manager=manager)
    withdraw_group_report(store, report["version"], "changed source")
    withdraw_group_report(store, report["version"], "changed source")
    deliver_group_notifications(store, config, manager=manager)
    assert len(sent) == 2
    assert "更正" in sent[-1][0]
    assert "fixture judgement" not in sent[-1][1]
    assert deliver_group_notifications(store, config, manager=manager) == []


def test_inflight_withdrawal_late_receipt_queues_correction(
    tmp_path, accepted, monkeypatch
):
    store = ResearchStore(tmp_path)
    report = _publish(store, accepted, channels=["email"])

    def sender(title, body):
        withdraw_group_report(store, report["version"], "changed during send")
        return {"status": "sent"}

    deliver_group_notifications(
        store,
        _config(monkeypatch),
        manager=SimpleNamespace(email=SimpleNamespace(_send_email=sender)),
    )
    with store.transaction() as db:
        pending = db.execute(
            "SELECT * FROM longitudinal_outbox WHERE kind='correction'"
        ).fetchall()
        assert len(pending) == 1
        assert pending[0]["status"] == "pending"


def test_unknown_delivery_not_resent_on_restart(tmp_path, accepted, monkeypatch):
    store = ResearchStore(tmp_path)
    _publish(store, accepted, channels=["email"])
    calls = []

    def sender(title, body):
        calls.append(title)
        raise TimeoutError("ambiguous provider outcome")

    manager = SimpleNamespace(email=SimpleNamespace(_send_email=sender))
    config = _config(monkeypatch)
    deliver_group_notifications(store, config, manager=manager)
    assert (
        deliver_group_notifications(ResearchStore(tmp_path), config, manager=manager)
        == []
    )
    assert len(calls) == 1


def test_tampered_html_fails_closed(tmp_path, accepted):
    store = ResearchStore(tmp_path)
    report = _publish(store, accepted)
    Path(report["html_path"]).write_text("changed", encoding="utf8")
    assert list_group_reports(store) == []


def test_service_bot_query_returns_current_group_without_registry_entry(
    tmp_path, accepted
):
    from src.fundamental_quant.service import FundamentalQuantService

    store = ResearchStore(tmp_path)
    report = _publish(store, accepted)
    service = FundamentalQuantService.__new__(FundamentalQuantService)
    service._store = store
    service.status = lambda: {"companies": [], "errors": []}
    result = service.query("600001")
    assert result["longitudinal_reports"][0]["version"] == report["version"]
    assert result["errors"] == []
    withdraw_group_report(store, report["version"], "withdraw")
    assert service.query("600001")["longitudinal_reports"] == []


def test_incremental_publication_requires_review_of_new_evidence(tmp_path, accepted):
    packet, state = accepted
    effective = deepcopy(packet)
    effective["documents"].append({"document_id": "D2", "pages": {"1": "new original"}})
    effective.pop("packet_hash")
    effective["packet_hash"] = fingerprint(effective)
    state["effective_packet"] = effective
    state["evidence_revision"] = fingerprint(effective)
    state["evidence_updates"] = [{"new_hash": fingerprint(effective)}]
    assert "publication_review_evidence_mismatch" in publication_errors(packet, state)
    original_review = {
        k: v
        for k, v in state["reviews"][-1].items()
        if k not in {"result_hash", "validation_errors", "independence"}
    }
    state["reviews"][-1]["evidence_revision"] = state["evidence_revision"]
    state["report_version"] = fingerprint(
        {
            "packet": state["packet_hash"],
            "evidence_revision": state["evidence_revision"],
            "result": state["approved_result"],
            "review": original_review,
        }
    )
    assert publication_errors(packet, state) == []
    store = ResearchStore(tmp_path)
    report = publish_group_report(store, packet, state)
    assert report["payload"]["packet"] == effective
    assert report["payload"]["base_packet"] == packet
    assert report["packet_hash"] == effective["packet_hash"]
    assert get_group_report(store, "test-peers")["version"] == report["version"]
    state["effective_packet"]["documents"][-1]["pages"]["1"] = "tampered"
    assert "publication_incremental_evidence_mismatch" in publication_errors(
        packet, state
    )
