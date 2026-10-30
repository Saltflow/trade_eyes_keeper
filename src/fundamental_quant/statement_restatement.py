"""Explicit PDF comparative-column restatement proof; no generic latest-wins rule."""

import hashlib
import re
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=128)
def _aligned_marker(path, sha, page_number, current_year, prior_year):
    import pdfplumber

    if hashlib.sha256(Path(path).read_bytes()).hexdigest() != sha:
        return None
    with pdfplumber.open(path) as pdf:
        words = pdf.pages[page_number - 1].extract_words()
    current = [
        w
        for w in words
        if re.fullmatch(rf"{current_year}年(?:度|\d{{1,2}}月\d{{1,2}}日)?", w["text"])
    ]
    prior = [
        w
        for w in words
        if re.fullmatch(rf"{prior_year}年(?:度|\d{{1,2}}月\d{{1,2}}日)?", w["text"])
    ]
    markers = [w for w in words if "经重述" in w["text"]]
    center = lambda w: (w["x0"] + w["x1"]) / 2
    for old in prior:
        for new in current:
            if abs(old["top"] - new["top"]) > 3 or center(old) <= center(new):
                continue
            for mark in markers:
                distance = abs(center(mark) - center(old))
                if (
                    0 <= mark["top"] - old["top"] <= 30
                    and (
                        distance <= 20
                        or (
                            abs(mark["x1"] - old["x1"]) <= 6
                            and mark["x0"] >= old["x0"] - 6
                        )
                    )
                    and abs(center(mark) - center(new)) > 2 * max(distance, 10)
                ):
                    return {
                        "current_column": new,
                        "comparative_column": old,
                        "restatement_marker": mark,
                    }
    return None


def comparative_header_basis(field, metadata, document_id):
    if "经重述" not in field.get("header", ""):
        return None
    if not metadata.get("path") or not metadata.get("sha256"):
        return None
    prior = field.get("comparative_period_end", "")
    if not prior or prior >= metadata["period_end"]:
        return None
    for page in field.get("header_source_pages", []):
        proof = _aligned_marker(
            metadata["path"],
            metadata["sha256"],
            page,
            metadata["period_end"][:4],
            prior[:4],
        )
        if proof:
            return {
                "method": "pdf_aligned_comparative_restatement_header/v1",
                "reason": "explicit_restated_marker_aligned_to_comparative_column",
                "quote": field["header"],
                "refs": [f"{document_id}:p{page}"],
                "report_period": metadata["period_end"],
                "published_at": metadata["published_at"],
                "scope": "this_statement_comparative_column_only",
                "geometry": proof,
            }
    return None
