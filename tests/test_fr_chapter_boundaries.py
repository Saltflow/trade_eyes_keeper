"""Regression against cached originals: complete chapters, never TOC fragments."""
import gzip
import json
from pathlib import Path

import pytest

from src.fundamental_data.disclosure import locate_sections

FIXTURES = Path(__file__).parent / "fixtures" / "fr_chapter_boundaries"


def load(name):
    return json.loads(gzip.decompress((FIXTURES / (name + ".json.gz")).read_bytes()))


@pytest.mark.parametrize("name,start,last", [
    ("000498_2019-12-31", 19, 48),
    ("601800_2024-12-31", 24, 66),
    ("601800_2025-12-31", 24, 62),
    ("601800_2026-06-30", 16, 42),
])
def test_real_management_chapter_is_complete(name, start, last):
    source = load(name)
    sections = locate_sections(source["pages"])
    mda = [s for s in sections if s["kind"] == "mda"]
    assert len(mda) == 1
    section = mda[0]
    assert min(section["pages"]) == start
    assert max(section["pages"]) >= last
    assert len(section["text"]) > 10000
    assert set(range(start, last + 1)) <= set(section["pages"])
    # Preserve whole body text and its source pages, including footnotes/tables.
    for page in range(start + 1, last + 1):
        for line in source["pages"][str(page)].splitlines():
            assert line in section["text"]
    assert any(s["kind"] == "policy_block" for s in sections)
    assert any(s["kind"].startswith("other_notes") for s in sections)


def test_toc_without_body_is_not_a_retrieved_chapter():
    sections = locate_sections({"1": "第一节 公司简介\n第二节 管理层讨论与分析\n第三节 财务报告"})
    assert not [s for s in sections if s["kind"] == "mda"]


@pytest.mark.parametrize("name", [
    "000498_2019-12-31", "601800_2024-12-31",
    "601800_2025-12-31", "601800_2026-06-30",
])
def test_chapter_repair_preserves_policy_and_note_spans(name, monkeypatch):
    from src.fundamental_data import disclosure as fr_pack
    pages = load(name)["pages"]
    current = locate_sections(pages)
    monkeypatch.setattr(fr_pack, "_chapter_heads", lambda rows: [
        (i, t.strip()) for i, (_, _, t) in enumerate(rows)
        if fr_pack.CHAPTER.fullmatch(t.strip())
    ])
    previous = locate_sections(pages)
    def notes(sections):
        return [s for s in sections if "policy" in s["kind"] or "notes" in s["kind"]]
    assert notes(current) == notes(previous)
