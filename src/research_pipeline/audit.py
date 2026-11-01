"""Read-only delivery accounting; provider usage and local bodies stay separate."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .common import file_sha, now
from .store import Store

TOKEN_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "cache_hit_tokens",
    "cache_miss_tokens",
    "reasoning_tokens",
)


def _integer(value):
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else None
    )


def _usage(receipt):
    usage = receipt.get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    hit = _integer(usage.get("prompt_cache_hit_tokens", details.get("cached_tokens")))
    prompt = _integer(usage.get("prompt_tokens"))
    miss = _integer(usage.get("prompt_cache_miss_tokens"))
    if miss is None and prompt is not None and hit is not None and hit <= prompt:
        miss = prompt - hit
    return {
        "prompt_tokens": prompt,
        "completion_tokens": _integer(usage.get("completion_tokens")),
        "cache_hit_tokens": hit,
        "cache_miss_tokens": miss,
        "reasoning_tokens": _integer(
            completion_details.get("reasoning_tokens", usage.get("reasoning_tokens"))
        ),
    }


def _known_cost(receipt):
    cost = receipt.get("cost") or {}
    if cost.get("kind") == "usage_cache_unknown_upper_bound" or cost.get("usd") is None:
        return None
    try:
        value = Decimal(str(cost["usd"]))
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() and value >= 0 else None


def _issuer(value):
    """Use declared identity only; directory names and bare codes prove nothing."""
    if value.get("issuer_id"):
        return str(value["issuer_id"])
    instrument = value.get("instrument")
    if (
        isinstance(instrument, str)
        and instrument.partition(":")[0] in {"a_share", "hk", "us"}
        and ":" in instrument
    ):
        return instrument
    if value.get("market") in {"a_share", "hk", "us"} and value.get("code"):
        from .models import Instrument

        try:
            return Instrument.parse(
                str(value["market"]) + ":" + str(value["code"])
            ).issuer
        except ValueError:
            return None
    return None


def _documents(value):
    result = []
    for source in (value, value.get("metadata") or {}):
        if isinstance(source.get("document"), dict):
            result.append(source["document"])
        if isinstance(source.get("documents"), list):
            result.extend(source["documents"])
    return result


def _company(record):
    receipt = record.get("receipt") or {}
    declared = record.get("company") or receipt.get("company")
    if declared and ":" in str(declared):
        return str(declared)
    documents = _documents(record)
    issuers = {
        _issuer(document) for document in documents if isinstance(document, dict)
    }
    if documents and len(issuers) == 1 and None not in issuers:
        return next(iter(issuers))
    return _issuer(record) or (
        str(declared) if declared and not str(declared).isdigit() else "shared"
    )


def _phase(record):
    receipt = record.get("receipt") or {}
    return (
        record.get("phase")
        or receipt.get("phase")
        or record.get("stage")
        or ("extract:" + str(record["block"]) if record.get("block") else "unknown")
    )


def _attempt(record):
    receipt = record.get("receipt") or {}
    sent = bool(record.get("sent_intent"))
    cost = _known_cost(receipt) if sent else None
    return {
        "task_key": record["key"],
        "producer_run_id": record.get("producer_run"),
        "capture_dir": record.get("capture_dir"),
        "company": _company(record),
        "phase": _phase(record),
        "status": record.get("status"),
        "sent": sent,
        "historical": bool(record.get("historical")),
        "simulated": receipt.get("simulated"),
        "usage": _usage(receipt) if sent else dict.fromkeys(TOKEN_FIELDS),
        "known_usage_usd": str(cost) if cost is not None else None,
        "usage_unknown": sent and not bool(receipt.get("usage")),
        "cost_unknown": sent and cost is None,
        "reason": receipt.get("reason") or receipt.get("error_type"),
    }


def _totals(attempts):
    sent = [item for item in attempts if item["sent"]]
    result = {
        "attempt_count": len(attempts),
        "sent_count": len(sent),
        "not_sent_count": len(attempts) - len(sent),
        "simulated_sent_count": sum(bool(item.get("simulated")) for item in sent),
        "active_attempt_count": sum(
            item["status"] in {"queued", "sending"} for item in attempts
        ),
        "known_usage_usd": str(
            sum(
                (
                    Decimal(item["known_usage_usd"])
                    for item in sent
                    if item["known_usage_usd"] is not None
                ),
                Decimal(0),
            )
        ),
        "unknown_usage_attempts": sum(item["usage_unknown"] for item in sent),
        "unknown_cost_attempts": sum(item["cost_unknown"] for item in sent),
        "tokens": {},
        "completion_includes_reasoning": True,
        "is_provider_invoice": False,
    }
    for field in TOKEN_FIELDS:
        values = [item["usage"][field] for item in sent]
        result["tokens"][field] = {
            "known_subtotal": sum(value for value in values if value is not None),
            "unknown_attempts": sum(value is None for value in values),
        }
    # Reasoning is already in completion, so it is never added to this total.
    result["total_tokens_known_subtotal"] = sum(
        result["tokens"][field]["known_subtotal"]
        for field in ("prompt_tokens", "completion_tokens")
    )
    return result


def _all_events(store, run_id):
    events, after = [], 0
    while True:
        page = store.events(run_id, after=after, limit=500)
        if not page:
            return events
        events.extend(page)
        after = page[-1]["seq"]


def _concurrency(attempts, events):
    sent_keys = {item["task_key"] for item in attempts if item["sent"]}
    if not sent_keys:
        return {
            "maximum_actual_concurrency": 0,
            "method": "no_new_sent_attempts",
            "complete_evidence": True,
        }
    starts, ends = defaultdict(list), defaultdict(list)
    for event in events:
        key = event.get("task_key")
        if key not in sent_keys:
            continue
        if event.get("event") == "provider_post_started":
            starts[key].append(event)
        elif event.get("event") in {"model_finished", "model_error", "call_settled"}:
            ends[key].append(event)
    intervals, missing = [], []
    for key in sorted(sent_keys):
        if len(starts[key]) != 1 or len(ends[key]) != 1:
            missing.append(key)
            continue
        try:
            start = datetime.fromisoformat(starts[key][0]["at"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(ends[key][0]["at"].replace("Z", "+00:00"))
            if start.tzinfo is None or end.tzinfo is None or end < start:
                raise ValueError("unreliable_interval")
        except (TypeError, ValueError, KeyError):
            missing.append(key)
            continue
        intervals.extend(
            ((start, starts[key][0]["seq"], 1), (end, ends[key][0]["seq"], -1))
        )
    active, peak = 0, 0
    for _, _, delta in sorted(intervals):
        active += delta
        peak = max(peak, active)
    return {
        "maximum_actual_concurrency": None if missing else peak,
        "observed_complete_intervals_peak": peak,
        "complete_evidence": not missing,
        "missing_interval_task_keys": missing,
        "method": "persisted_provider_invocation_to_settlement_events",
        "limitation": "Client invocation intervals include transport and local settlement; server-side execution overlap is unavailable.",
    }


def _references(value, keys, captures):
    if isinstance(value, dict):
        for name, child in value.items():
            if name in {
                "task_key",
                "attempt_key",
                "key",
                "artifact_key",
            } and isinstance(child, str):
                keys.add(child)
            elif name == "capture_dir" and isinstance(child, str):
                captures.add(child)
            elif name == "part_attempt_keys" and isinstance(child, list):
                keys.update(item for item in child if isinstance(item, str))
            elif name in {"producer_captures", "attempt_captures"} and isinstance(
                child, list
            ):
                captures.update(item for item in child if isinstance(item, str))
            _references(child, keys, captures)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _references(child, keys, captures)


def _local_bodies(artifacts):
    bodies, seen = [], set()
    for artifact in artifacts:
        path = Path(artifact["path"])
        metadata = artifact.get("metadata") or {}
        digest = file_sha(path) if path.is_file() else None
        identity = digest or str(path.resolve())
        if identity in seen:
            continue
        seen.add(identity)
        bodies.append(
            {
                "stage": artifact.get("stage", artifact.get("kind")),
                "company": _company(artifact),
                "path": str(path),
                "sha256": digest,
                "tokens_local": _integer(
                    metadata.get("tokens_local", metadata.get("body_tokens"))
                ),
                "failure_appendix_tokens_local": _integer(
                    metadata.get("failure_appendix_tokens_local")
                ),
                "basis": "artifact_tokenizer_metadata; not API usage",
            }
        )
    return bodies


def _deduplicate_captures(records):
    """Aliases of the same immutable capture are one actual provider attempt."""
    result = {}
    for record in records:
        capture = record.get("capture_dir")
        identity = (
            ("capture", str(Path(capture).resolve()))
            if capture
            else ("key", record["key"])
        )
        result.setdefault(identity, record)
    return list(result.values())


def _digest(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdefABCDEF" for char in value)
    )


def _annual_scope(record):
    """Prove every declared input by issuer, fiscal year and exact source SHA."""
    metadata = record.get("metadata") or {}
    if record.get("scope_unresolved") or metadata.get("scope_unresolved"):
        return None
    scope = set()
    documents = _documents(record)
    for document in documents:
        if not isinstance(document, dict):
            return None
        issuer, year, digest = (
            _issuer(document),
            str(document.get("fiscal_year", "")),
            document.get("sha256"),
        )
        if not issuer or not year.isdigit() or not _digest(digest):
            return None
        scope.add((issuer, year, digest.lower()))
    versions = record.get("versions", metadata.get("versions"))
    if versions is not None:
        if not isinstance(versions, dict) or not versions:
            return None
        owner = metadata.get("issuer") or _issuer(record)
        doc_owners = {item[0] for item in scope}
        if not owner and len(doc_owners) == 1:
            owner = next(iter(doc_owners))
        if not owner and ":" in str(record.get("company", "")):
            owner = record["company"]
        groups = (
            {owner: versions}
            if all(str(year).isdigit() for year in versions)
            else versions
        )
        for issuer, years in groups.items():
            if not issuer or not isinstance(years, dict) or not years:
                return None
            for year, digests in years.items():
                digests = [digests] if isinstance(digests, str) else digests
                if (
                    not str(year).isdigit()
                    or not isinstance(digests, list)
                    or not digests
                    or not all(_digest(digest) for digest in digests)
                ):
                    return None
                scope.update(
                    (str(issuer), str(year), digest.lower()) for digest in digests
                )
    return scope or None


def _grouped(attempts):
    groups = defaultdict(list)
    for attempt in attempts:
        groups[(attempt["company"], attempt["phase"])].append(attempt)
    return [
        {"company": company, "phase": phase, **_totals(items)}
        for (company, phase), items in sorted(groups.items())
    ]


def audit_run(store: Store, run_id: str, artifacts: list[dict]) -> dict:
    """Attribute new producer receipts once, retaining unknown and inherited evidence."""
    store.get_run(run_id)
    with store.connection() as db:
        records = [
            store._attempt(row)
            for row in db.execute(
                "SELECT * FROM research_attempts ORDER BY rowid"
            ).fetchall()
        ]
    # Stable task key is the durable attempt identity; repeated delivery copies,
    # reuse events and receipt views cannot create extra charged requests.
    records = list({record["key"]: record for record in records}.values())
    current = [
        _attempt(record)
        for record in _deduplicate_captures(
            [
                record
                for record in records
                if record.get("producer_run") == run_id and not record.get("historical")
            ]
        )
    ]
    events = _all_events(store, run_id)
    keys, captures = set(), set()
    _references(artifacts, keys, captures)
    used_artifacts = list(artifacts)
    pending, inspected = set(keys), set()
    while pending:
        key = pending.pop()
        if key in inspected:
            continue
        inspected.add(key)
        cached = store.get_artifact(key)
        if cached:
            used_artifacts.append(cached)
            _references(cached, keys, captures)
            pending.update(keys - inspected)
    used_scope = set()
    for artifact in used_artifacts:
        used_scope.update(_annual_scope(artifact) or ())
    for event in events:
        if event.get("event") == "attempt_reused" and event.get("task_key"):
            keys.add(event["task_key"])
    inherited_records, inclusion = [], {}
    for record in records:
        if not (record.get("historical") or record.get("producer_run") != run_id):
            continue
        direct = record["key"] in keys or record.get("capture_dir") in captures
        scope = _annual_scope(record) if record.get("historical") else None
        if direct or scope and scope.issubset(used_scope):
            inherited_records.append(record)
            inclusion[record["key"]] = (
                "explicit_attempt_or_capture_reference"
                if direct
                else "declared_annual_inputs_subset_of_delivered_source_scope"
            )
    inherited = [
        {**_attempt(record), "inclusion_basis": inclusion[record["key"]]}
        for record in _deduplicate_captures(inherited_records)
    ]
    persisted_keys = {item["task_key"] for item in current}
    blocked = {}
    for event in events:
        if (
            event.get("event") == "dispatch_blocked"
            and event.get("task_key") not in persisted_keys
        ):
            identity = (event.get("task_key"), event.get("reason"))
            blocked[identity] = {
                "task_key": identity[0],
                "reason": identity[1],
                "at": event["at"],
            }
    return {
        "run_id": run_id,
        "generated_at": now(),
        "new_producer_attempts": current,
        "totals": _totals(current),
        "blocked_admissions_without_provider_attempt": list(blocked.values()),
        "by_company_phase": _grouped(current),
        "inherited_or_reused_attempts_not_charged_to_this_run": inherited,
        "inherited_by_company_phase_not_current_spend": _grouped(inherited),
        "inherited_totals_not_current_spend": _totals(inherited),
        "inherited_history_scope_note": "Declared annual input scope proves related historical work, not that every old output was consumed. Historical totals are never current authorization spend.",
        "inherited_known_usage_usd_not_current_spend": _totals(inherited)[
            "known_usage_usd"
        ],
        "budget_guard": store.budget(run_id),
        "local_bodies_not_api_usage": _local_bodies(artifacts),
        "concurrency": _concurrency(current, events),
        "account_balance_decrease_is_per_run_invoice": False,
        "unknown_liability_or_inflight_reservation_is_spend": False,
    }


def hash_manifest(paths, root: str | Path | None = None, *, exclude=()) -> list[dict]:
    """Hash explicit public delivery paths, never follow an escaping symlink."""
    if isinstance(paths, (str, Path)):
        paths = [paths]
    base = Path(root).resolve() if root else None
    excluded = {str(Path(path).resolve()) for path in exclude}
    files = {}
    for item in paths:
        item = Path(item)
        candidates = item.rglob("*") if item.is_dir() else [item]
        for path in candidates:
            if not path.is_file() or path.is_symlink():
                continue
            resolved = path.resolve()
            if str(resolved) in excluded or (
                base and not resolved.is_relative_to(base)
            ):
                continue
            if resolved.name.lower() in {".env", "id_rsa", "id_ed25519"}:
                continue
            files[str(resolved)] = {
                "path": str(resolved.relative_to(base)) if base else str(resolved),
                "bytes": resolved.stat().st_size,
                "sha256": file_sha(resolved),
            }
    return [files[key] for key in sorted(files)]
