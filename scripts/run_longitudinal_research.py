"""Offline preparation and explicitly budgeted DeepSeek peer research.

No command creates an authorization or resets/extends a campaign. Prepare,
estimate, status and render never instantiate a paid transport.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.core.process_lock import exclusive_process_lock
from src.fundamental_quant.comparison_transport import _write
from src.fundamental_quant.longitudinal_data import prepare_comparison
from src.fundamental_quant.longitudinal_report import render_longitudinal_report
from src.fundamental_quant.longitudinal_workflow import (
    OUTPUTS,
    RESEARCH_PROMPT,
    REVIEW_PROMPT,
    LongitudinalWorkflow,
    compact_packet,
    digest,
    dumps,
    read,
)
from src.fundamental_quant.session_context import load_token_counter

DEFAULT_CONFIG = Path("config/longitudinal_research.json")
DEFAULT_ROOT = Path("data/fundamental_quant/longitudinal_research_20261010")
TOKENIZER = Path("data/fundamental_quant/tokenizer/tokenizer.json")


PREPARATION_ADAPTER_VERSION = "longitudinal-preparation/5"


def _manifest_document(entry, manifest, path, companies):
    parent = manifest if isinstance(manifest, dict) else {}
    code = str(
        entry.get("issuer_id")
        or entry.get("issuer")
        or entry.get("code")
        or entry.get("ticker")
        or parent.get("code")
        or parent.get("ticker")
        or ""
    )
    issuer = code if ":" in code else "a_share:" + code
    if issuer not in companies:
        return None
    end = entry.get("period_end")
    if not end:
        title = " ".join(
            str(entry.get(k, "")) for k in ("period", "title", "key", "document_id")
        )
        year = re.search(r"20[0-9]{2}", title)
        if not year:
            raise ValueError("manifest_period_missing:" + str(path))
        folded = title.lower()
        if any(x in folded for x in ("h1", "half", "interim", "半年度", "中报")):
            suffix = "-06-30"
        elif any(x in folded for x in ("q1", "一季度", "第一季度")):
            suffix = "-03-31"
        elif any(x in folded for x in ("q3", "三季度", "第三季度")):
            suffix = "-09-30"
        elif any(x in folded for x in ("annual", "年度", "年报")):
            suffix = "-12-31"
        else:
            raise ValueError("manifest_period_kind_ambiguous:" + str(path))
        end = year[0] + suffix
    published = (
        entry.get("publication_date")
        or entry.get("disclosure_date")
        or entry.get("published_at")
    )
    file = entry.get("pdf_file") or entry.get("file") or entry.get("local_file")
    sha = str(entry.get("pdf_sha256") or entry.get("sha256") or "").lower()
    if not file or not published or not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise ValueError("manifest_source_metadata_incomplete:" + str(path))
    end, published = (
        date.fromisoformat(end[:10]).isoformat(),
        date.fromisoformat(published[:10]).isoformat(),
    )
    return {
        "issuer_id": issuer,
        "company_name": companies[issuer]["name"],
        "period_end": end,
        "published_at": published,
        "path": str((path.parent / file).resolve()),
        "sha256": sha,
        "url": entry.get("source_url") or entry.get("url", ""),
        **({"short_id": entry["short_id"]} if entry.get("short_id") else {}),
    }


def discover_documents(group, as_of):
    """Adapt explicit manifest metadata, including legacy identity/date aliases."""
    companies = {c["issuer_id"]: c for c in group["companies"]}
    docs = {}
    for manifest_path in group.get("document_manifests", []):
        path = Path(manifest_path)
        manifest = read(path)
        entries = (
            manifest if isinstance(manifest, list) else manifest.get("documents", [])
        )
        for entry in entries:
            doc = _manifest_document(entry, manifest, path, companies)
            if doc:
                key = (doc["issuer_id"], doc["sha256"], doc["published_at"])
                if key in docs and docs[key]["period_end"] != doc["period_end"]:
                    raise ValueError("manifest_source_period_conflict")
                docs[key] = doc
    if group.get("ledger_issuers"):
        dbpath = Path("data/fundamental_quant/research.sqlite3").resolve()
        with sqlite3.connect(dbpath.as_uri() + "?mode=ro", uri=True) as db:
            for issuer in group["ledger_issuers"]:
                if issuer not in companies:
                    raise ValueError("ledger_issuer_outside_group")
                record = db.execute(
                    "SELECT packet FROM companies WHERE id=?", (issuer,)
                ).fetchone()
                if not record or not record[0]:
                    continue
                for source in json.loads(record[0]).get("sources", []):
                    sha, file = (
                        source.get("document_sha256"),
                        source.get("document_path"),
                    )
                    if not sha or not file or not source.get("published_at"):
                        continue
                    doc = {
                        "issuer_id": issuer,
                        "company_name": companies[issuer]["name"],
                        "period_end": source["period_end"][:10],
                        "published_at": source["published_at"][:10],
                        "path": str(Path(file).resolve()),
                        "sha256": sha.lower(),
                        "url": source.get("url", ""),
                    }
                    docs[(issuer, sha.lower(), doc["published_at"])] = doc
    from src.fundamental_quant.longitudinal_discovery import archived_documents

    for doc in archived_documents(group["companies"], as_of):
        key = (doc["issuer_id"], doc["sha256"], doc["published_at"])
        if key in docs and docs[key]["period_end"] != doc["period_end"]:
            raise ValueError("catalog_source_period_conflict")
        if key in docs and not docs[key].get("url") and doc.get("url"):
            docs[key]["url"] = doc["url"]
        docs.setdefault(key, doc)
    lower = int(as_of[:4]) - group.get("history_years", 7)
    return sorted(
        (
            d
            for d in docs.values()
            if int(d["period_end"][:4]) >= lower and d["published_at"] <= as_of
        ),
        key=lambda d: (d["issuer_id"], d["period_end"], d["published_at"]),
    )


def _preparation_inputs(group, spec, docs):
    from src.fundamental_quant.fr_pack import verified_pack
    from src.fundamental_quant.fr_pack_contract import PARSER_VERSION
    from src.fundamental_quant.longitudinal_data import (
        VERSION,
        load_cached_valuation_events,
    )
    from src.fundamental_quant.share_count_sources import VERSION as SHARE_COUNT_VERSION

    inputs = {
        "share_count_version": SHARE_COUNT_VERSION,
        "spec": spec,
        "documents": docs,
        "pack_roots": group.get("pack_roots", []),
        "adapter_version": PREPARATION_ADAPTER_VERSION,
        "parser_version": PARSER_VERSION,
        "data_version": VERSION,
        "pack_manifests": {},
        "verified_packs": {},
    }
    for doc in docs:
        if hashlib.sha256(Path(doc["path"]).read_bytes()).hexdigest() != doc["sha256"]:
            raise ValueError("source_file_changed:" + doc["path"])
    for directory in group.get("pack_roots", []):
        pack_root = Path(directory)
        manifest = read(pack_root / "latest_run.json")
        inputs["pack_manifests"][directory] = manifest
        for entry in manifest["entries"]:
            if not entry.get("pack_key"):
                continue
            folder = pack_root / "packs" / entry["pack_key"]
            pack, source = verified_pack(folder)
            inputs["verified_packs"][str(folder)] = digest(
                {"pack": pack, "source": source}
            )
    valuation = load_cached_valuation_events(
        {
            **spec,
            **(
                {"valuation_store_root": group["valuation_store_root"]}
                if group.get("valuation_store_root")
                else {}
            ),
        }
    )
    inputs["valuation_events_hash"] = digest(valuation)
    inputs["supplemental_facts_hashes"] = {
        p: digest(read(p)) for p in group.get("supplemental_facts_paths", [])
    }
    from src.fundamental_quant.fr_image_evidence import (
        VERSION as IMAGE_EVIDENCE_VERSION,
    )

    inputs["image_evidence_version"] = IMAGE_EVIDENCE_VERSION
    inputs["image_evidence_hashes"] = {}
    for manifest_path in group.get("image_evidence_paths", []):
        manifest = read(manifest_path)
        receipt_path = Path(manifest_path).with_suffix(".receipt.json")
        inputs["image_evidence_hashes"][manifest_path] = {
            "manifest": digest(manifest),
            "receipt": digest(read(receipt_path)),
            "images": {
                item["image_path"]: hashlib.sha256(
                    Path(item["image_path"]).read_bytes()
                ).hexdigest()
                for item in manifest.get("pages", [])
            },
        }
    return inputs, valuation


def prepare(root, config, as_of):
    root.mkdir(parents=True, exist_ok=True)
    outputs = []
    for group in config["groups"]:
        folder = root / group["group_id"]
        folder.mkdir(exist_ok=True)
        spec = {
            k: group[k] for k in ("group_id", "companies", "industry", "history_years")
        }
        spec["as_of"] = as_of
        if group.get("valuation_store_root"):
            spec["valuation_store_root"] = group["valuation_store_root"]
        if group.get("title"):
            spec["title"] = group["title"]
        docs = discover_documents(group, as_of)
        inputs, valuation = _preparation_inputs(group, spec, docs)
        input_hash = digest(inputs)
        packet_path = folder / "packet.json"
        cache = folder / "prepare_receipt.json"
        if (
            packet_path.exists()
            and cache.exists()
            and read(cache).get("input_hash") == input_hash
        ):
            packet = read(packet_path)
            if digest(packet) != read(cache)["packet_hash"]:
                raise ValueError("prepared_packet_tampered")
            reused = True
        else:
            if (folder / "checkpoint.json").exists():
                raise ValueError(
                    "research_checkpoint_exists_use_new_output_for_changed_evidence"
                )
            prices, statements = (
                valuation["price_events"],
                valuation["statement_events"],
            )
            packet = prepare_comparison(
                spec,
                [Path(p) for p in group.get("pack_roots", [])],
                document_specs=docs,
                price_events=prices,
                statement_events=statements,
                external_evidence=valuation.get("external_evidence", {}),
                image_evidence_paths=[
                    Path(p) for p in group.get("image_evidence_paths", [])
                ],
                supplemental_facts_paths=[
                    Path(p) for p in group.get("supplemental_facts_paths", [])
                ],
            )
            packet.setdefault("issues", []).extend(valuation["issues"])
            packet.pop("packet_hash", None)
            packet["packet_hash"] = digest(packet)
            _write(packet_path, packet)
            _write(cache, {"input_hash": input_hash, "packet_hash": digest(packet)})
            reused = False
        from src.fundamental_quant.longitudinal_discovery import (
            original_report_coverage,
        )

        original_coverage = original_report_coverage(spec, packet.get("documents", []))
        original_coverage["packet_hash"] = digest(packet)
        _write(folder / "original_report_coverage.json", original_coverage)
        state = (
            read(folder / "checkpoint.json")
            if (folder / "checkpoint.json").exists()
            else {"status": "prepared"}
        )
        render_longitudinal_report(packet, state, folder / "index.html")
        outputs.append(
            {
                "group_id": group["group_id"],
                "documents": len(packet.get("documents", [])),
                "facts": len(packet.get("facts", {})),
                "rows": len(packet.get("rows", [])),
                "original_reports_available": original_coverage["available"],
                "original_reports_missing": original_coverage["missing"],
                "original_report_coverage": str(
                    (folder / "original_report_coverage.json").resolve()
                ),
                "reused": reused,
                "packet_hash": digest(packet),
                "report": str((folder / "index.html").resolve()),
            }
        )
    _write(root / "prepared.json", {"as_of": as_of, "groups": outputs, "paid_calls": 0})
    return outputs


def estimate(root, config):
    from src.fundamental_quant.longitudinal_costs import FLASH_RATES, estimate_cost

    count = load_token_counter(TOKENIZER)
    groups = []
    for group in config["groups"]:
        packet = read(root / group["group_id"] / "packet.json")
        seed = compact_packet(packet)
        initial = count(
            dumps(
                [
                    {"role": "system", "content": RESEARCH_PROMPT},
                    {"role": "user", "content": dumps(seed)},
                ]
            )
        )
        original = sum(
            count(str(c.get("text", ""))) for c in packet.get("chapters", [])
        )
        # Scenarios are NOT cost promises: research chooses tools, review may need originals.
        # Two calls is a lower bound; 6 and 12 include repeat context and corrections.
        scenarios = []
        for calls in (2, 6, 12):
            per_input = min(900000, initial + count(REVIEW_PROMPT) + 32768 + 100000)
            scenarios.append(
                {
                    "calls": calls,
                    "input_reservation_upper_scenario": calls
                    * (per_input + max(4096, int(per_input * 0.05))),
                    "output_reservation_upper_scenario": calls * 32768,
                    "assumption": "planning scenario: growing stable context plus 100k source tokens per call; actual tool choice may require more calls/compaction; not a completion guarantee",
                    "all_miss_fee_estimate": estimate_cost(
                        calls * (per_input + max(4096, int(per_input * 0.05))),
                        calls * 32768,
                    ),
                }
            )
        groups.append(
            {
                "group_id": group["group_id"],
                "packet_hash": digest(packet),
                "initial_input_tokens": initial,
                "initial_request_fits_1m_context": initial
                + OUTPUTS["research"]
                + max(4096, int(initial * 0.05))
                <= 1000000,
                "context_headroom_after_research_output": 1000000
                - initial
                - OUTPUTS["research"],
                "all_chapter_tokens": original,
                "minimum_research_review_calls": 2,
                "scenarios": scenarios,
            }
        )
    result = {
        "groups": groups,
        "output_limits": OUTPUTS,
        "paid_calls": 0,
        "fee_status": "published_rate_scenarios_not_provider_invoice",
        "rate_profile": FLASH_RATES,
        "note": "token reservations are not provider bills; unknown usage is never zero; no automatic paid start",
    }
    _write(root / "estimate.json", result)
    return result


def render_index(root, config):
    import html

    root.mkdir(parents=True, exist_ok=True)
    blocks = []
    estimates = (
        read(root / "estimate.json") if (root / "estimate.json").exists() else {}
    )
    for group in config["groups"]:
        folder = root / group["group_id"]
        if not (folder / "packet.json").exists():
            continue
        packet = read(folder / "packet.json")
        state = (
            read(folder / "checkpoint.json")
            if (folder / "checkpoint.json").exists()
            else {"status": "prepared"}
        )
        item = next(
            (
                g
                for g in estimates.get("groups", [])
                if g["group_id"] == group["group_id"]
            ),
            {},
        )
        names = "、".join(c["name"] for c in group["companies"])
        blocks.append(
            f"<section><h2>{html.escape(names)}</h2><p>状态：{html.escape(state['status'])} · 财报 {len(packet['documents'])} 份 · 时点 {len(packet['rows'])} 个</p>"
            + f'<p><a href="{html.escape(group["group_id"])}/index.html">打开资料、图表与研究进展</a></p>'
            + "<p>尚无有效报告时，不显示投资结论。完整原文和引用均可查看。</p>"
            + "<details><summary>真实输入计量与费用场景</summary><pre>"
            + html.escape(json.dumps(item, ensure_ascii=False, indent=2))
            + "</pre></details></section>"
        )
    body = (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>长期同行自动研究</title><style>body{max-width:1100px;margin:45px auto;padding:0 24px;background:#f4f5f2;color:#233b3b;font:16px/1.8 system-ui}section{background:white;border:1px solid #dce5de;padding:24px;margin:20px 0;border-radius:12px}a{color:#167872}pre{white-space:pre-wrap;font-size:13px}</style><h1>长期同行自动研究</h1><p>先取得可复用证据，再由 DeepSeek 研究和独立审查。准备页不等于已验收研究报告。</p>'
        + "".join(blocks)
        + "</html>"
    )
    _write(root / "index.html", body.encode("utf8"))


def validate_authorization(auth, config, root, now=None):
    now = now or datetime.now(timezone.utc)
    groups = [g["group_id"] for g in config.get("groups", [])]
    if (
        not groups
        or len(set(groups)) != len(groups)
        or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", g) for g in groups)
    ):
        raise ValueError("invalid_authorization_groups")
    required = (
        "id",
        "started_at",
        "expires_at",
        "max_model_calls",
        "max_input_tokens",
        "max_output_tokens",
        "packet_hashes",
        "model",
        "capabilities",
        "approval_reference",
    )
    if any(not auth.get(key) for key in required):
        raise ValueError("explicit_complete_authorization_required")
    start, end = (
        datetime.fromisoformat(auth["started_at"]),
        datetime.fromisoformat(auth["expires_at"]),
    )
    if start.tzinfo is None or end.tzinfo is None or not start <= now < end:
        raise ValueError("authorization_not_active")
    for key in ("max_model_calls", "max_input_tokens", "max_output_tokens"):
        if type(auth[key]) is not int or auth[key] <= 0:
            raise ValueError("invalid_authorization_limit")
    if (
        not isinstance(auth["id"], str)
        or not auth["id"].strip()
        or not isinstance(auth["approval_reference"], str)
        or not auth["approval_reference"].strip()
    ):
        raise ValueError("explicit_authorization_reference_required")
    if set(auth["packet_hashes"]) != set(groups):
        raise ValueError("authorization_group_scope_mismatch")
    caps = auth["capabilities"]
    if not caps.get("verified_source") or caps.get("max_output_tokens", 0) < max(
        OUTPUTS.values()
    ):
        raise ValueError("verified_model_output_capability_required")
    if caps.get("context_tokens", 0) <= max(OUTPUTS.values()) or not caps.get(
        "verified_at"
    ):
        raise ValueError("verified_model_context_capability_required")
    for group in config["groups"]:
        ident = group["group_id"]
        if auth["packet_hashes"].get(ident) != digest(
            read(root / ident / "packet.json")
        ):
            raise ValueError("authorization_evidence_mismatch:" + ident)
    return auth


def bind_authorization(store, auth, root):
    """Bind a campaign's model/evidence/approval and output root across restarts."""
    identity = {"authorization": auth, "root": str(Path(root).resolve())}
    with store.transaction() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS longitudinal_authorizations (id TEXT PRIMARY KEY,payload TEXT NOT NULL)"
        )
        existing = db.execute(
            "SELECT payload FROM longitudinal_authorizations WHERE id=?", (auth["id"],)
        ).fetchone()
        if existing and json.loads(existing[0]) != identity:
            raise ValueError("campaign_authorization_binding_changed")
        if not existing:
            has_campaigns = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='acceptance_campaigns'"
            ).fetchone()
            if (
                has_campaigns
                and db.execute(
                    "SELECT 1 FROM acceptance_campaigns WHERE id=?", (auth["id"],)
                ).fetchone()
            ):
                raise ValueError("existing_campaign_without_longitudinal_authorization")
            db.execute(
                "INSERT INTO longitudinal_authorizations VALUES(?,?)",
                (auth["id"], dumps(identity)),
            )


