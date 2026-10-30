"""Scoped original document acquisition and image retrieval for peer research."""

from __future__ import annotations

import base64
import hashlib
import io
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from .longitudinal_data import prepare_comparison
from .session_sources import SessionReportSource


class DocumentAcquirer:
    def __init__(self, packet, config, store, campaign):
        self.spec = packet["spec"]
        self.config, self.store, self.campaign = config, store, campaign
        self.companies = {c["issuer_id"]: c for c in self.spec["companies"]}
        service = SimpleNamespace(
            store=store,
            root=store.root,
            acceptance=campaign,
            config=config,
            settings=config.get("fundamental_quant", {}),
            today=date.fromisoformat(self.spec["as_of"]),
            sources=SimpleNamespace(project_root=Path.cwd()),
        )
        self.source = SessionReportSource(service)

    def __call__(self, request):
        issuer = request.get("issuer_id")
        if issuer not in self.companies:
            return {"status": "unavailable", "errors": ["company_outside_group"]}
        period = date.fromisoformat(request["period"])
        if period > date.fromisoformat(self.spec["as_of"]) or period.year < int(
            self.spec["as_of"][:4]
        ) - self.spec.get("history_years", 7):
            return {
                "status": "unavailable",
                "errors": ["period_outside_research_scope"],
            }
        kind = request["kind"]
        kinds = {
            "annual": "annual",
            "interim": "half_year",
            "half_year": "half_year",
            "first_quarter": "first_quarter",
            "third_quarter": "third_quarter",
        }
        if kind not in kinds:
            return {"status": "unavailable", "errors": ["unsupported_report_kind"]}
        company = {
            "id": issuer,
            "code": issuer.split(":")[-1],
            "name": self.companies[issuer]["name"],
            "market": "a_share",
        }
        result = self.source.collect(company, kinds[kind], period.year)
        if result["status"] != "success":
            return result
        metadata = []
        for document in result["documents"]:
            if (
                document["period_end"] != period.isoformat()
                or document["published_at"] > self.spec["as_of"]
            ):
                return {
                    "status": "conflict",
                    "errors": ["acquired_document_period_or_disclosure_mismatch"],
                }
            metadata.append(
                {**document, "issuer_id": issuer, "company_name": company["name"]}
            )
        # One deterministic extraction builds the same facts/chapters/panel used
        # by offline prepare. Download success is not parsing success.
        supplement = prepare_comparison(self.spec, document_specs=metadata)
        if len(supplement["documents"]) != len(metadata):
            return {
                "status": "unavailable",
                "data": {"issues": supplement["issues"]},
                "errors": ["acquired_document_parsing_failed"],
                "retryable": False,
                "next_action": "repair_parser_or_source",
            }
        pages = {
            f"{document['document_id']}:p{number}": text
            for document in supplement["documents"]
            for number, text in document["pages"].items()
        }
        return {
            "status": "success",
            "pages": pages,
            "documents": supplement["documents"],
            "supplement": supplement,
        }

    def market_history(self, request):
        """One scoped acquisition into an isolated cache, never an optimizer run."""
        import copy

        from src.data.point_in_time_backfill import PointInTimeBackfillService

        from .longitudinal_data import (
            historical_month_end_valuations,
            load_cached_valuation_events,
        )

        issuer = request.get("issuer_id")
        if issuer not in self.companies or not request.get("reason"):
            raise ValueError("history_request_outside_scope_or_missing_reason")
        self.source._guard()
        cfg = copy.deepcopy(self.config)
        output = self.store.root / "longitudinal_history"
        settings = cfg.setdefault("point_in_time_data", {})
        settings.update(
            output_dir=str(output),
            history_years=self.spec.get("history_years", 7),
            fx_symbols=[],
            market_only_symbols=[],
            official_statement_crawlers=False,
        )
        service = PointInTimeBackfillService(cfg)
        receipt = service.run(
            [issuer.split(":")[-1]],
            evaluation_date=date.fromisoformat(self.spec["as_of"]),
        )
        spec = {**self.spec, "companies": [self.companies[issuer]]}
        cached = load_cached_valuation_events(spec, root=output)
        return {
            "status": "success" if cached["price_events"] else "partial",
            "external_evidence": cached.get("external_evidence", {}),
            "valuations": historical_month_end_valuations(
                cached["price_events"],
                cached["statement_events"],
                as_of=self.spec["as_of"],
            ),
            "issues": cached["issues"],
            "receipt": receipt,
            "refs": list(cached.get("external_evidence", {})),
        }


def page_image(document, page_number, *, allowed_root=None):
    """Full physical page; only registered/hash-verified originals are readable."""
    root = Path(allowed_root or Path.cwd()).resolve()
    path = Path(document["source_path"]).resolve()
    if (
        not path.is_relative_to(root)
        or hashlib.sha256(path.read_bytes()).hexdigest() != document["source_sha256"]
    ):
        raise ValueError("original_pdf_path_or_hash_invalid")
    if type(page_number) is not int or page_number < 1:
        raise ValueError("invalid_page_number")
    import pypdfium2

    with pypdfium2.PdfDocument(str(path)) as pdf:
        if page_number > len(pdf):
            raise ValueError("page_outside_document")
        page = pdf[page_number]
        bitmap = page.render(scale=2)
        try:
            image = bitmap.to_pil().convert("RGB")
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=88)
            data = buffer.getvalue()
        finally:
            bitmap.close()
            page.close()
    return {
        "ref": f"{document['document_id']}:p{page_number}",
        "sha256": hashlib.sha256(data).hexdigest(),
        "image_url": "data:image/jpeg;base64," + base64.b64encode(data).decode(),
        "page": page_number,
        "source_sha256": document["source_sha256"],
    }
