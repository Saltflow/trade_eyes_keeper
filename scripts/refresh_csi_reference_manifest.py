#!/usr/bin/env python3
"""Create an auditable current CSI constituent manifest (300 + 500 by default).

The official constituent workbooks identify *current* membership only.  The
output deliberately records that limitation so a historical backtest cannot
silently treat today\'s members as historical index membership.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import date, datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.data.reference_universe_backfill import REFERENCE_MANIFEST_CONTRACT

OFFICIAL_INDEX_SPECS: dict[str, dict[str, str]] = {
    "csi_300": {
        "index_code": "000300",
        "name": "沪深300",
        "name_en": "CSI 300",
        "reference_etf": "510300",
        "constituent_source": (
            "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/"
            "file/autofile/cons/000300cons.xls"
        ),
    },
    "csi_500": {
        "index_code": "000905",
        "name": "中证500",
        "name_en": "CSI 500",
        "reference_etf": "510500",
        "constituent_source": (
            "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/"
            "file/autofile/cons/000905cons.xls"
        ),
    },
    "csi_1000": {
        "index_code": "000852",
        "name": "中证1000",
        "name_en": "CSI 1000",
        "constituent_source": (
            "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/"
            "file/autofile/cons/000852cons.xls"
        ),
    },
}
DEFAULT_INDICES = ("csi_300", "csi_500")
EXPECTED_CONSTITUENT_COUNTS = {"csi_300": 300, "csi_500": 500, "csi_1000": 1000}
DEFAULT_OUTPUT_DIRECTORY = ROOT / "data" / "reference_universe"


def _selected_indices(indices: Sequence[str]) -> tuple[str, ...]:
    selected = tuple(indices)
    if not selected:
        raise ValueError("select at least one index")
    unknown = set(selected) - set(OFFICIAL_INDEX_SPECS)
    if unknown:
        raise ValueError(f"unsupported index keys: {sorted(unknown)}")
    if len(set(selected)) != len(selected):
        raise ValueError("index selection contains duplicates")
    return tuple(key for key in OFFICIAL_INDEX_SPECS if key in selected)


def _column(
    frame: pd.DataFrame, fragment: str, *, exclude: tuple[str, ...] = ()
) -> str:
    matches = [
        str(column)
        for column in frame.columns
        if fragment.lower() in str(column).lower()
        and all(item.lower() not in str(column).lower() for item in exclude)
    ]
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one workbook column containing {fragment!r}; "
            f"got {matches!r}"
        )
    return matches[0]


def _code(value: object) -> str:
    if pd.isna(value):
        raise ValueError("constituent workbook contains an empty code")
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    if not text.isdigit():
        raise ValueError(f"invalid constituent code: {value!r}")
    code = text.zfill(6)
    if len(code) != 6:
        raise ValueError(f"constituent code is not six digits: {value!r}")
    return code


def _as_of_date(value: object) -> date:
    if pd.isna(value):
        raise ValueError("constituent workbook contains an empty effective date")
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    if len(text) == 8 and text.isdigit():
        return date(int(text[:4]), int(text[4:6]), int(text[6:]))
    parsed = pd.Timestamp(value)
    if pd.isna(parsed):
        raise ValueError(f"invalid constituent effective date: {value!r}")
    return parsed.date()


def _exchange(value: object) -> str:
    text = str(value or "").lower()
    if "shanghai" in text or "上海" in text:
        return "sse"
    if "shenzhen" in text or "深圳" in text:
        return "szse"
    if "beijing" in text or "北京" in text:
        return "bjse"
    raise ValueError(f"unrecognized A-share exchange: {value!r}")


def parse_constituent_frame(
    frame: pd.DataFrame,
    *,
    index_key: str,
    expected_count: int,
    expected_as_of: date | None = None,
) -> tuple[date, list[dict[str, str]]]:
    """Parse one official workbook into normalized issuer rows."""

    if index_key not in OFFICIAL_INDEX_SPECS:
        raise ValueError(f"unsupported index key: {index_key}")
    if len(frame) != expected_count:
        raise ValueError(
            f"{index_key} constituent count is {len(frame)}, expected {expected_count}"
        )
    date_column = _column(frame, "Date")
    code_column = _column(frame, "Constituent Code")
    name_column = _column(frame, "Constituent Name", exclude=("(Eng)",))
    name_en_column = _column(frame, "Constituent Name(Eng)")
    exchange_column = _column(frame, "Exchange(Eng)")
    dates = {_as_of_date(value) for value in frame[date_column]}
    if len(dates) != 1:
        raise ValueError(f"{index_key} has multiple constituent effective dates")
    as_of = next(iter(dates))
    if expected_as_of is not None and as_of != expected_as_of:
        raise ValueError(
            f"{index_key} effective date is {as_of}, expected {expected_as_of}"
        )
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for _, row in frame.iterrows():
        code = _code(row[code_column])
        if code in seen:
            raise ValueError(f"{index_key} contains duplicate code {code}")
        seen.add(code)
        name = str(row[name_column] or "").strip()
        name_en = str(row[name_en_column] or "").strip()
        if not name or name.lower() == "nan":
            raise ValueError(f"{index_key} has no Chinese name for {code}")
        result.append(
            {
                "code": code,
                "name": name,
                "name_en": "" if name_en.lower() == "nan" else name_en,
                "exchange": _exchange(row[exchange_column]),
            }
        )
    return as_of, result


def build_manifest(
    frames: Mapping[str, pd.DataFrame],
    sources: Mapping[str, Mapping[str, object]],
    *,
    indices: Sequence[str] = DEFAULT_INDICES,
    expected_as_of: date | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Combine official workbooks without inventing historical membership."""

    selected = _selected_indices(indices)
    required = set(selected)
    if set(frames) != required or set(sources) != required:
        raise ValueError(
            "frames and sources must contain exactly the selected indices: "
            + ", ".join(selected)
        )
    issuer_rows: dict[str, dict[str, Any]] = {}
    as_of_dates: set[date] = set()
    index_rows: list[dict[str, object]] = []
    for index_key in selected:
        expected_count = EXPECTED_CONSTITUENT_COUNTS[index_key]
        as_of, rows = parse_constituent_frame(
            frames[index_key],
            index_key=index_key,
            expected_count=expected_count,
            expected_as_of=expected_as_of,
        )
        as_of_dates.add(as_of)
        source = dict(sources[index_key])
        spec = OFFICIAL_INDEX_SPECS[index_key]
        index_rows.append(
            {
                "key": index_key,
                "index_code": spec["index_code"],
                "name": spec["name"],
                "name_en": spec["name_en"],
                "reference_etf": spec.get("reference_etf"),
                "member_as_of": as_of.isoformat(),
                "weight_as_of": None,
                "constituent_count": expected_count,
                "constituent_source": spec["constituent_source"],
                "constituent_source_sha256": source["sha256"],
                "constituent_source_headers": {
                    key: source.get(key)
                    for key in ("etag", "last_modified", "content_length")
                    if source.get(key) is not None
                },
            }
        )
        if source.get("raw_file") is not None:
            index_rows[-1]["constituent_source_file"] = source["raw_file"]
        if source.get("downloaded_at") is not None:
            index_rows[-1]["constituent_source_downloaded_at"] = source["downloaded_at"]
        for row in rows:
            existing = issuer_rows.get(row["code"])
            if existing is None:
                issuer_rows[row["code"]] = {
                    **row,
                    "memberships": [index_key],
                }
                continue
            if (
                existing["name"] != row["name"]
                or existing["exchange"] != row["exchange"]
            ):
                raise ValueError(
                    f"conflicting issuer identity across workbooks: {row['code']}"
                )
            existing["memberships"].append(index_key)
    if len(as_of_dates) != 1:
        raise ValueError("selected indices' effective dates do not match")
    as_of = next(iter(as_of_dates))
    created = generated_at or datetime.now(timezone.utc).astimezone()
    return {
        "contract": REFERENCE_MANIFEST_CONTRACT,
        "generated_at": created.isoformat(),
        "as_of": as_of.isoformat(),
        "weight_as_of": None,
        "membership_semantics": "current_as_of_only_not_historical_membership",
        "purpose": "ke_low_vol_current_universe_research",
        "indices": index_rows,
        "company_count": len(issuer_rows),
        "companies": [issuer_rows[code] for code in sorted(issuer_rows)],
    }


