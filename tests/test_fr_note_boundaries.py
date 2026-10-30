"""Original-heading regression: no tax block swallowing following notes."""

import json
from pathlib import Path

import pytest

from src.fundamental_data.disclosure import locate_sections, records


@pytest.mark.parametrize(
    "case",
    json.loads(
        (
            Path(__file__).parent
            / "fixtures/fundamental_quant/cccc_note_boundaries_20261022.json"
        ).read_text(encoding="utf-8")
    ),
)
def test_actual_tax_boundaries_preserve_source_lines(case):
    sections = locate_sections(case["pages"])
    taxes = [
        s for s in sections if s["kind"] == "other_notes_block" and "税项" in s["title"]
    ]
    assert len(taxes) == 1
    tax = taxes[0]
    assert len(tax["text"]) < case["tax_max_chars"]
    assert max(tax["pages"]) <= case["tax_next_section_page"]
    assert "税收优惠" in tax["text"]
    assert "合并财务报表主要项目注释" not in tax["text"]
    assert "货币资金" not in tax["text"]
    original = records(case["pages"])
    assert tax["text"] == "\n".join(r[2] for r in original[tax["start"] : tax["end"]])


def test_continued_major_heading_keeps_whole_policy_across_pages():
    pages = {
        "1": "三、重要会计政策\n1. 收入\n保留原句。",
        "2": "三、重要会计政策(续)\n收入续表不删\n2. 存货\n加权平均。",
        "3": "四、税项\n2. 税收优惠\n完整优惠条件。\n五、合并财务报表主要项目注释\n1. 货币资金\n余额。",
    }
    sections = locate_sections(pages)
    policy = [s for s in sections if s["kind"] == "policy_block"]
    assert len(policy) == 1 and "存货" in policy[0]["text"]
    assert "收入续表不删" in policy[0]["text"]
    income = next(s for s in sections if s["kind"] == "policy" and "收入" in s["title"])
    assert "收入续表不删" in income["text"]


def test_unknown_peer_major_heading_stops_tax_even_without_classification():
    sections = locate_sections(
        {"1": "四、 税项\n税收优惠原句。\n五、独立的未知附注标题\n不能属于税项的金额。"}
    )
    tax = next(s for s in sections if s["kind"] == "other_notes_block")
    assert "税收优惠原句" in tax["text"]
    assert "不能属于税项" not in tax["text"]


def test_asset_rollforward_groups_are_nested_inside_amount_notes():
    sections = locate_sections({
        "1": "七、合并财务报表项目注释\n15、投资性房地产\n一、账面原值\n原值。\n二、累计折旧和累计摊销\n折旧。",
        "2": "三、减值准备\n减值。\n四、账面价值\n净值。\n16、固定资产\n本期新增。",
        "3": "八、独立的未知附注标题\n下一大节。",
    })
    block = next(s for s in sections if s["kind"] == "notes_block")
    assert "本期新增" in block["text"]
    assert "下一大节" not in block["text"]
