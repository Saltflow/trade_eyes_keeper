"""Hash-bound nonfinancial cover review; incomplete body pages remain blocked."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path

from .contracts import envelope, fingerprint, issue
from .page_evidence import (
    MAX_DOCUMENT_PAGES,
    MAX_PDF_BYTES,
    PAGE_EVIDENCE_VERSION,
    PAGE_PROMPT_VERSION,
    PAGE_REVIEW_PROMPT,
    PAGE_TRANSCRIBE_PROMPT,
    _normalized,
    _validate_reading,
    review_cover_page,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ARCHIVE_ROOT = PROJECT_ROOT / "data" / "dataset_documents"


def _failure(packet, code, source_id="", **data):
    return envelope(
        "partial",
        {"status": "blocked", "source_id": source_id, **data},
        packet.get("as_of", ""),
        [issue(code, source_id, stage="evidence", company=packet["company"]["id"])],
    )


def _artifact_matches(path, digest):
    try:
        if not path.is_file() or path.stat().st_size > MAX_PDF_BYTES:
            return False
        result, count = hashlib.sha256(), 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                count += len(chunk)
                if count > MAX_PDF_BYTES:
                    return False
                result.update(chunk)
        return result.hexdigest() == digest
    except OSError:
        return False


def _group_error(packet, pages):
    """Every page must belong to one complete, consistently classified report."""
    from .sources import DOCUMENT_PARSER_VERSION

    cover = next((s for s in pages if s.get("page") == 1), {})
    if not cover:
        return "cover_source_missing"
    count = cover.get("document_pages")
    if type(count) is not int or not 1 <= count <= MAX_DOCUMENT_PAGES:
        return "cover_document_page_count_invalid"
    if (
        len(pages) != count
        or any(type(s.get("page")) is not int for s in pages)
        or {s["page"] for s in pages} != set(range(1, count + 1))
    ):
        return "cover_document_source_pages_missing_or_duplicate"
    if (
        cover.get("document_kind") not in {"annual", "interim"}
        or cover.get("parser_version") != DOCUMENT_PARSER_VERSION
        or not isinstance(cover.get("origin_id"), str)
        or not cover["origin_id"]
    ):
        return "cover_document_provenance_invalid"
    for source in pages:
        if (
            source.get("issuer_id") != packet["company"]["id"]
            or source.get("kind") != "statement"
            or source.get("document_status") != "incomplete_text"
            or source.get("incomplete_pages") != [1]
            or source.get("page_status")
            != ("incomplete_text" if source["page"] == 1 else "complete_text")
            or any(
                source.get(k) != cover.get(k)
                for k in (
                    "document_sha256",
                    "document_pages",
                    "document_path",
                    "document_title",
                    "document_kind",
                    "period_end",
                    "published_at",
                    "origin_id",
                    "parser_version",
                )
            )
        ):
            return "cover_document_group_conflict"
        if (
            not isinstance(source.get("id"), str)
            or not source["id"]
            or not isinstance(source.get("text"), str)
            or not source["text"].strip()
            or hashlib.sha256(source["text"].encode("utf-8")).hexdigest()
            != source.get("hash")
        ):
            return "cover_document_source_hash_invalid"
    if len({s["id"] for s in pages}) != len(pages):
        return "cover_document_source_id_duplicate"
    return None


def _record_matches(result, page):
    record = result.get("data") or {}
    if (
        result.get("status") != "success"
        or result.get("errors")
        or record.get("status") != "excluded_nonfinancial_cover"
        or record.get("assessment") != "reviewed_nonfinancial_cover"
        or record.get("version") != PAGE_EVIDENCE_VERSION
        or any(
            record.get(k) != page[k]
            for k in ("source_id", "issuer_id", "document_sha256", "page", "period_end")
        )
        or record.get("original_text") != page["raw_text"]
        or record.get("original_text_sha256")
        != hashlib.sha256(page["raw_text"].encode()).hexdigest()
        or not re.fullmatch(
            r"[a-f0-9]{64}", str(record.get("rendered_image_sha256", ""))
        )
        or not isinstance(record.get("limitations"), list)
        or not record["limitations"]
    ):
        return False
    try:
        readings = record["readings"]
        if not isinstance(readings, list) or [r.get("role") for r in readings] != [
            "page_transcribe",
            "page_review",
        ]:
            return False
        first, second = [_validate_reading(r["output"]) for r in readings]
        if any(
            _normalized(first[k]) != _normalized(second[k])
            for k in ("issuer", "title", "report_year", "transcription")
        ):
            return False
        text = _normalized(first["transcription"])
        return (
            _normalized(page["issuer_name"]) in _normalized(first["issuer"])
            and _normalized(page["report_title"]) == _normalized(first["title"])
            and first["report_year"] == page["period_end"][:4]
            and all(
                _normalized(first[k]) in text
                for k in ("issuer", "title", "report_year")
            )
            and all(
                _normalized(line) in text
                for line in page["raw_text"].splitlines()
                if line.strip()
            )
            and record.get("transcription") == first["transcription"]
            and record.get("transcription_sha256")
            == hashlib.sha256(first["transcription"].encode()).hexdigest()
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def _cover_identity(source):
    """Immutable cover provenance, independent of body extraction and valuation."""
    return {
        key: source.get(key)
        for key in (
            "id",
            "issuer_id",
            "hash",
            "text",
            "page",
            "document_sha256",
            "document_pages",
            "document_title",
            "document_kind",
            "document_path",
            "period_end",
            "published_at",
            "origin_id",
        )
    }


def _legacy_cover_receipt(service, issuer, cover, page, events):
    """Reuse only auditable successful readings of this exact original cover.

    Current body coverage and the PDF bytes are checked by the caller. An old
    opaque cache key alone proves neither provenance nor the model/prompt used.
    Missing ledger history is a cache miss, never permission to waive review.
    """
    identity = _cover_identity(cover)
    matching = [
        event
        for event in events
        if event.get("payload", {}).get("result", {}).get("data", {}).get("source_id")
        == page["source_id"]
    ]
    if not matching:
        return None
    event = matching[-1]  # Never resurrect an older receipt over a later failure.
    payload = event["payload"]
    result = payload.get("result", {})
    if not _record_matches(result, page):
        return None
    if "cover_identity" in payload and payload["cover_identity"] != identity:
        return None
    if "cover_identity" not in payload:
        originals = service.store.events(kind="tool_result", company_id=issuer)
        if not any(
            old["id"] < event["id"]
            and any(
                _cover_identity(source) == identity
                for source in old.get("payload", {}).get("data", {}).get("sources", [])
                if isinstance(source, dict)
            )
            for old in originals
        ):
            return None
    requests = service.store.events(kind="model_request", company_id=issuer)
    responses = service.store.events(kind="model_response", company_id=issuer)
    model = service.settings.get("llm", {}).get("vision_model", "deepseek-flash")
    record = result["data"]
    prompts = {
        "page_transcribe": PAGE_TRANSCRIBE_PROMPT,
        "page_review": PAGE_REVIEW_PROMPT,
    }
    for reading in record["readings"]:
        proven = False
        for request_event in requests:
            request = request_event["payload"]
            if (
                request_event["id"] >= event["id"]
                or request.get("role") != reading["role"]
                or request.get("requested_model") != model
                or request.get("prompt_version") != PAGE_PROMPT_VERSION
            ):
                continue
            try:
                messages = request["messages"]
                if messages[0] != {
                    "role": "system",
                    "content": f"{PAGE_PROMPT_VERSION}\n{prompts[reading['role']]}",
                }:
                    continue
                contents = messages[1]["content"]
                metadata = json.loads(contents[0]["text"])
                if metadata["issuer_id"] != issuer or metadata["page_evidence"] != {
                    "version": PAGE_EVIDENCE_VERSION,
                    "source_id": page["source_id"],
                    "document_sha256": page["document_sha256"],
                    "page": 1,
                    "rendered_image_sha256": record["rendered_image_sha256"],
                    "full_page": True,
                }:
                    continue
                encoded = contents[1]["image_url"]
                prefix = "data:image/png;base64,"
                if encoded.get("detail") != "original" or not encoded["url"].startswith(
                    prefix
                ):
                    continue
                pixels = base64.b64decode(encoded["url"][len(prefix) :], validate=True)
                if (
                    hashlib.sha256(pixels).hexdigest()
                    != record["rendered_image_sha256"]
                ):
                    continue
            except (KeyError, IndexError, TypeError, ValueError, AttributeError):
                continue
            proven = any(
                request_event["id"] < response["id"] < event["id"]
                and response["payload"].get("request_hash")
                == request.get("request_hash")
                and response["payload"].get("role") == reading["role"]
                and response["payload"].get("content") == reading["output"]
                and isinstance(response["payload"].get("model"), str)
                and bool(response["payload"]["model"])
                for response in responses
            )
            if proven:
                break
        if not proven:
            return None
    return payload["key"], result


def resolve_covers(service, packet: dict) -> None:
    """Use shared persistent budgets; missing/financial body pages remain blocked."""
    issuer = packet["company"]["id"]
    groups = {}
    for source in packet.get("sources", []):
        if not isinstance(source, dict):
            continue
        if source.get("document_status") == "text_with_reviewed_nonfinancial_cover":
            # Incoming promotion is not proof; recheck its PDF and persisted ledger.
            source["document_status"] = "incomplete_text"
            source["kind"] = "statement"
            source.pop("page_review_ref", None)
            source.pop("coverage_limitations", None)
        if isinstance(source.get("document_sha256"), str):
            groups.setdefault(source["document_sha256"], []).append(source)
    cached = {}
    cover_events = service.store.events(kind="cover_review", company_id=issuer)
    for event in cover_events:
        payload = event.get("payload", {})
        result = payload.get("result")
        if not isinstance(result, dict) or not isinstance(payload.get("key"), str):
            continue
        if (
            payload.get("budget_day") != service.budget_day.isoformat()
            or service.acceptance.enabled
        ) and any(
            x.get("code") == "daily_model_budget_exhausted"
            for x in result.get("errors", [])
            if isinstance(x, dict)
        ):
            continue
        cached[payload["key"]] = result
    ledgers = []
    for digest, pages in groups.items():
        if not any(s.get("document_status") == "incomplete_text" for s in pages):
            continue
        cover = next((s for s in pages if s.get("page") == 1), {})
        source_id = cover.get("id", "")
        problem = _group_error(packet, pages)
        if problem:
            failed = _failure(packet, problem, source_id, document_sha256=digest)
            ledgers.append({**failed["data"], "errors": failed["errors"]})
            continue
        title = re.search(
            r"20\d{2}年?(?:半年度|年度|中期)报告", str(cover.get("document_title", ""))
        )
        path = (PROJECT_ROOT / str(cover.get("document_path", ""))).resolve()
        if (
            not title
            or not path.is_relative_to(ARCHIVE_ROOT.resolve())
            or not _artifact_matches(path, digest)
        ):
            failed = _failure(
                packet,
                "cover_original_artifact_invalid",
                source_id,
                document_sha256=digest,
            )
            ledgers.append({**failed["data"], "errors": failed["errors"]})
            continue
        raw_text = cover["text"]
        legal = re.search(r"[^\s\d]+(?:股份有限公司|有限公司)", raw_text)
        page = {
            "source_id": source_id,
            "issuer_id": issuer,
            "issuer_name": legal[0] if legal else packet["company"].get("name", ""),
            "report_title": title[0],
            "period_end": cover["period_end"],
            "document_sha256": digest,
            "pdf_path": str(path),
            "page": 1,
            "raw_text": raw_text,
        }
        key = fingerprint(
            {
                "page": page,
                "version": PAGE_EVIDENCE_VERSION,
                "coverage": [
                    {
                        k: s.get(k)
                        for k in (
                            "id",
                            "page",
                            "hash",
                            "document_pages",
                            "document_kind",
                            "page_status",
                            "published_at",
                            "origin_id",
                            "parser_version",
                        )
                    }
                    for s in sorted(pages, key=lambda s: s["page"])
                ],
                "vision_model": service.settings.get("llm", {}).get(
                    "vision_model", "deepseek-flash"
                ),
                "rules": service._versions(),
                "request_limit": service._limit("max_request_chars", 450000),
            }
        )
        result = cached.get(key)
        if result is None:
            previous = _legacy_cover_receipt(service, issuer, cover, page, cover_events)
            if previous:
                previous_key, result = previous
                service._event(
                    issuer,
                    "cover_review",
                    {
                        "key": key,
                        "result": result,
                        "budget_day": service.budget_day.isoformat(),
                        "cover_identity": _cover_identity(cover),
                        "reused_from": previous_key,
                    },
                )
        if result is None:
            reserved = (
                service.acceptance.reserve_operation(
                    service.budget_day.isoformat(),
                    "cover_review_documents",
                    key,
                    service.acceptance.state()["config"]["max_model_calls"],
                )
                if service.acceptance.enabled
                else service.budget_store.reserve(
                    service.budget_day.isoformat(),
                    "cover_review_documents",
                    service._limit("daily_cover_review_document_limit", 10),
                    key=key,
                )
            )
            if not reserved:
                result = _failure(
                    packet,
                    "daily_cover_review_budget_exhausted_or_reserved",
                    source_id,
                    document_sha256=digest,
                )
            else:
                result = review_cover_page(packet, page, service._call)
                service._event(
                    issuer,
                    "cover_review",
                    {
                        "key": key,
                        "result": result,
                        "budget_day": service.budget_day.isoformat(),
                        "cover_identity": _cover_identity(cover),
                    },
                )
        record = result.get("data") or {}
        ledger = {**record, "id": key, "errors": result.get("errors", [])}
        ledgers.append(ledger)
        if result.get("status") != "success":
            continue
        if not _record_matches(result, page) or not _artifact_matches(path, digest):
            ledger.update(status="blocked", assessment="blocked")
            ledger["errors"] = _failure(
                packet, "cover_cached_assessment_invalid", source_id
            )["errors"]
            continue
        for source in pages:
            source["raw_document_status"] = source["document_status"]
            source["document_status"] = "text_with_reviewed_nonfinancial_cover"
            source["page_review_ref"] = key
            source["coverage_limitations"] = record["limitations"]
            # The cover remains a statement, never a financial-fact source.
            if source["page"] != 1:
                source["kind"] = source["document_kind"]
    packet["page_reviews"] = ledgers
