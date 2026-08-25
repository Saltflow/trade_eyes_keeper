"""Parse official CSRC quarterly industry-classification publications."""

from __future__ import annotations

import hashlib
import io
import re
from datetime import date

import pdfplumber

from .industry_history import IndustryClassificationObservation


_FIRST_COMPANY = re.compile(
    r"^(?P<prefix>.*?)\s*(?P<major>\d{2})\s+(?P<name>.*?)\s+(?P<symbol>\d{6})\b"
)
_CONTINUED_COMPANY = re.compile(r"^(?:\([A-S]\)\s*)?(?P<symbol>\d{6})\b")


def _sector(major: int) -> str | None:
    """Map the 2012 CSRC two-digit major class to its one-letter section."""

    ranges = (
        (range(1, 6), "A"),
        (range(6, 12), "B"),
        (range(13, 44), "C"),
        (range(44, 47), "D"),
        (range(47, 51), "E"),
        (range(51, 53), "F"),
        (range(53, 61), "G"),
        (range(61, 63), "H"),
        (range(63, 66), "I"),
        (range(66, 68), "J"),
        (range(68, 71), "K"),
        (range(71, 73), "L"),
        (range(73, 76), "M"),
        (range(76, 78), "N"),
        (range(78, 80), "O"),
        (range(80, 83), "P"),
        (range(83, 85), "Q"),
        (range(85, 90), "R"),
        (range(90, 91), "S"),
    )
    return next((letter for values, letter in ranges if major in values), None)


def extract_pdf_text(content: bytes) -> str:
    """Extract text from an official attachment with no OCR fallback."""

    with pdfplumber.open(io.BytesIO(content)) as document:
        return "\n".join(page.extract_text() or "" for page in document.pages)


def parse_csrc_classification_text(
    text: str,
    *,
    period_end: date,
    published_at: date,
    source_url: str,
    source_content: bytes | None = None,
) -> list[IndustryClassificationObservation]:
    """Parse a CSRC attachment while carrying the last industry down rows.

    The published PDFs put the section letter on a separate visual row in
    some pages.  The 2012 two-digit major code has a deterministic section,
    so the parser derives (for example) ``C38`` from major code ``38`` rather
    than relying on visual page layout.
    """

    current_code: str | None = None
    current_name: str | None = None
    observations: dict[str, IndustryClassificationObservation] = {}
    sha256 = hashlib.sha256(source_content).hexdigest() if source_content else None
    for raw in text.splitlines():
        line = " ".join(raw.split())
        if not line or "上市公司代码" in line:
            continue
        matched = _FIRST_COMPANY.match(line)
        if matched is not None:
            major = int(matched.group("major"))
            section = _sector(major)
            name = matched.group("name").strip()
            if section is not None and name:
                current_code = f"{section}{major:02d}"
                current_name = name
                symbol = matched.group("symbol")
                observations[symbol] = IndustryClassificationObservation(
                    symbol=symbol,
                    industry_code=current_code,
                    industry_name=current_name,
                    taxonomy="csrc-2012",
                    period_end=period_end,
                    published_at=published_at,
                    source_url=source_url,
                    source_sha256=sha256,
                ).validate()
                continue
        continued = _CONTINUED_COMPANY.match(line)
        if (
            continued is not None
            and current_code is not None
            and current_name is not None
        ):
            symbol = continued.group("symbol")
            observations[symbol] = IndustryClassificationObservation(
                symbol=symbol,
                industry_code=current_code,
                industry_name=current_name,
                taxonomy="csrc-2012",
                period_end=period_end,
                published_at=published_at,
                source_url=source_url,
                source_sha256=sha256,
            ).validate()
    if not observations:
        raise ValueError("official CSRC attachment produced no industry rows")
    return sorted(observations.values(), key=lambda item: item.symbol)
