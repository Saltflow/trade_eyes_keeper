"""Reuse reviewed scanned-page facts without pretending they came from PDF text.

The reviewed manifest is immutable via a separate receipt. Each original PDF and
rendered image is checked again; rendering is compared pixel-for-pixel. These
checks preserve provenance, not a proof that a reviewer's transcription is true.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

from .fr_pack_contract import FIELDS

VERSION = "fr-image-evidence/2"


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _pixel_digest(image):
    rgb = image.convert("RGB")
    return hashlib.sha256(str(rgb.size).encode() + rgb.tobytes()).hexdigest()


def load_reviewed_image_facts(manifest_path, documents):
    """Load explicitly reviewed current/comparative source columns, not estimates."""
    manifest_path = Path(manifest_path)
    result = {
        "observations": [],
        "external_evidence": {},
        "issues": [],
        "version": VERSION,
    }
    try:
        receipt = json.loads(
            manifest_path.with_suffix(".receipt.json").read_text(encoding="utf8")
        )
        if receipt["manifest_sha256"] != _digest(manifest_path):
            raise ValueError("image_review_manifest_changed")
        manifest = json.loads(manifest_path.read_text(encoding="utf8"))
        if manifest.get("review_method") != "codex_visual_original_pdf_review":
            raise ValueError("unrecognized_image_review_method")
    except (OSError, ValueError, KeyError) as exc:
        result["issues"].append(
            {"code": "image_review_manifest_rejected", "reason": str(exc)}
        )
        return result
    import pypdfium2
    from PIL import Image

    known = {d["source_sha256"]: d for d in documents}
    verified_pdf_hashes = set()
    for entry in manifest["pages"]:
        try:
            document = known.get(entry["pdf_sha256"])
            if not document:
                raise ValueError("reviewed_pdf_not_in_packet")
            if (
                entry["issuer_id"] != document["issuer_id"]
                or entry["published_at"] != document["published_at"]
                or entry["report_period"] != document["period_end"]
            ):
                raise ValueError("reviewed_pdf_identity_or_period_mismatch")
            pdf_path = Path(document["source_path"])
            if entry["pdf_sha256"] not in verified_pdf_hashes:
                if _digest(pdf_path) != entry["pdf_sha256"]:
                    raise ValueError("reviewed_original_pdf_changed")
                verified_pdf_hashes.add(entry["pdf_sha256"])
            page = int(entry["page"])
            if page < 1 or str(page) not in document["pages"]:
                raise ValueError("reviewed_page_out_of_range")
            image_path = Path(entry["image_path"])
            if _digest(image_path) != entry["image_sha256"]:
                raise ValueError("reviewed_image_changed")
            with Image.open(image_path) as stored:
                if _pixel_digest(stored) != entry["pixel_sha256"]:
                    raise ValueError("reviewed_image_pixels_changed")
            with pypdfium2.PdfDocument(str(pdf_path)) as pdf:
                original_page = pdf[page - 1]
                bitmap = original_page.render(scale=float(entry["render_scale"]))
                try:
                    if _pixel_digest(bitmap.to_pil()) != entry["pixel_sha256"]:
                        raise ValueError("reviewed_image_not_original_pdf_render")
                finally:
                    bitmap.close()
                    original_page.close()
            ref = "EIMG:" + entry["pdf_sha256"][:16] + f":p{page}"
            staged = []
            for field in entry["fields"]:
                if field["metric"] not in set(FIELDS) | {"net_profit_total"}:
                    raise ValueError("unrecognized_reviewed_financial_metric")
                if (
                    field["currency"] != "CNY"
                    or field["unit"] != "元"
                    or not field.get("label")
                ):
                    raise ValueError("reviewed_currency_unit_or_label_missing")
                for column in ("current", "comparative"):
                    if column not in field:
                        continue
                    period = entry[f"{column}_period"]
                    value = Decimal(str(field[column]))
                    if not value.is_finite():
                        raise ValueError("nonfinite_reviewed_amount")
                    outflow = field["metric"] == "capex"
                    payment_basis = entry.get("payment_sign_convention", {}).get(
                        column, {}
                    )
                    payments = payment_basis.get("ordinary_rows", [])
                    normalize_payment = (
                        outflow
                        and payment_basis.get("mode") == "payments_shown_negative"
                        and len({x.get("label") for x in payments}) >= 2
                        and all(
                            Decimal(str(x["reported_amount"])) < 0 for x in payments
                        )
                        and Decimal(str(payment_basis.get("outflow_subtotal", 0))) < 0
                    )
                    normalized = -value if normalize_payment else value
                    restated = bool(entry.get(f"{column}_restated", False))
                    fact = {
                        "issuer_id": document["issuer_id"],
                        "company_name": document["company_name"],
                        "metric": field["metric"],
                        "period": period,
                        "kind": "FY"
                        if period.endswith("12-31")
                        else "H1"
                        if period.endswith("06-30")
                        else "YTD",
                        "value": float(normalized),
                        "raw_amount": str(normalized),
                        "reported_amount": str(value),
                        "unit": "元",
                        "currency": "CNY",
                        "scope": "consolidated",
                        "published_at": document["published_at"],
                        "document_id": document["document_id"],
                        "column": column,
                        "quote": f"[image transcription] {field['label']} | {period} | {value} 人民币元",
                        "refs": [ref],
                        "origin": "agent_verified_image",
                        "restated": restated,
                        "sign_convention": "cash_paid_positive_refund_negative"
                        if normalize_payment
                        else "as_reported_signed",
                        "cash_payment_sign_basis": payment_basis if outflow else {},
                        "verification": "codex_visual_original_pdf_review; not PDF-text literal matching",
                    }
                    if restated:
                        fact["restatement_basis"] = {
                            "method": "reviewed_image_restatement_header/v1",
                            "reason": "comparative_column_explicitly_restated",
                            "quote": entry["restatement_header"],
                            "refs": [ref],
                            "report_period": document["period_end"],
                            "published_at": document["published_at"],
                            "scope": "consolidated",
                        }
                    staged.append(fact)
            result["observations"].extend(staged)
            result["external_evidence"][ref] = {
                "source_type": "reviewed_pdf_image",
                "path": str(image_path.resolve()),
                "file_sha256": entry["image_sha256"],
                "pdf_path": str(pdf_path.resolve()),
                "pdf_sha256": entry["pdf_sha256"],
                "page": page,
                "issuer_id": document["issuer_id"],
                "record": entry,
                "verification": "original_pdf_pixel_match_and_prior_visual_transcription_review",
                "review_method": manifest["review_method"],
                "reviewed_by": manifest["reviewed_by"],
                "reviewed_at": manifest["reviewed_at"],
                "manifest_sha256": receipt["manifest_sha256"],
            }
        except (OSError, ValueError, KeyError, IndexError) as exc:
            result["issues"].append(
                {
                    "code": "reviewed_image_fact_rejected",
                    "page": entry.get("page"),
                    "report_period": entry.get("report_period"),
                    "reason": str(exc),
                }
            )
    return result
