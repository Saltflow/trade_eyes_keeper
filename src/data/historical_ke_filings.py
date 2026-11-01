"""Bounded, provenance-preserving disclosed EPS inputs for historical Ke.

This module does not fetch current valuation ratios, infer share-class ratios,
or require a balance sheet.  Publication-day values become usable the following
calendar day, including comparative figures first discovered in a later filing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class EpsFact:
    symbol: str
    period_start: date
    period_end: date
    period_kind: str
    published_at: date
    eps: float
    currency: str
    share_class: str
    source_url: str
    document_sha256: str
    available_from: date | None = None
    eps_kind: str = "basic"
    selected_pdf_pages: tuple[int, ...] = ()
    extraction_note: str = ""

    def __post_init__(self) -> None:
        for name in ("period_start", "period_end", "published_at", "available_from"):
            value = getattr(self, name)
            if isinstance(value, str):
                object.__setattr__(self, name, date.fromisoformat(value))
        if self.available_from is None:
            object.__setattr__(
                self, "available_from", self.published_at + timedelta(days=1)
            )
        if self.period_kind not in {"annual", "ytd", "quarter", "ttm"}:
            raise ValueError("unsupported EPS period kind")
        if not math.isfinite(self.eps):
            raise ValueError("EPS must be finite; losses are valid observations")
        if self.period_start > self.period_end or self.period_end > self.published_at:
            raise ValueError("EPS period/publication chronology is invalid")
        if self.available_from <= self.published_at:
            raise ValueError("date-only disclosure requires next-day availability")
        if not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise ValueError("explicit ISO currency required")
        if not self.symbol or not self.share_class or not self.source_url:
            raise ValueError("security identity and source URL required")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", self.document_sha256):
            raise ValueError("document SHA256 required")
        if self.eps_kind not in {"basic", "diluted"}:
            raise ValueError("EPS kind must be basic or diluted")
        object.__setattr__(self, "selected_pdf_pages", tuple(self.selected_pdf_pages))

    def to_dict(self) -> dict:
        result = asdict(self)
        for name in ("period_start", "period_end", "published_at", "available_from"):
            result[name] = getattr(self, name).isoformat()
        result["selected_pdf_pages"] = list(self.selected_pdf_pages)
        return result

    @classmethod
    def from_dict(cls, value: dict) -> EpsFact:
        return cls(**value)


def earnings_as_of(
    facts: Iterable[EpsFact | dict],
    day: date | str,
    *,
    symbol: str | None = None,
    share_class: str | None = None,
) -> dict:
    """Resolve disclosed TTM EPS; never fall back past a newer missing bridge.

    Components must agree on currency, class and basic/diluted basis.  Restated
    comparisons can only replace prior observations after their own publication.
    A latest loss is returned as a loss; the caller must mask nonpositive PE.
    """
    day = date.fromisoformat(day) if isinstance(day, str) else day
    known = [f if isinstance(f, EpsFact) else EpsFact.from_dict(f) for f in facts]
    known = [
        f
        for f in known
        if f.available_from <= day
        and f.period_end <= day
        and (symbol is None or f.symbol == symbol)
        and (share_class is None or f.share_class == share_class)
    ]
    missing = {
        "ttm_eps": None,
        "currency": None,
        "available_from": None,
        "components": [],
        "status": "unavailable",
        "period_end": None,
        "reason": "no disclosed EPS available",
    }
    if not known:
        return missing
    if len({(f.symbol, f.share_class) for f in known}) != 1:
        return {**missing, "reason": "ambiguous security/share class"}
    latest_end = max(f.period_end for f in known)
    newest = [f for f in known if f.period_end == latest_end]
    # Latest filing determines the active currency and EPS basis. Basic is the
    # stable default when both were disclosed in the same release.
    active = max(newest, key=lambda f: (f.published_at, f.eps_kind == "basic"))
    compatible = [
        f
        for f in known
        if (f.currency, f.eps_kind) == (active.currency, active.eps_kind)
    ]
    revisions: dict[tuple, EpsFact] = {}
    for fact in compatible:
        key = (fact.period_start, fact.period_end, fact.period_kind)
        old = revisions.get(key)
        if old is None or fact.published_at > old.published_at:
            revisions[key] = fact
        elif fact.published_at == old.published_at and fact.eps != old.eps:
            return {**missing, "reason": "conflicting same-date EPS evidence"}
    known = list(revisions.values())
    current = [f for f in known if f.period_end == latest_end]
    components: list[EpsFact] = []
    weights: list[int] = []
    direct = [f for f in current if f.period_kind in {"annual", "ttm"}]
    if direct:
        components = [
            max(direct, key=lambda f: (f.published_at, f.period_kind == "ttm"))
        ]
        weights = [1]
    else:
        ytd = [f for f in current if f.period_kind == "ytd"]
        if ytd:
            latest = max(ytd, key=lambda f: f.published_at)
            prior_end = latest.period_end.replace(year=latest.period_end.year - 1)
            prior_start = latest.period_start.replace(year=latest.period_start.year - 1)
            prior = [
                f
                for f in known
                if f.period_kind == "ytd"
                and f.period_start == prior_start
                and f.period_end == prior_end
            ]
            annual = [
                f
                for f in known
                if f.period_kind == "annual"
                and f.period_end == latest.period_start - timedelta(days=1)
            ]
            if prior and annual:
                components = [
                    max(annual, key=lambda f: f.published_at),
                    latest,
                    max(prior, key=lambda f: f.published_at),
                ]
                weights = [1, 1, -1]
        if not components:
            quarters = [f for f in known if f.period_kind == "quarter"]
            cursor = latest_end
            for _ in range(4):
                matches = [f for f in quarters if f.period_end == cursor]
                if not matches:
                    break
                item = max(matches, key=lambda f: f.published_at)
                if not 80 <= (item.period_end - item.period_start).days <= 100:
                    break
                components.append(item)
                cursor = item.period_start - timedelta(days=1)
            if len(components) == 4 and 350 <= (latest_end - cursor).days <= 380:
                weights = [1] * 4
            else:
                components = []
    if not components:
        return {
            **missing,
            "period_end": latest_end.isoformat(),
            "currency": active.currency,
            "reason": "latest disclosed period has no complete TTM bridge",
        }
    value = sum(w * f.eps for w, f in zip(weights, components))
    return {
        "ttm_eps": value,
        "currency": active.currency,
        "available_from": max(f.available_from for f in components).isoformat(),
        "period_end": latest_end.isoformat(),
        "components": [
            {**f.to_dict(), "weight": w} for f, w in zip(components, weights)
        ],
        "status": "available",
        "reason": None,
        "eps_kind": active.eps_kind,
        "share_class": active.share_class,
    }


def eps_numbers(row: str) -> list[float]:
    """Read an explicitly selected EPS numeric row, retaining accounting losses."""
    tokens = re.findall(r"\(?\s*[-−]?\d[\d,]*\.\d+\s*\)?|\(\s*\d[\d,]*\s*\)", row)
    result = []
    for token in tokens:
        text = token.replace(",", "").replace(" ", "").replace("−", "-")
        if text.startswith("(") and text.endswith(")"):
            text = "-" + text[1:-1]
        result.append(float(text))
    return result


def dollar_eps_cells(row: str, *, decimal_places: int = 2) -> list[float]:
    """Read separate dollar-denominated EPS cells with dotted-leader artifacts.

    Some PDFs draw leader dots over digits (``$ ...1..2...18``). Dollar cell
    boundaries and the explicitly reviewed displayed precision are required;
    this function never combines amounts across cells or changes share class.
    """
    if not 0 <= decimal_places <= 6 or "$" not in row:
        raise ValueError("explicit dollar EPS cells and precision required")
    result = []
    for cell in row.split("$")[1:]:
        digits = re.sub(r"\D", "", cell)
        if not digits or (decimal_places and "." not in cell):
            raise ValueError("dollar EPS cell lacks explicit decimal evidence")
        if len(digits) <= decimal_places:
            raise ValueError("dollar EPS cell precision is ambiguous")
        number = int(digits) / 10**decimal_places
        if "(" in cell or re.search(r"-\s*\d", cell):
            number = -number
        result.append(number)
    return result


def facts_from_eps_row(
    row: str,
    columns: Iterable[dict],
    *,
    symbol: str,
    published_at: date,
    currency: str,
    share_class: str,
    source_url: str,
    document_sha256: str,
    unit: str = "per_share",
    eps_kind: str = "basic",
    selected_pdf_pages: tuple[int, ...] = (),
    extraction_note: str = "",
) -> list[EpsFact]:
    """Materialize explicit filing table columns without inferring their meaning.

    ``columns`` contains period_start/end/kind and numeric ``value_index``.
    Selection must be backed by the original table header. The caller must
    select ordinary-share or Class-B rows explicitly, never an ADS/Class-A row.
    Whole-table million/billion units must not be applied to EPS.
    """
    if unit not in {"per_share", "cents_per_share"}:
        raise ValueError("EPS unit must explicitly be per share or cents per share")
    values = eps_numbers(row)
    result = []
    for column in columns:
        index = column["value_index"]
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or not 0 <= index < len(values)
        ):
            raise ValueError("EPS table column unavailable")
        result.append(
            EpsFact(
                symbol=symbol,
                period_start=column["period_start"],
                period_end=column["period_end"],
                period_kind=column["period_kind"],
                published_at=published_at,
                eps=values[index] / (100 if unit == "cents_per_share" else 1),
                currency=currency,
                share_class=share_class,
                source_url=source_url,
                document_sha256=document_sha256,
                eps_kind=eps_kind,
                selected_pdf_pages=selected_pdf_pages,
                extraction_note=extraction_note,
            )
        )
    return result


def extract_pdf_pages(
    content: bytes,
    pages: Iterable[int] = range(1, 17),
    *,
    timeout_seconds: float = 20,
    max_bytes: int = 20 * 1024 * 1024,
) -> dict:
    """Extract only selected 1-based pages in an independently killable process."""
    pages = tuple(sorted(set(pages)))
    if not content or len(content) > max_bytes:
        raise ValueError("PDF empty or exceeds byte budget")
    if not pages or len(pages) > 24 or min(pages) < 1 or max(pages) > 128:
        raise ValueError("PDF page selection exceeds bounded extraction budget")
    if not 0 < timeout_seconds <= 120:
        raise ValueError("PDF timeout must be in (0,120]")
    with tempfile.TemporaryDirectory(prefix="ke_eps_") as directory:
        source = Path(directory) / "input.pdf"
        output = Path(directory) / "output.json"
        source.write_bytes(content)
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--extract",
            str(source),
            str(output),
            "--pages",
            ",".join(map(str, pages)),
        ]
        try:
            process = subprocess.run(
                command, capture_output=True, timeout=timeout_seconds, check=False
            )
        except subprocess.TimeoutExpired as exc:
            # subprocess.run kills and waits for its direct worker on timeout.
            raise TimeoutError(
                "EPS PDF extraction worker exceeded hard deadline"
            ) from exc
        if process.returncode or not output.exists():
            raise ValueError(
                "EPS PDF worker failed: "
                + process.stderr.decode("utf-8", errors="replace")[-800:]
            )
        if output.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("EPS extracted text exceeds budget")
        result = json.loads(output.read_text(encoding="utf-8"))
    result["document_sha256"] = hashlib.sha256(content).hexdigest()
    return result


def _extract_worker(source: Path, output: Path, pages: list[int]) -> None:
    import pdfplumber

    result = []
    with pdfplumber.open(source) as document:
        count = len(document.pages)
        for number in pages:
            if number > count:
                continue
            page = document.pages[number - 1]
            text = page.extract_text(layout=True) or ""
            result.append(
                {
                    "page": number,
                    "text": text,
                    "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                }
            )
            page.flush_cache()
    output.write_text(
        json.dumps({"page_count": count, "pages": result}, ensure_ascii=False),
        encoding="utf-8",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--extract", nargs=2, type=Path)
    parser.add_argument("--pages", required=True)
    args = parser.parse_args()
    _extract_worker(*args.extract, [int(p) for p in args.pages.split(",")])