def run(root, config, authorization):
    from dotenv import dotenv_values

    from src.core.config_store import ConfigStore
    from src.fundamental_quant.acceptance_campaign import AcceptanceCampaign
    from src.fundamental_quant.comparison_transport import ComparisonTransport
    from src.fundamental_quant.longitudinal_release import (
        deliver_group_notifications,
        publish_group_report,
    )
    from src.fundamental_quant.store import ResearchStore

    auth = validate_authorization(read(authorization), config, root)
    frozen_path = root / "authorization.json"
    if frozen_path.exists() and read(frozen_path) != auth:
        raise ValueError("authorization_immutable_use_existing_round")
    _write(frozen_path, auth)
    cfg = ConfigStore().load_runtime()
    credential = dotenv_values("config/.env").get("DEEPSEEK_API_KEY")
    if credential:
        cfg.setdefault("llm", {})["api_key"] = credential
    cfg.setdefault("fundamental_quant", {}).setdefault("llm", {})["model"] = auth[
        "model"
    ]
    store = ResearchStore(Path("data/fundamental_quant"))
    bind_authorization(store, auth, root)
    settings = {
        "enabled": True,
        **{
            k: auth[k]
            for k in (
                "id",
                "started_at",
                "expires_at",
                "max_model_calls",
                "max_input_tokens",
                "max_output_tokens",
            )
        },
        "target_limit": len(config["groups"]),
        "max_extraction_batches_per_company": 24,
    }
    campaign = AcceptanceCampaign({"acceptance": settings}, store)
    before = campaign.state()
    if before.get("status") == "not_started":
        campaign.start([g["group_id"] for g in config["groups"]])
    elif before.get("config") != settings or before.get("status") != "active":
        raise ValueError("existing_campaign_changed_or_inactive")
    transport = ComparisonTransport(
        cfg,
        campaign,
        root / "private_calls",
        TOKENIZER,
        context_tokens=auth["capabilities"]["context_tokens"],
        max_output_tokens=auth["capabilities"]["max_output_tokens"],
        image_token_upper_bound=auth["capabilities"].get("image_token_upper_bound"),
        request_timeout_seconds=600,
    )
    transport.rate_profile = auth.get("rate_profile")
    for group in config["groups"]:
        folder = root / group["group_id"]
        packet = read(folder / "packet.json")
        caps = auth["capabilities"]
        from src.fundamental_quant.longitudinal_tools import DocumentAcquirer

        flow = LongitudinalWorkflow(
            folder,
            packet,
            transport,
            fetcher=DocumentAcquirer(packet, cfg, store, campaign),
            context_tokens=caps["context_tokens"],
            max_request_bytes=caps.get("max_request_bytes", 48 * 1024 * 1024),
            max_images=caps.get("max_images", 600),
        )
        if flow.state["status"] == "accepted":
            # Recovery after a crash between final review, durable publication
            # and delivery; no new model call and no duplicate sent notification.
            publish_group_report(store, packet, flow.state, channels=("email",))
            deliver_group_notifications(store, cfg)
            continue
        if flow.state["status"] == "blocked":
            return {
                "status": "blocked",
                "group_id": group["group_id"],
                "reason": flow.state.get("error"),
            }
        # Reserve capacity for at least one study and one review before starting group.
        state = campaign.state()
        min_input = 2 * (
            flow._tokens(flow.state["histories"]["research"])
            + 4096
            + OUTPUTS["research"]
        )
        needed = {
            "model_calls": 2,
            "input_token_reservations": min_input,
            "output_token_reservations": OUTPUTS["research"] + OUTPUTS["review"],
        }
        if any(
            state["usage"].get(k, 0) + v > state["limits"][k] for k, v in needed.items()
        ):
            return {
                "status": "insufficient_budget_for_group_and_review",
                "group_id": group["group_id"],
            }
        outcome = flow.run()
        if outcome["status"] != "accepted":
            return {
                "status": outcome["status"],
                "group_id": group["group_id"],
                "error": outcome.get("error"),
                "usage": transport.status(),
            }
        publication = publish_group_report(store, packet, outcome, channels=("email",))
        receipts = deliver_group_notifications(store, cfg)
        _write(
            folder / "publication_receipt.json",
            {"publication": publication, "notifications": receipts},
        )
    return {"status": "accepted", "usage": transport.status()}


