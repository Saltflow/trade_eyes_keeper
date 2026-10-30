"""PDF-coordinate evidence for sparse two-column consolidated statement rows.

Coordinates supplement text; missing cells never become zero. No network/model.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from datetime import date
from pathlib import Path
from statistics import median

LAYOUT_VERSION = "fr-statement-layout/2"
MONEY = re.compile(
    r"(?<![\d.,])[-−－—]?(?:\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d+\.\d{2})(?![\d.,])"
)
HEADER = re.compile(
    r"期末余额|期初余额|年初余额|20\d{2}\s*年(?:\s*\d{1,2}\s*月\s*\d{1,2}\s*日|半年度|度)?"
)


def _hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def capture_page(textpage, page=None):
    """Retain the PDFium text and each amount/header's original point rectangle."""
    from pypdfium2 import raw

    text = textpage.get_text_range()

    def box(start, end):
        boxes = []
        for index in range(start, end):
            if text[index].isspace():
                continue
            char_index = raw.FPDFText_GetCharIndexFromTextIndex(textpage, index)
            if char_index >= 0:
                boxes.append(textpage.get_charbox(char_index))
        if not boxes:
            return None
        return [
            round(min(b[0] for b in boxes), 4),
            round(min(b[1] for b in boxes), 4),
            round(max(b[2] for b in boxes), 4),
            round(max(b[3] for b in boxes), 4),
        ]

    lines, offset = [], 0
    for line_no, full_line in enumerate(text.splitlines(keepends=True), 1):
        line = full_line.rstrip("\r\n")
        record = {
            "line": line_no,
            "text": line,
            "bbox": box(offset, offset + len(line)),
        }
        record["cell_glyphs"] = [
            {"text": char, "bbox": box(offset + i, offset + i + 1)}
            for i, char in enumerate(line)
            if not char.isspace()
        ]
        for key, pattern in (("amounts", MONEY), ("headings", HEADER)):
            record[key] = [
                {"text": m[0], "bbox": box(offset + m.start(), offset + m.end())}
                for m in pattern.finditer(line)
            ]
        lines.append(record)
        offset += len(full_line)
    return {
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "lines": lines,
        "image_bounds": [
            list(obj.get_bounds())
            for obj in page.get_objects(filter=[raw.FPDF_PAGEOBJ_IMAGE])
        ]
        if page is not None
        else None,
    }


def layout_for_source(source, sections):
    """Hash-bound, versioned page layout cache; errors are explicit, not fatal."""
    meta = source["metadata"]
    if not meta.get("path") or not meta.get("sha256"):
        return {
            "status": "unavailable",
            "reason": "pdf_path_or_hash_missing",
            "pages": {},
        }
    pdf = Path(meta["path"])
    page_numbers = sorted(
        {
            line["page"]
            for s in sections
            if s["kind"] in {"balance", "income", "cashflow"}
            for line in s["lines"]
        }
    )
    cache = pdf.parent / f"fr_statement_layout_v2_{meta['sha256']}.json"
    try:
        if hashlib.sha256(pdf.read_bytes()).hexdigest() != meta["sha256"]:
            raise ValueError("pdf_source_hash_mismatch")
        cache_issue = None
        if cache.exists():
            try:
                stored = json.loads(cache.read_text(encoding="utf-8"))
                if (
                    isinstance(stored, dict)
                    and stored.get("version") == LAYOUT_VERSION
                    and stored.get("pdf_sha256") == meta["sha256"]
                    and isinstance(stored.get("pages"), dict)
                    and stored.get("layout_sha256") == _hash(stored["pages"])
                    and all(str(p) in stored["pages"] for p in page_numbers)
                    and all(
                        "cell_glyphs" in row
                        for page in stored["pages"].values()
                        for row in page["lines"]
                    )
                ):
                    return {**stored, "cache_reused": True}
                cache_issue = "stale_or_invalid_layout_cache"
            except (OSError, ValueError) as exc:
                cache_issue = f"unreadable_layout_cache:{type(exc).__name__}"
        import pypdfium2

        pages = {}
        with pypdfium2.PdfDocument(str(pdf)) as document:
            for page_no in page_numbers:
                page = document[page_no - 1]
                textpage = page.get_textpage()
                try:
                    actual = textpage.get_text_range()
                    if actual != source["pages"].get(str(page_no)):
                        raise ValueError(f"pdf_text_layout_mismatch:p{page_no}")
                    pages[str(page_no)] = capture_page(textpage, page)
                finally:
                    textpage.close()
                    page.close()
        stored = {
            "status": "obtained",
            "version": LAYOUT_VERSION,
            "pdf_sha256": meta["sha256"],
            "pages": pages,
            "layout_sha256": _hash(pages),
            "repaired_cache_issue": cache_issue,
        }
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=cache.parent,
                prefix=".fr_layout_",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(stored, handle, ensure_ascii=False)
            temporary.replace(cache)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return {**stored, "cache_reused": False}
    except Exception as exc:  # noqa: BLE001 - report a per-document acquisition failure
        return {
            "status": "parse_failed",
            "reason": f"layout:{type(exc).__name__}:{exc}",
            "pages": {},
        }