def _atomic_write_bytes(path: Path, content: bytes, *, overwrite: bool = False) -> None:
    """Publish a complete file, without replacing an existing path by default."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            os.replace(temporary, path)
        else:
            # Creating a link fails if the destination appeared concurrently.
            os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _archive_workbook(
    directory: Path, url: str, content: bytes, content_hash: str
) -> Path:
    source_name = Path(urlsplit(url).path)
    archived = directory / f"{source_name.stem}_{content_hash}{source_name.suffix}"
    try:
        _atomic_write_bytes(archived, content)
    except FileExistsError:
        if archived.read_bytes() != content:
            raise ValueError(
                f"archived workbook does not match its SHA-256: {archived}"
            )
    return archived.resolve()


def _download_workbook(
    url: str,
    timeout_seconds: int,
    *,
    archive_directory: Path | None = None,
) -> tuple[pd.DataFrame, dict[str, object]]:
    response = requests.get(url, timeout=timeout_seconds)
    response.raise_for_status()
    content = bytes(response.content)
    source: dict[str, object] = {
        "sha256": hashlib.sha256(content).hexdigest(),
        "etag": response.headers.get("ETag"),
        "last_modified": response.headers.get("Last-Modified"),
        "content_length": len(content),
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
    }
    if archive_directory is not None:
        archived = _archive_workbook(
            archive_directory, url, content, str(source["sha256"])
        )
        source["raw_file"] = str(archived)
    frame = pd.read_excel(BytesIO(content), dtype=object)
    return frame, source


def _atomic_write(path: Path, payload: dict[str, Any], overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite existing manifest: {path}")
    _atomic_write_bytes(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        overwrite=overwrite,
    )


def _manifest_filename(as_of: str, indices: Sequence[str]) -> str:
    selected = _selected_indices(indices)
    suffix = "" if selected == DEFAULT_INDICES else "_" + "_".join(selected)
    return f"index_constituents_{as_of.replace('-', '')}{suffix}.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIRECTORY,
        help="manifest and raw-workbook directory; non-default selections get an index suffix",
    )
    parser.add_argument(
        "--indices",
        nargs="+",
        choices=tuple(OFFICIAL_INDEX_SPECS),
        default=DEFAULT_INDICES,
        help="indices to download; defaults to csi_300 csi_500",
    )
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=None,
        help="require this exact official effective date",
    )
    parser.add_argument("--timeout-seconds", type=int, default=60)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    if args.timeout_seconds <= 0:
        raise SystemExit("--timeout-seconds must be positive")
    try:
        selected = _selected_indices(args.indices)
    except ValueError as exc:
        parser.error(str(exc))

    frames: dict[str, pd.DataFrame] = {}
    sources: dict[str, dict[str, object]] = {}
    for index_key in selected:
        spec = OFFICIAL_INDEX_SPECS[index_key]
        print(f"downloading {index_key} official constituents ...", flush=True)
        frame, source = _download_workbook(
            spec["constituent_source"],
            args.timeout_seconds,
            archive_directory=args.output_dir / "raw_workbooks",
        )
        source["raw_file"] = (
            Path(str(source["raw_file"]))
            .relative_to(args.output_dir.resolve())
            .as_posix()
        )
        frames[index_key] = frame
        sources[index_key] = source
    manifest = build_manifest(
        frames, sources, indices=selected, expected_as_of=args.as_of
    )
    output = args.output_dir / _manifest_filename(manifest["as_of"], selected)
    _atomic_write(output, manifest, args.overwrite)
    print(
        json.dumps(
            {
                "manifest": str(output),
                "as_of": manifest["as_of"],
                "company_count": manifest["company_count"],
                "source_hashes": {
                    item["key"]: item["constituent_source_sha256"]
                    for item in manifest["indices"]
                },
                "raw_sources": {
                    item["key"]: item["constituent_source_file"]
                    for item in manifest["indices"]
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