def acquire(
    root,
    config,
    as_of,
    *,
    run_id,
    max_documents=8,
    max_seconds=900,
    groups=None,
    source=None,
):
    """Explicit public-document network step, followed by offline preparation."""
    from src.fundamental_quant.longitudinal_acquisition import (
        PublicAcquisition,
        acquisition_tasks,
        public_report_source,
    )
    from src.fundamental_quant.longitudinal_discovery import original_report_coverage

    selected = [g for g in config["groups"] if not groups or g["group_id"] in groups]
    if not selected or (groups and set(groups) - {g["group_id"] for g in selected}):
        raise ValueError("unknown_acquisition_group")
    before = []
    for group in selected:
        spec = {**group, "as_of": as_of}
        before.append(original_report_coverage(spec, discover_documents(group, as_of)))
    run = PublicAcquisition(root, run_id, as_of, max_documents, max_seconds)
    if source is None:
        from src.core.config_store import ConfigStore
        from src.fundamental_quant.store import ResearchStore

        source = public_report_source(
            ConfigStore().load_runtime(),
            ResearchStore(Path("data/fundamental_quant")),
            run,
        )
    run.run(acquisition_tasks(before), source)
    # Freeze changed evidence separately whenever research exists. No checkpoint
    # migration, history mutation, old-budget restart, or claim republication.
    destination = root
    changed = []
    for group in config["groups"]:
        docs = discover_documents(group, as_of)
        spec = {
            k: group[k] for k in ("group_id", "companies", "industry", "history_years")
        }
        spec["as_of"] = as_of
        if group.get("valuation_store_root"):
            spec["valuation_store_root"] = group["valuation_store_root"]
        if group.get("title"):
            spec["title"] = group["title"]
        inputs, _ = _preparation_inputs(group, spec, docs)
        changed.append({"group_id": group["group_id"], "input_hash": digest(inputs)})
    if any(
        (root / g["group_id"] / "checkpoint.json").exists() for g in config["groups"]
    ):
        destination = root / "revisions" / ("sources-" + digest(changed)[:16])
    result = {
        "acquisition": run.state,
        "coverage_before": before,
        "output_root": str(destination.resolve()),
        "research_migration_required": destination != root,
        "paid_model_calls": 0,
    }
    try:
        result["preparation"] = prepare(destination, config, as_of)
        result["status"] = (
            "prepared_revision_awaiting_research_migration"
            if destination != root
            else "prepared"
        )
        render_index(destination, config)
    except Exception as exc:  # noqa: BLE001 -- persist a visible boundary failure
        result["status"] = "preparation_failed"
        result["error"] = type(exc).__name__ + ":" + str(exc)
    _write(root / "acquisition" / (run_id + ".result.json"), result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "prepare",
            "estimate",
            "acquire",
            "refresh",
            "run",
            "resume",
            "status",
            "render",
        ),
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--as-of", default=datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    )
    parser.add_argument("--authorization", type=Path)
    parser.add_argument(
        "--acquisition-id",
        help="Public-source ledger ID; reuse to resume without resetting its limits",
    )
    parser.add_argument("--max-documents", type=int, default=8)
    parser.add_argument("--max-seconds", type=int, default=900)
    parser.add_argument(
        "--group",
        action="append",
        help="Limit acquisition; offline preparation still verifies the complete config",
    )
    args = parser.parse_args()
    config = read(args.config)
    if args.command == "prepare":
        result = prepare(args.root, config, args.as_of)
    elif args.command == "estimate":
        result = estimate(args.root, config)
    elif args.command in {"acquire", "refresh"}:
        if not args.acquisition_id:
            parser.error(
                "acquire/refresh requires --acquisition-id; limits persist across restarts"
            )
        with exclusive_process_lock(args.root / "run.lock") as acquired:
            if not acquired:
                raise RuntimeError("research_runner_already_active")
            result = acquire(
                args.root,
                config,
                args.as_of,
                run_id=args.acquisition_id,
                max_documents=args.max_documents,
                max_seconds=args.max_seconds,
                groups=args.group,
            )
    elif args.command in {"run", "resume"}:
        if not args.authorization:
            parser.error(
                "run/resume requires --authorization; old rounds are not restarted"
            )
        with exclusive_process_lock(args.root / "run.lock") as acquired:
            if not acquired:
                raise RuntimeError("research_runner_already_active")
            result = run(args.root, config, args.authorization)
    else:
        result = []
        for group in config["groups"]:
            folder = args.root / group["group_id"]
            state = (
                read(folder / "checkpoint.json")
                if (folder / "checkpoint.json").exists()
                else {"status": "prepared"}
            )
            if args.command == "render":
                render_longitudinal_report(
                    read(folder / "packet.json"), state, folder / "index.html"
                )
            result.append(
                {
                    "group_id": group["group_id"],
                    "status": state["status"],
                    "error": state.get("error"),
                    "usage": state.get("usage"),
                    "publication": read(folder / "publication_receipt.json")
                    if (folder / "publication_receipt.json").exists()
                    else None,
                }
            )
    render_index(args.root, config)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
