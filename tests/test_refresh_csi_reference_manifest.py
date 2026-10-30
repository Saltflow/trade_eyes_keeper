"""Tests for current CSI constituent-manifest parsing without network access."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from scripts import refresh_csi_reference_manifest as script
from scripts.refresh_csi_reference_manifest import (
    DEFAULT_INDICES,
    OFFICIAL_INDEX_SPECS,
    build_manifest,
    parse_constituent_frame,
)


def _frame(start: int, count: int, as_of: str = "20260918") -> pd.DataFrame:
    rows = []
    for offset in range(count):
        code = f"{start + offset:06d}"
        rows.append(
            {
                "日期Date": as_of,
                "成份券代码Constituent Code": code,
                "成份券名称Constituent Name": f"公司{code}",
                "成份券英文名称Constituent Name(Eng)": f"Company {code}",
                "交易所英文名称Exchange(Eng)": (
                    "Shanghai Stock Exchange"
                    if offset % 2
                    else "Shenzhen Stock Exchange"
                ),
            }
        )
    return pd.DataFrame(rows)


def _sources(indices=DEFAULT_INDICES) -> dict[str, dict[str, object]]:
    return {
        key: {
            "sha256": f"hash-{key}",
            "etag": f"etag-{key}",
            "last_modified": "Fri, 18 Sep 2026 09:00:00 GMT",
            "content_length": 123,
        }
        for key in indices
    }


def test_parse_constituents_preserves_code_date_and_exchange():
    frame = _frame(1, 3)

    as_of, rows = parse_constituent_frame(
        frame,
        index_key="csi_300",
        expected_count=3,
        expected_as_of=date(2026, 9, 18),
    )

    assert as_of == date(2026, 9, 18)
    assert rows[0] == {
        "code": "000001",
        "name": "公司000001",
        "name_en": "Company 000001",
        "exchange": "szse",
    }
    assert rows[1]["exchange"] == "sse"


def test_parse_rejects_mixed_effective_dates():
    frame = _frame(1, 3)
    frame.loc[1, "日期Date"] = "20260917"

    with pytest.raises(ValueError, match="multiple constituent effective dates"):
        parse_constituent_frame(frame, index_key="csi_300", expected_count=3)


def test_manifest_combines_membership_and_records_source_identity():
    manifest = build_manifest(
        {"csi_300": _frame(1, 300), "csi_500": _frame(301, 500)},
        _sources(),
        expected_as_of=date(2026, 9, 18),
        generated_at=datetime(2026, 9, 19, tzinfo=timezone.utc),
    )

    assert manifest["as_of"] == "2026-09-18"
    assert manifest["company_count"] == 800
    assert manifest["membership_semantics"] == (
        "current_as_of_only_not_historical_membership"
    )
    assert manifest["companies"][0]["memberships"] == ["csi_300"]
    assert manifest["companies"][-1]["memberships"] == ["csi_500"]
    assert manifest["indices"][0]["constituent_source_sha256"] == "hash-csi_300"
    assert manifest["indices"][1]["weight_as_of"] is None


def test_csi1000_manifest_requires_explicit_selection_and_1000_members():
    frames = {"csi_1000": _frame(1, 1000)}
    sources = _sources(("csi_1000",))
    sources["csi_1000"].update(
        raw_file="raw_workbooks/000852cons_example.xls",
        downloaded_at="2026-09-21T00:00:00+00:00",
    )
    manifest = build_manifest(frames, sources, indices=("csi_1000",))

    assert manifest["company_count"] == 1000
    assert manifest["membership_semantics"] == (
        "current_as_of_only_not_historical_membership"
    )
    index = manifest["indices"][0]
    assert index["key"] == "csi_1000"
    assert index["index_code"] == "000852"
    assert index["constituent_count"] == 1000
    assert index["constituent_source"].endswith("/000852cons.xls")
    assert index["constituent_source_file"] == sources["csi_1000"]["raw_file"]
    assert (
        index["constituent_source_downloaded_at"]
        == sources["csi_1000"]["downloaded_at"]
    )
    assert all(
        company["memberships"] == ["csi_1000"] for company in manifest["companies"]
    )
    with pytest.raises(ValueError, match="selected indices"):
        build_manifest(frames, sources)
    with pytest.raises(ValueError, match="expected 1000"):
        build_manifest({"csi_1000": _frame(1, 999)}, sources, indices=("csi_1000",))


def test_all_three_indices_can_be_selected_in_canonical_order():
    selected = ("csi_1000", "csi_500", "csi_300")
    manifest = build_manifest(
        {
            "csi_300": _frame(1, 300),
            "csi_500": _frame(301, 500),
            "csi_1000": _frame(801, 1000),
        },
        _sources(selected),
        indices=selected,
    )
    assert manifest["company_count"] == 1800
    assert [item["key"] for item in manifest["indices"]] == [
        "csi_300",
        "csi_500",
        "csi_1000",
    ]


@pytest.mark.parametrize("indices", [(), ("csi_1000", "csi_1000"), ("unknown",)])
def test_invalid_index_selection_is_rejected(indices):
    with pytest.raises(ValueError):
        build_manifest({}, {}, indices=indices)


def test_selected_indices_still_require_matching_effective_dates():
    with pytest.raises(ValueError, match="effective dates do not match"):
        build_manifest(
            {
                "csi_300": _frame(1, 300),
                "csi_1000": _frame(301, 1000, as_of="20260917"),
            },
            _sources(("csi_300", "csi_1000")),
            indices=("csi_300", "csi_1000"),
        )


def _mock_downloads(monkeypatch, frames):
    """Mock HTTP and Excel decoding, retaining the exact response bytes on disk."""
    requested = []
    payloads = {key: f"official-workbook-{key}".encode() for key in frames}
    by_url = {OFFICIAL_INDEX_SPECS[key]["constituent_source"]: key for key in frames}

    def get(url, *, timeout):
        assert timeout == 60
        requested.append(url)
        key = by_url[url]
        return SimpleNamespace(
            content=payloads[key],
            headers={"ETag": f"etag-{key}", "Last-Modified": "source-date"},
            raise_for_status=lambda: None,
        )

    def read_excel(buffer, *, dtype):
        assert dtype is object
        content = buffer.getvalue()
        key = next(key for key, payload in payloads.items() if payload == content)
        return frames[key].copy()

    monkeypatch.setattr(script.requests, "get", get)
    monkeypatch.setattr(script.pd, "read_excel", read_excel)
    return requested, payloads


def test_cli_default_stays_csi300_and_csi500_with_original_filename(
    tmp_path, monkeypatch
):
    requested, payloads = _mock_downloads(
        monkeypatch, {"csi_300": _frame(1, 300), "csi_500": _frame(301, 500)}
    )
    monkeypatch.setattr("sys.argv", ["refresh", "--output-dir", str(tmp_path)])

    assert script.main() == 0

    output = tmp_path / "index_constituents_20260918.json"
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert requested == [
        OFFICIAL_INDEX_SPECS[key]["constituent_source"] for key in DEFAULT_INDICES
    ]
    assert manifest["company_count"] == 800
    for item in manifest["indices"]:
        source = tmp_path / item["constituent_source_file"]
        assert not Path(item["constituent_source_file"]).is_absolute()
        assert source.read_bytes() == payloads[item["key"]]
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        assert item["constituent_source_sha256"] == digest
        assert digest in source.name


def test_cli_csi1000_retains_raw_source_without_overwriting_historical_manifest(
    tmp_path, monkeypatch
):
    previous = tmp_path / "index_constituents_20260918.json"
    previous.write_bytes(b"existing-300-500-manifest")
    requested, payloads = _mock_downloads(monkeypatch, {"csi_1000": _frame(1, 1000)})
    monkeypatch.setattr(
        "sys.argv",
        ["refresh", "--output-dir", str(tmp_path), "--indices", "csi_1000"],
    )

    assert script.main() == 0

    output = tmp_path / "index_constituents_20260918_csi_1000.json"
    saved = output.read_bytes()
    manifest = json.loads(saved)
    assert requested == [OFFICIAL_INDEX_SPECS["csi_1000"]["constituent_source"]]
    assert previous.read_bytes() == b"existing-300-500-manifest"
    raw_source = tmp_path / manifest["indices"][0]["constituent_source_file"]
    assert raw_source.read_bytes() == payloads["csi_1000"]
    assert manifest["company_count"] == 1000
    assert manifest["membership_semantics"] == (
        "current_as_of_only_not_historical_membership"
    )

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        script.main()
    assert output.read_bytes() == saved
    assert previous.read_bytes() == b"existing-300-500-manifest"
    assert list((tmp_path / "raw_workbooks").iterdir()) == [raw_source]


def test_raw_archive_is_immutable_and_keeps_different_source_versions(tmp_path):
    url = OFFICIAL_INDEX_SPECS["csi_1000"]["constituent_source"]
    original = b"first-source-version"
    changed = b"second-source-version"
    digest = hashlib.sha256(original).hexdigest()
    first = script._archive_workbook(tmp_path, url, original, digest)
    assert script._archive_workbook(tmp_path, url, original, digest) == first
    second = script._archive_workbook(
        tmp_path, url, changed, hashlib.sha256(changed).hexdigest()
    )
    assert first != second
    assert first.read_bytes() == original
    assert second.read_bytes() == changed

    first.write_bytes(b"corrupt-existing-source")
    with pytest.raises(ValueError, match="does not match its SHA-256"):
        script._archive_workbook(tmp_path, url, original, digest)
    assert first.read_bytes() == b"corrupt-existing-source"
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_manifest_publish_does_not_replace_a_concurrent_writer(
    tmp_path, monkeypatch
):
    output = tmp_path / "manifest.json"
    real_link = script.os.link

    def concurrent_link(source, destination):
        Path(destination).write_bytes(b"other-writers-manifest")
        real_link(source, destination)

    monkeypatch.setattr(script.os, "link", concurrent_link)
    with pytest.raises(FileExistsError):
        script._atomic_write(output, {"new": "manifest"}, overwrite=False)
    assert output.read_bytes() == b"other-writers-manifest"
    assert not list(tmp_path.glob("*.tmp"))


def test_existing_explicit_overwrite_option_remains_supported(tmp_path):
    output = tmp_path / "manifest.json"
    output.write_text('{"old": true}', encoding="utf-8")
    script._atomic_write(output, {"new": True}, overwrite=True)
    assert json.loads(output.read_text(encoding="utf-8")) == {"new": True}


def test_cli_rejects_duplicate_selection_before_any_download(tmp_path, monkeypatch):
    requested, _payloads = _mock_downloads(monkeypatch, {"csi_1000": _frame(1, 1000)})
    monkeypatch.setattr(
        "sys.argv",
        [
            "refresh",
            "--output-dir",
            str(tmp_path),
            "--indices",
            "csi_1000",
            "csi_1000",
        ],
    )
    with pytest.raises(SystemExit) as error:
        script.main()
    assert error.value.code == 2
    assert not requested
    assert not list(tmp_path.iterdir())
