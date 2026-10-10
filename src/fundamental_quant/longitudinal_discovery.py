"""Offline archive discovery and original-FR coverage, separate from numeric coverage."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from .pdf_sessions import PDFBatchReader

VERSION = "longitudinal-original-coverage/1"


class _ReadOnlyCatalog(PDFBatchReader):
    """Reuse catalog parsing/hash checks without constructing a research store.

    Only discover_catalog and _bytes are used; neither creates rendering tables,
    downloads files, nor initializes a model/provider.
    """

    def __init__(self, project_root: Path):
        self.project_root = project_root.resolve()
        self.allowed_roots = (self.project_root,)


def archived_documents(companies: list, as_of: str, project_root=None) -> list:
    reader = _ReadOnlyCatalog(Path(project_root or Path.cwd()))
    found = []
    for company in companies:
        issuer = company["issuer_id"]
        if not issuer.startswith("a_share:"):
            continue
        identity = {"id": issuer, "code": issuer.split(":", 1)[1]}
        for doc in reader.discover_catalog(identity, date.fromisoformat(as_of)):
            reader._bytes(doc)
            found.append(
                {
                    "issuer_id": issuer,
                    "company_name": company.get("name") or company["issuer_id"],
                    "document_id": doc.document_id,
                    "period_end": doc.period_end,
                    "published_at": doc.published_at,
                    "path": doc.path,
                    "sha256": doc.sha256,
                    "kind": doc.kind,
                    "url": doc.url,
                    "discovered_from": "dataset_catalog",
                }
            )
    return found


def original_report_coverage(spec: dict, documents: list) -> dict:
    """Inventory full report originals; comparative facts never satisfy a slot.

    Missing means absent from this frozen input, not that the issuer failed to
    disclose. A latest ended half year can still be within its disclosure window.
    """
    as_of = date.fromisoformat(spec["as_of"])
    periods = [
        (f"{year}-12-31", "annual")
        for year in range(as_of.year - spec.get("history_years", 7), as_of.year)
    ]
    interim_year = as_of.year if as_of.month >= 7 else as_of.year - 1
    periods.append((f"{interim_year}-06-30", "interim"))
    rows = []
    for company in spec["companies"]:
        for period, kind in periods:
            candidates = []
            for doc in documents:
                if (
                    doc.get("issuer_id") != company["issuer_id"]
                    or doc.get("period_end") != period
                    or not doc.get("published_at")
                    or doc["published_at"][:10] > spec["as_of"]
                ):
                    continue
                doc_kind = doc.get("kind")
                if doc_kind and doc_kind not in {
                    kind,
                    "half_year" if kind == "interim" else kind,
                }:
                    continue
                path = doc.get("source_path") or doc.get("path")
                sha = doc.get("source_sha256") or doc.get("sha256")
                if not path or not sha:
                    continue
                candidates.append(
                    {
                        "document_id": doc.get("document_id"),
                        "published_at": doc["published_at"],
                        "path": path,
                        "sha256": sha,
                    }
                )
            rows.append(
                {
                    "issuer_id": company["issuer_id"],
                    "company_name": company.get("name") or company["issuer_id"],
                    "period_end": period,
                    "kind": kind,
                    "status": "archived_original" if candidates else "missing_original",
                    "documents": candidates,
                    "next_action": "read_original_and_verify_chapters"
                    if candidates
                    else "bounded_public_original_acquisition",
                }
            )
    return {
        "version": VERSION,
        "as_of": spec["as_of"],
        "group_id": spec["group_id"],
        "meaning": "Original reports only; comparative financial numbers do not establish that year's MD&A or footnotes. Missing is not undisclosed or investment failure.",
        "rows": rows,
        "expected": len(rows),
        "available": sum(row["status"] == "archived_original" for row in rows),
        "missing": sum(row["status"] == "missing_original" for row in rows),
        "network_requests": 0,
        "paid_model_calls": 0,
    }
