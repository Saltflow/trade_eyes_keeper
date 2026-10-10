"""Runner safety and immutable offline preparation; no paid requests."""

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import run_longitudinal_research as runner
from src.fundamental_quant.contracts import fingerprint
from src.fundamental_quant.store import ResearchStore


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf8")


@pytest.fixture
def authorized(tmp_path):
    config = {"groups": [{"group_id": "liquor", "companies": []}]}
    packet = {"spec": {"group_id": "liquor"}}
    write(tmp_path / "liquor" / "packet.json", packet)
    now = datetime.now(timezone.utc)
    auth = {
        "id": "fixture-new-explicit-round",
        "started_at": (now - timedelta(minutes=1)).isoformat(),
        "expires_at": (now + timedelta(minutes=10)).isoformat(),
        "max_model_calls": 10,
        "max_input_tokens": 100000,
        "max_output_tokens": 100000,
        "packet_hashes": {"liquor": fingerprint(packet)},
        "model": "fixture-model",
        "capabilities": {
            "verified_source": "fixture-not-live",
            "verified_at": now.isoformat(),
            "max_output_tokens": 40000,
            "context_tokens": 1000000,
        },
        "approval_reference": "fixture-explicit-user-budget",
    }
    return config, auth, now


@pytest.mark.parametrize(
    "change",
    [
        "expired",
        "missing_approval",
        "wrong_evidence",
        "negative_limit",
        "extra_group",
        "future",
    ],
)
def test_authorization_fail_closed_before_paid_transport(
    tmp_path, authorized, monkeypatch, change
):
    config, auth, now = authorized
    if change == "expired":
        auth["expires_at"] = (now - timedelta(seconds=1)).isoformat()
    elif change == "missing_approval":
        auth.pop("approval_reference")
    elif change == "wrong_evidence":
        auth["packet_hashes"]["liquor"] = "other"
    elif change == "negative_limit":
        auth["max_model_calls"] = -1
    elif change == "extra_group":
        auth["packet_hashes"]["new-scope"] = "unapproved"
    else:
        auth["started_at"] = (now + timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError):
        runner.validate_authorization(auth, config, tmp_path, now=now)


def test_authorization_is_bound_to_original_ledger_root_and_scope(tmp_path, authorized):
    config, auth, now = authorized
    assert runner.validate_authorization(auth, config, tmp_path, now=now) == auth
    store = ResearchStore(tmp_path / "db")
    runner.bind_authorization(store, auth, tmp_path)
    runner.bind_authorization(store, auth, tmp_path)
    for changed in ({**auth, "model": "different"}, {**auth, "max_model_calls": 20}):
        with pytest.raises(ValueError, match="binding_changed"):
            runner.bind_authorization(store, changed, tmp_path)
    with pytest.raises(ValueError, match="binding_changed"):
        runner.bind_authorization(store, auth, tmp_path / "new-root")


def test_legacy_campaign_cannot_be_adopted(tmp_path, authorized):
    _, auth, _ = authorized
    store = ResearchStore(tmp_path)
    with store.transaction() as db:
        db.execute(
            "CREATE TABLE acceptance_campaigns(id TEXT PRIMARY KEY,payload TEXT)"
        )
        db.execute("INSERT INTO acceptance_campaigns VALUES(?,?)", (auth["id"], "{}"))
    with pytest.raises(ValueError, match="without_longitudinal_authorization"):
        runner.bind_authorization(store, auth, tmp_path)


def test_real_legacy_manifests_preserve_all_companies_and_interim_periods(monkeypatch):
    # Isolate manifest alias parsing; offline unified-catalog discovery has its
    # own real-source coverage acceptance and legitimately adds more originals.
    monkeypatch.setattr(
        "src.fundamental_quant.longitudinal_discovery.archived_documents",
        lambda *args, **kwargs: [],
    )
    # Real legacy metadata is a checked-in fixture, independent of deployment config.
    group = {
        "companies": [
            {"issuer_id": issuer, "name": issuer}
            for issuer in ("a_share:600502", "a_share:601800", "a_share:000498")
        ],
        "document_manifests": [
            str(path)
            for path in sorted(
                (Path(__file__).parent / "fixtures" / "longitudinal_manifest_aliases").glob("*.json")
            )
        ],
    }
    docs = runner.discover_documents(group, "2026-10-10")
    assert len(docs) == 13
    assert {d["issuer_id"] for d in docs} == {
        "a_share:600502",
        "a_share:601800",
        "a_share:000498",
    }
    for issuer in ("a_share:600502", "a_share:601800", "a_share:000498"):
        assert any(
            d["issuer_id"] == issuer and d["period_end"] == "2026-06-30" for d in docs
        )
    assert all(d["sha256"] == d["sha256"].lower() for d in docs)