def _column_grid(window, section, layout, year, period_end=None):
    """Match one amount to verified columns using same-page paired-row anchors.

    Never cross a page or statement boundary; two independent paired rows must
    establish stable right edges. Header roles must be explicit and current-first.
    """
    if (
        not layout
        or layout.get("status") != "obtained"
        or len({r["page"] for r in window}) != 1
    ):
        return None
    indexed = {
        int(p): {r["line"]: r for r in v["lines"]} for p, v in layout["pages"].items()
    }

    def original(row):
        item = indexed.get(row["page"], {}).get(row["line"])
        return item if item and item["text"] == row["text"] else None

    headings = []
    header_source = None
    for row in section["lines"][:22]:
        item = original(row)
        if not item or not re.match(
            r"^(?:项目|附注|资产附注|负债和股东权益附注)",
            re.sub(r"\s+", "", item["text"]),
        ):
            continue
        tokens = item["headings"]
        if len(tokens) != 2 or any(not t["bbox"] for t in tokens):
            continue
        left, right = sorted(tokens, key=lambda t: t["bbox"][0])
        is_balance = left["text"] == "期末余额" and right["text"] in {
            "期初余额",
            "年初余额",
        }
        is_year = left["text"].startswith(str(year)) and right["text"].startswith(
            str(year - 1)
        )
        dates = []
        for token in (left, right):
            match = re.fullmatch(
                r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", token["text"]
            )
            try:
                dates.append(
                    date(*map(int, match.groups())).isoformat() if match else None
                )
            except ValueError:
                dates.append(None)
        is_dates = bool(
            period_end and all(dates) and dates[0] == period_end and dates[1] < dates[0]
        )
        if is_balance or is_year or is_dates:
            headings = [left, right]
            header_source = {
                "page": row["page"],
                "line": row["line"],
                "quote": row["text"],
            }
            break
    if not headings:
        return None
    page_no = window[0]["page"]
    row_items = [original(r) for r in window]
    if any(item is None for item in row_items):
        return None
    anchors = []
    for row in section["lines"]:
        if row["page"] != page_no:
            continue
        item = original(row)
        if (
            not item
            or len(item["amounts"]) != 2
            or any(not a["bbox"] for a in item["amounts"])
        ):
            continue
        pair = sorted(item["amounts"], key=lambda t: t["bbox"][0])
        if pair[0]["bbox"][2] >= pair[1]["bbox"][0]:
            continue
        anchors.append((row, pair))
    if len(anchors) < 2:
        return None
    edges = [median(pair[col]["bbox"][2] for _, pair in anchors) for col in (0, 1)]
    if edges[1] - edges[0] < 30 or any(
        abs(pair[col]["bbox"][2] - edges[col]) > 3
        for _, pair in anchors
        for col in (0, 1)
    ):
        return None
    # Page continuation may shift a few points, but never exchange columns.
    if any(
        abs((headings[col]["bbox"][0] + headings[col]["bbox"][2]) / 2 - edges[col])
        > (edges[1] - edges[0]) * 0.7
        for col in (0, 1)
    ):
        return None
    return {
        "page": page_no,
        "header_tokens": headings,
        "header_source": header_source,
        "column_right_edges": edges,
        "column_left_edges": [
            min(pair[col]["bbox"][0] for _, pair in anchors) for col in (0, 1)
        ],
        "anchors": [
            {"page": r["page"], "line": r["line"], "quote": r["text"], "amounts": pair}
            for r, pair in anchors[:3]
        ],
        "method": LAYOUT_VERSION,
        "pdf_sha256": layout.get("pdf_sha256"),
    }


