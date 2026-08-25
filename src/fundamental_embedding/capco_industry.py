"""Parser for official CAPCO half-year industry classification PDFs."""

from __future__ import annotations

import hashlib
import io
import re
from datetime import date

import pdfplumber

from .industry_history import IndustryClassificationObservation

_SYMBOL = re.compile(r"^\d{6}$")
_SECTOR = re.compile(r"^[A-S]$")
_FINE_CODE = re.compile(r"^\d{2}$")


def _cell(value: object) -> str:
    return " ".join(str(value or "").split())


def parse_capco_classification_pdf(
    content: bytes,
    *,
    period_end: date,
    published_at: date,
    source_url: str,
) -> list[IndustryClassificationObservation]:
    """Extract code, sector and two-digit industry from CAPCO tables.

    The official PDF's first eight columns are code, name, sector code/name,
    an optional intermediate group, and two-digit industry code/name.  Company
    names are deliberately ignored; they are not needed for joining and can
    contain line breaks or punctuation.
    """

    sha256 = hashlib.sha256(content).hexdigest()
    observations: dict[str, IndustryClassificationObservation] = {}
    with pdfplumber.open(io.BytesIO(content)) as document:
        for page in document.pages:
            for table in page.extract_tables() or []:
                for row in table:
                    cells = [_cell(item) for item in (row or [])]
                    if len(cells) < 8 or not _SYMBOL.fullmatch(cells[0]):
                        continue
                    sector = cells[2]
                    fine_code = cells[6] or cells[4]
                    if not _SECTOR.fullmatch(sector) or not _FINE_CODE.fullmatch(
                        fine_code
                    ):
                        continue
                    fine_name = cells[7] or cells[5] or cells[3]
                    if not fine_name:
                        continue
                    symbol = cells[0]
                    observations[symbol] = IndustryClassificationObservation(
                        symbol=symbol,
                        industry_code=f"{sector}{fine_code}",
                        industry_name=fine_name,
                        taxonomy="capco-listed-company-2024",
                        period_end=period_end,
                        published_at=published_at,
                        source_url=source_url,
                        source_sha256=sha256,
                    ).validate()
    if not observations:
        raise ValueError("official CAPCO attachment produced no industry rows")
    return sorted(observations.values(), key=lambda item: item.symbol)