def test_unknown_period_not_silently_assumed_annual(tmp_path):
    manifest = {
        "ticker": "600502",
        "documents": [
            {
                "title": "2025 报告",
                "local_file": "x.pdf",
                "sha256": "a" * 64,
                "disclosure_date": "2026-01-01",
            }
        ],
    }
    write(tmp_path / "manifest.json", manifest)
    group = {
        "companies": [{"issuer_id": "a_share:600502", "name": "test"}],
        "document_manifests": [str(tmp_path / "manifest.json")],
    }
    with pytest.raises(ValueError, match="period_kind_ambiguous"):
        runner.discover_documents(group, "2026-10-10")


def test_preparation_reuses_and_invalidates_valuation_change(tmp_path, monkeypatch):
    import src.fundamental_quant.longitudinal_data as data

    group = {
        "group_id": "test",
        "companies": [],
        "industry": "test",
        "history_years": 7,
    }
    config = {"groups": [group]}
    valuation = {"price_events": [], "statement_events": [], "issues": []}
    monkeypatch.setattr(
        data, "load_cached_valuation_events", lambda spec: copy.deepcopy(valuation)
    )
    calls = []

    def prepare(spec, roots, **kwargs):
        calls.append(kwargs)
        packet = {"spec": spec, "documents": [], "facts": {}, "rows": [], "issues": []}
        packet["packet_hash"] = fingerprint(packet)
        return packet

    monkeypatch.setattr(runner, "prepare_comparison", prepare)
    monkeypatch.setattr(runner, "render_longitudinal_report", lambda *args: "")
    assert runner.prepare(tmp_path, config, "2026-10-10")[0]["reused"] is False
    assert runner.prepare(tmp_path, config, "2026-10-10")[0]["reused"] is True
    assert len(calls) == 1
    valuation["issues"].append({"code": "new_cache_revision"})
    assert runner.prepare(tmp_path, config, "2026-10-10")[0]["reused"] is False
    write(tmp_path / "test" / "checkpoint.json", {"status": "researching"})
    valuation["issues"].append({"code": "another_revision"})
    with pytest.raises(ValueError, match="checkpoint_exists"):
        runner.prepare(tmp_path, config, "2026-10-10")


def test_cache_reuse_rechecks_immutable_pack_content(tmp_path, monkeypatch):
    import src.fundamental_quant.fr_pack as packs
    import src.fundamental_quant.longitudinal_data as data

    pack_root = tmp_path / "packs"
    write(
        pack_root / "latest_run.json", {"entries": [{"pack_key": "same-manifest-key"}]}
    )
    monkeypatch.setattr(
        data,
        "load_cached_valuation_events",
        lambda spec: {"price_events": [], "statement_events": [], "issues": []},
    )

    def corrupt(folder):
        raise ValueError("immutable_cached_source_corrupt")

    monkeypatch.setattr(packs, "verified_pack", corrupt)
    with pytest.raises(ValueError, match="immutable_cached_source_corrupt"):
        runner._preparation_inputs(
            {"pack_roots": [str(pack_root)]}, {"companies": []}, []
        )


def test_cli_run_never_implicitly_authorizes(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    write(config, {"groups": []})
    monkeypatch.setattr(
        runner.sys,
        "argv",
        [
            "run_longitudinal_research.py",
            "run",
            "--config",
            str(config),
            "--root",
            str(tmp_path),
        ],
    )

    def no_run(*args):
        pytest.fail("run called without explicit authorization")

    monkeypatch.setattr(runner, "run", no_run)
    with pytest.raises(SystemExit) as exc:
        runner.main()
    assert exc.value.code == 2


def test_preparation_uses_isolated_valuation_snapshot(tmp_path, monkeypatch):
    observed = []
    def load(spec):
        observed.append(spec["valuation_store_root"])
        return {"price_events": [], "statement_events": [], "external_evidence": {}, "issues": []}
    monkeypatch.setattr("src.fundamental_quant.longitudinal_data.load_cached_valuation_events", load)
    group = {"valuation_store_root": str(tmp_path / "frozen-pit")}
    inputs, _ = runner._preparation_inputs(group, {"companies": [], "as_of": "2026-10-10"}, [])
    assert observed == [group["valuation_store_root"]]
    assert "valuation_events_hash" in inputs


def test_catalog_url_fills_duplicate_manifest_missing_url(tmp_path, monkeypatch):
    manifest_path = tmp_path / "manifest.json"
    entry = {"issuer_id": "a_share:600039", "period_end": "2022-12-31", "published_at": "2023-04-01", "pdf_file": "original.pdf", "pdf_sha256": "a" * 64}
    write(manifest_path, {"documents": [entry]})
    group = {"companies": [{"issuer_id": "a_share:600039", "name": "test"}], "document_manifests": [str(manifest_path)]}
    catalog = {"issuer_id": entry["issuer_id"], "period_end": entry["period_end"], "published_at": entry["published_at"], "sha256": entry["pdf_sha256"], "url": "https://static.cninfo.com.cn/fixture.pdf"}
    monkeypatch.setattr("src.fundamental_quant.longitudinal_discovery.archived_documents", lambda *a: [catalog])
    docs = runner.discover_documents(group, "2026-10-10")
    assert len(docs) == 1
    assert docs[0]["url"] == catalog["url"]
    assert docs[0]["path"] == str((tmp_path / "original.pdf").resolve())