def repair_prefixed_row(row, pattern, layout):
    """Split PDF drawing-order joins only when original Y coordinates prove it."""
    raw = next(
        (
            r
            for r in layout.get("pages", {}).get(str(row["page"]), {}).get("lines", [])
            if r["line"] == row["line"] and r["text"] == row["text"]
        ),
        None,
    )
    if not raw or not raw.get("cell_glyphs"):
        return row
    text = row["text"]
    match = re.search(pattern, text)
    if not match or not match.start():
        return row
    prefix = text[: match.start()]
    if not MONEY.search(prefix) or re.search(r"[^\s\d,.()（）−－—–/-]", prefix):
        return row
    glyphs = raw["cell_glyphs"]
    left_count = len(re.sub(r"\s", "", prefix))
    label_count = len(re.sub(r"\s", "", match[0]))
    label = glyphs[left_count : left_count + label_count]
    before = glyphs[:left_count]
    after = glyphs[left_count + label_count :]
    if not label or not before or not after or any(not g.get("bbox") for g in glyphs):
        return row
    label_y = median((g["bbox"][1] + g["bbox"][3]) / 2 for g in label)
    if any((g["bbox"][1] + g["bbox"][3]) / 2 <= label_y + 5 for g in before):
        return row
    if any(
        g["bbox"][3] < min(x["bbox"][1] for x in label)
        or g["bbox"][1] > max(x["bbox"][3] for x in label)
        for g in after
    ):
        return row
    return {
        **row,
        "text": text[match.start() :],
        "drawing_order_repair": {
            "method": "original_pdf_y_separation/1",
            "original_quote": text,
            "page": row["page"],
            "line": row["line"],
            "pdf_sha256": layout["pdf_sha256"],
            "excluded_glyphs": before,
            "retained_glyphs": label + after,
        },
    }


def split_pair_on_same_pdf_row(window, layout):
    """Prove text-split amounts occupy the same original two-column PDF row."""
    if not layout or len({row["page"] for row in window}) != 1:
        return None
    page = layout.get("pages", {}).get(str(window[0]["page"]), {})
    indexed = {row["line"]: row for row in page.get("lines", [])}
    originals = [indexed.get(row["line"]) for row in window]
    if any(
        not original or original["text"] != row["text"]
        for original, row in zip(originals, window)
    ):
        return None
    amounts = [amount for row in originals for amount in row.get("amounts", [])]
    if len(amounts) != 2 or any(not amount.get("bbox") for amount in amounts):
        return None
    left, right = [amount["bbox"] for amount in amounts]
    if (
        left[2] >= right[0]
        or abs(left[1] - right[1]) > 1
        or abs(left[3] - right[3]) > 1
    ):
        return None
    anchors = [
        row
        for row in page.get("lines", [])
        if len(row.get("amounts", [])) == 2
        and all(
            amount.get("bbox") and abs(amount["bbox"][2] - box[2]) <= 4
            for amount, box in zip(row["amounts"], (left, right))
        )
    ]
    if len(anchors) < 2:
        return None
    return {
        "method": "split_text_same_pdf_row_two_columns/1",
        "page": window[0]["page"],
        "pdf_sha256": layout.get("pdf_sha256"),
        "amounts": amounts,
        "anchors": [{"line": row["line"], "quote": row["text"]} for row in anchors[:3]],
    }


