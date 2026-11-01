"""Application-neutral adapters for legacy frozen streaming request plans."""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path

from . import transport
from .ledger import ExistingAttempt, GatewayLedger, account_id


def perform_authorized(
    folder,
    job,
    request,
    plan,
    key,
    post=None,
    *,
    authority,
    authorization,
    producer,
    logical_intent,
    clock=datetime,
):
    ledger = GatewayLedger(authority)
    budget = plan.get("authorized_usd", plan.get("budget_limit_usd"))
    if budget is None:
        # Direct single-call adapter use is authorized only for this frozen
        # reservation. Multi-phase CLI plans always carry their total cap.
        budget = job["reserved_usd_peak"]
    auth = ledger.authorize(authorization, budget)
    serialized = json.dumps(
        request, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    request_hash = hashlib.sha256(serialized.encode()).hexdigest()
    if not logical_intent:
        raise ValueError("explicit_model_logical_intent_required")
    intent = "frozen_stream:" + logical_intent
    capture = Path(folder) / "jobs" / job["id"]
    try:
        lease = ledger.claim(
            intent,
            authorization=auth,
            producer=producer,
            request_sha256=request_hash,
            account=account_id(plan["endpoint"], key),
            reservation=job["reserved_usd_peak"],
            capture=capture,
        )
    except ExistingAttempt as old:
        source = Path(old.record["capture"])
        receipt = json.loads(old.record["receipt"] or "{}")
        if source == capture:
            raise FileExistsError("immutable_attempt_already_exists") from old
        if not (source / "request.json").is_file():
            raise ValueError("prior_model_attempt_artifact_unavailable") from old
        prior = transport.load(source / "request.json")
        if prior != request:
            raise ValueError("prior_model_attempt_request_changed") from old
        if receipt.get("content_sha256") and (
            not (source / "content.md").is_file()
            or transport.sha(source / "content.md") != receipt["content_sha256"]
        ):
            raise ValueError("prior_model_attempt_content_changed") from old
        capture.mkdir(parents=True, exist_ok=True)
        for item in source.iterdir():
            if item.is_file() and item.name != "receipt.json":
                target = capture / item.name
                if target.exists():
                    if transport.sha(target) != transport.sha(item):
                        raise ValueError("consumer_capture_conflict") from old
                else:
                    shutil.copyfile(item, target)
        for name in ("content.md", "reasoning.txt", "response.raw", "events.jsonl"):
            if not (capture / name).exists():
                transport.save(capture / name, b"")
        reused = {
            **receipt,
            "id": job["id"],
            "status": receipt.get("status", "unknown"),
            "usage": None,
            "historical_usage": receipt.get("usage"),
            "historical_cost": receipt.get("cost"),
            "reserved_usd_peak": "0",
            "cost": {"usd": "0", "kind": "reuse_no_new_model_charge"},
            "reused": True,
            "charged_to_current_run": False,
            "producer_run_id": old.record["producer"],
            "source_capture": str(source),
            "automatic_retries": 0,
        }
        transport.save(capture / "receipt.json", reused)
        return reused
    with lease:
        return transport.perform(
            folder, job, request, plan, key, post=post, lease=lease, clock=clock
        )
