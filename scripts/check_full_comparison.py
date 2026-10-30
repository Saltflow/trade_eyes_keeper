"""Offline evidence audit; mechanical readiness never certifies financial truth."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

KEYS = {
    "normalized_earnings",
    "capital_efficiency",
    "normalized_cashflow",
    "implied_return",
    "peer_thesis",
    "thesis_breaker",
}
STATUSES = {"supported", "negative", "insufficient", "conflict", "not_applicable"}
REF = re.compile(r"\b(D\d{2,})\s+p\s*(\d+)(?:\s*[–—-]\s*(\d+))?", re.IGNORECASE)


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def stable_hash(value):
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)


def original_pages(text):
    parts = re.split(r"--- PAGE (\d+) ---", text)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}


def source_payload(doc, pages):
    return (
        json.dumps(doc, ensure_ascii=False)
        + "\n"
        + "\n".join(f"[{doc['short_id']} p{p}]\n{text}" for p, text in pages.items())
    )


def page_number(value):
    match = re.fullmatch(r"\s*[pP]?\s*(\d+)\s*", str(value))
    return int(match.group(1)) if match else None


def report_in_request(request):
    for message in request.get("messages", []):
        if message.get("role") != "user":
            continue
        try:
            content = json.loads(message.get("content", ""))
        except (ValueError, TypeError):
            continue
        if isinstance(content, dict) and isinstance(content.get("report"), dict):
            return content["report"]
    return None


def audit(root, *, pdf_page_counter=None):
    root = Path(root)
    result = {
        "mechanical_ready": False,
        "user_acceptance_complete": False,
        "errors": [],
        "warnings": [],
        "documents": [],
        "review_bindings": [],
        "image_requirements": [],
        "rejected_facts_in_synthesis": [],
        "human_review_required": [
            "Numeric token presence does not verify table columns, units, period or scope.",
            "Input delivery and model coverage declarations do not prove comprehension.",
            "Review bank/finance-company customer deposits, restricted cash and cash bridges.",
            "Review minority claims, capex classification, normalization and valuation assumptions.",
            "Parent source/image review and final artifact acceptance are still required.",
        ],
    }
    errors = result["errors"]

    def problem(code, detail):
        errors.append({"code": code, "detail": detail})

    def load(path, *, required=True):
        try:
            return read(path)
        except (OSError, ValueError) as exc:
            if required:
                problem("missing_or_invalid_json", f"{path.name}: {type(exc).__name__}")
            return None

    manifest = load(root / "manifest.json")
    if not isinstance(manifest, list) or not manifest:
        problem("invalid_manifest", "Expected a nonempty document list")
        return result
    docs = {d["short_id"]: d for d in manifest}
    if len(docs) != len(manifest):
        problem("duplicate_document_ids", "Manifest short IDs must be unique")
    replay = load(root / "replay.json", required=False) or {}
    review_mapping_path = root / "review_call_mapping.json"
    review_mapping = load(review_mapping_path) if review_mapping_path.exists() else {}
    if not isinstance(review_mapping, dict):
        problem(
            "invalid_review_call_mapping", "Expected logical-key to call-ID mapping"
        )
        review_mapping = {}
    image_checks = load(root / "image_checks.json", required=False) or []
    if isinstance(image_checks, dict):
        image_checks = image_checks.get("checks", [])

    def refs(value, label):
        count = 0
        for text in strings(value):
            for match in REF.finditer(text):
                sid, start, end = match.groups()
                sid = sid.upper()
                start, end = int(start), int(end or start)
                if sid not in docs or not 1 <= start <= end <= docs[sid]["page_count"]:
                    problem("invalid_source_reference", f"{label}: {match.group(0)}")
                count += 1
        return count

    for doc in manifest:
        sid = doc["short_id"]
        entry = {
            "document_id": sid,
            "expected_pages": doc["page_count"],
            "full_text_delivered": False,
            "model_declared_coverage": None,
        }
        result["documents"].append(entry)
        pdf = Path(doc["path"])
        try:
            if hashlib.sha256(pdf.read_bytes()).hexdigest() != doc["sha256"]:
                problem("pdf_hash_mismatch", sid)
            if pdf_page_counter is None:
                import pypdfium2

                with pypdfium2.PdfDocument(str(pdf)) as document:
                    actual_pages = len(document)
            else:
                actual_pages = pdf_page_counter(pdf)
            if actual_pages != doc["page_count"]:
                problem("pdf_page_count_mismatch", sid)
            entry["actual_pdf_pages"] = actual_pages
            text = pdf.with_name("document.txt").read_text(encoding="utf-8")
            if hashlib.sha256(text.encode()).hexdigest() != doc["text_sha256"]:
                problem("text_hash_mismatch", sid)
            source = load(root / "sources" / f"{sid}.json") or {}
            pages = source.get("pages", {})
            entry["frozen_text_pages"] = len(pages)
            if source.get("metadata") != doc:
                problem("source_metadata_mismatch", sid)
            if set(pages) != {str(i) for i in range(1, actual_pages + 1)}:
                problem("source_pages_missing", sid)
            if pages != original_pages(text):
                problem("source_pages_differ_from_frozen_text", sid)
        except (OSError, ValueError, RuntimeError) as exc:
            problem("source_unreadable", f"{sid}: {type(exc).__name__}")
            pages = {}
        note = load(root / "notes" / f"{sid}.json")
        if not isinstance(note, dict):
            continue
        declared_company = str(note.get("company", ""))
        legal_name = re.sub(r"[（(].*?[）)]", "", declared_company).strip()
        cover = re.sub(r"\s+", "", str(pages.get("1", "")))
        cover_supported_legal_name = legal_name.endswith("公司") and legal_name in cover
        if (
            declared_company not in {doc["company_name"], doc["issuer_id"]}
            and not cover_supported_legal_name
        ):
            problem("note_company_mismatch", sid)
        if note.get("document_id") not in {sid, doc["document_id"]}:
            problem("note_document_mismatch", sid)
        entry["model_declared_coverage"] = note.get("coverage")
        if not note.get("coverage"):
            problem("missing_coverage_declaration", sid)
        call_id = replay.get(f"read_{sid}", f"read_{sid}")
        call = root / "calls" / call_id
        request = load(call / "request.json") or {}
        call_state = load(call / "state.json") or {}
        call_result = load(call / "result.json") or {}
        entry["request_hash"] = call_state.get("request_hash")
        entry["full_text_delivered"] = any(
            m.get("role") == "user" and m.get("content") == source_payload(doc, pages)
            for m in request.get("messages", [])
        )
        if not entry["full_text_delivered"]:
            problem("complete_source_not_in_request", sid)
        if call_state.get("status") != "completed":
            problem("reading_call_not_completed", sid)
        if call_result.get("content") != note:
            problem("note_not_bound_to_paid_result", sid)
        refs(note, f"note:{sid}")
        for requirement in note.get("image_checks_needed", []):
            page = page_number(requirement.get("page"))
            matches = [
                c
                for c in image_checks
                if c.get("document_id") in {sid, doc["document_id"]}
                and page_number(c.get("page")) == page
            ]
            verified = False
            not_relied_upon = False
            for check in matches:
                image_path = Path(check.get("image_path", ""))
                if not image_path.is_absolute():
                    image_path = root / image_path
                verified = verified or bool(
                    check.get("status") == "verified"
                    and check.get("source_sha256") == doc["sha256"]
                    and check.get("reviewer")
                    and check.get("findings")
                    and image_path.is_file()
                )
                not_relied_upon = not_relied_upon or bool(
                    check.get("status") == "not_relied_upon"
                    and check.get("rationale")
                    and check.get("report_section")
                    and check.get("source_sha256") == doc["sha256"]
                )
            result["image_requirements"].append(
                {
                    "document_id": sid,
                    "page": page,
                    "reason": requirement.get("reason"),
                    "recorded_verified": verified,
                    "recorded_not_relied_upon": not_relied_upon,
                    "pending_human_judgment": not (verified or not_relied_upon),
                }
            )
            if not (verified or not_relied_upon):
                result["warnings"].append(
                    f"Pending image request {sid} p{page}; assess whether decisive, "
                    "rather than requiring every suggested spot check"
                )

    report = load(root / "report.json")
    if not isinstance(report, dict):
        problem("report_missing", "No final report object")
        return result
    result["report_sha256"] = stable_hash(report)
    conclusions = report.get("conclusions", [])
    if not isinstance(conclusions, list) or any(
        not isinstance(c, dict) for c in conclusions
    ):
        conclusions = []
    if len(conclusions) != 6 or {c.get("key") for c in conclusions} != KEYS:
        problem("six_conclusion_keys_required", "Six unique expected keys required")
    for conclusion in conclusions:
        if conclusion.get("status") not in STATUSES:
            problem("invalid_conclusion_status", conclusion.get("key"))
        if not conclusion.get("basis") or not conclusion.get("refs"):
            problem("conclusion_evidence_missing", conclusion.get("key"))
    if refs(report, "report") == 0:
        problem("report_without_source_references", "No recognized document/page refs")
    if not report.get("sections") or not report.get("executive_summary"):
        problem("report_content_incomplete", "Sections and executive summary required")

    synthesis = load(root / "calls" / "synthesis" / "request.json") or {}
    synthesis_strings = list(strings(synthesis))
    for message in synthesis.get("messages", []):
        try:
            synthesis_strings.extend(strings(json.loads(message.get("content", ""))))
        except (ValueError, TypeError):
            pass
    synthesis_text = "\n".join(synthesis_strings)
    for doc in manifest:
        checked = load(root / "facts" / f"{doc['short_id']}.json", required=False) or {}
        for item in checked.get("rejected", []):
            fact = item.get("fact", {})
            quoted = fact.get("quote")
            if quoted and quoted in synthesis_text:
                result["rejected_facts_in_synthesis"].append(
                    {
                        "document_id": doc["short_id"],
                        "metric": fact.get("metric"),
                        "error": item.get("error"),
                        "quote": quoted,
                        "meaning": "Rejected fact quote remains in synthesis input; semantic review required",
                    }
                )
    if result["rejected_facts_in_synthesis"]:
        result["warnings"].append(
            "Rejected facts were not fully excluded from synthesis input"
        )

    issuers = list(dict.fromkeys(d["issuer_id"] for d in manifest))
    reviews = [("review_" + issuer.split(":")[-1], issuer) for issuer in issuers]
    reviews.append(("review_summary", "all"))
    for logical_id, issuer in reviews:
        call_id = review_mapping.get(logical_id, logical_id)
        if not isinstance(call_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", call_id
        ):
            problem("invalid_review_call_id", logical_id)
            continue
        artifact = root / ("review.json" if issuer == "all" else logical_id + ".json")
        review = load(artifact)
        request = load(root / "calls" / call_id / "request.json") or {}
        call_result = load(root / "calls" / call_id / "result.json") or {}
        state = load(root / "calls" / call_id / "state.json") or {}
        requested_report = report_in_request(request)
        binding = {
            "logical_review_id": logical_id,
            "call_id": call_id,
            "report_sha256": stable_hash(requested_report),
            "current_report_sha256": result["report_sha256"],
            "request_hash": state.get("request_hash"),
            "request_content_sha256": stable_hash(request),
            "completed": state.get("status") == "completed",
        }
        result["review_bindings"].append(binding)
        if not binding["completed"] or requested_report != report:
            problem("review_report_binding_mismatch", call_id)
        if not isinstance(review, dict):
            continue
        binding["review_sha256"] = stable_hash(review)
        expected = call_result.get("content")
        if not isinstance(expected, dict):
            problem("review_paid_result_missing", call_id)
        if issuer != "all" and expected != review:
            problem("review_artifact_differs_from_paid_output", call_id)
        if issuer == "all" and isinstance(expected, dict):
            # Runner may append company reviews and conservatively downgrade a verdict.
            stripped = {k: v for k, v in review.items() if k != "company_reviews"}
            if (
                stripped.get("verdict") == "revise"
                and expected.get("verdict") == "pass"
            ):
                stripped["verdict"] = "pass"
            if stripped != expected:
                problem("review_artifact_differs_from_paid_output", call_id)
        if review.get("verdict") != "pass":
            problem("review_not_passed", call_id)
        issues = review.get("issues")
        if not isinstance(issues, list) or any(not isinstance(i, dict) for i in issues):
            problem("invalid_review_issues", call_id)
            issues = []
        if any(i.get("severity") == "material" for i in issues):
            problem("material_review_issue", call_id)
        if not isinstance(review.get("checks"), list) or not review["checks"]:
            problem("review_checks_missing", call_id)
        refs(review, call_id)
    result["mechanical_ready"] = not errors
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    result = audit(args.root)
    output = args.root / "mechanical_checks.json"
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "mechanical_ready": result["mechanical_ready"],
                "error_count": len(result["errors"]),
                "output": str(output),
            }
        )
    )
    raise SystemExit(0 if result["mechanical_ready"] else 1)


if __name__ == "__main__":
    main()