def sparse_column(window, section, layout, year, period_end=None):
    """Resolve a single printed amount against an explicit two-column grid."""
    proof = _column_grid(window, section, layout, year, period_end)
    if not proof:
        return None
    rows = {r["line"]: r for r in layout["pages"][str(proof["page"])]["lines"]}
    amounts = [a for row in window for a in rows[row["line"]]["amounts"]]
    if len(amounts) != 1 or not amounts[0]["bbox"]:
        return None
    hits = [
        col
        for col in (0, 1)
        if abs(amounts[0]["bbox"][2] - proof["column_right_edges"][col]) <= 3
    ]
    if len(hits) != 1:
        return None
    marker = None
    for item in window:
        original = rows[item["line"]]
        glyphs = original.get("cell_glyphs", [])
        joined = "".join(g["text"] for g in glyphs)
        if "不适用" not in joined:
            continue
        start = joined.index("不适用")
        boxes = [g.get("bbox") for g in glyphs[start : start + 3]]
        if len(boxes) != 3 or not all(boxes):
            return None
        other = 1 - hits[0]
        mid = (proof["column_right_edges"][0] + proof["column_left_edges"][1]) / 2
        if (
            any((b[0] + b[2]) / 2 < mid for b in boxes)
            if other == 1
            else any((b[0] + b[2]) / 2 >= mid for b in boxes)
        ):
            return None
        marker = ("current", "comparative")[other]
    return {
        **proof,
        "column": ("current", "comparative")[hits[0]],
        "amount": amounts[0],
        **({"non_numeric_column": marker} if marker else {}),
    }


def blank_current_cell(window, section, layout, year, period_end=None):
    """Prove no printed numeric glyph or image occupies the current cell.

    This is a disclosure-layout observation, never evidence that economic value
    is zero. A scanned/overlaid cell, missing boxes or ambiguous grid stays open.
    """
    proof = _column_grid(window, section, layout, year, period_end)
    if not proof:
        return None
    page = layout["pages"][str(proof["page"])]
    if page.get("image_bounds") is None:
        return None
    index = {row["line"]: row for row in page["lines"]}
    rows = [index[row["line"]] for row in window]
    if any(not row.get("bbox") or "cell_glyphs" not in row for row in rows):
        return None
    # The target label must end before the financial columns. A sparse printed
    # comparative value is handled separately with its existing amount proof.
    left = proof["column_left_edges"][0] - 3
    right = (proof["column_right_edges"][0] + proof["column_left_edges"][1]) / 2
    label_rows = [row for row in rows if not row["amounts"]]
    if any(row["bbox"][2] >= left for row in label_rows):
        return None
    bottom = min(row["bbox"][1] for row in rows)
    top = max(row["bbox"][3] for row in rows)
    cell = [left, bottom - 0.5, right, top + 0.5]

    def intersects(box):
        return (
            box
            and box[0] < cell[2]
            and box[2] > cell[0]
            and box[1] < cell[3]
            and box[3] > cell[1]
        )

    if any(intersects(bounds) for bounds in page["image_bounds"]):
        return None
    if any(
        intersects(glyph.get("bbox"))
        for row in page["lines"]
        for glyph in row.get("cell_glyphs", [])
        if glyph["text"] not in "-−－—–/"
    ):
        return None
    return {
        **proof,
        "current_cell_bbox": cell,
        "row_boxes": [row["bbox"] for row in rows],
        "method": "pdf_numeric_glyph_and_image_cell_check/1",
        "meaning": "printed current cell has no numeric disclosure; not a numeric zero",
    }
