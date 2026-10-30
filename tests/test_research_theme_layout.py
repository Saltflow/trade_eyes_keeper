"""Theme-first layout and real graph handoff, with network sockets forbidden."""

import base64
import json
import re

import pytest
import yaml

from scripts import research_inputs as inputs
from scripts import research_render as presentation
from scripts import research_report as run
from scripts import research_rounds as rounds
from tests.research_release_support import SOURCES
from tests.research_theme_fixtures import ALL, COMPANIES, bodies, chart_specs, name
from tests.test_research_report import (
    CONFIG,
    Response,
    offline,  # noqa: F401 - autouse fixture
)


def test_same_metric_time_and_denominator_are_shared():
    specs = {s["id"]: s for s in chart_specs()}
    for spec in specs.values():
        assert spec["x"] == ["FY2023", "FY2024", "FY2025"]
        assert all(len(s["values"]) == 3 for s in spec["series"])
    assert specs["engineering_revenue"]["series"] == [
        {"name": name(c), "values": COMPANIES[c]["revenue"]} for c in ALL[:2]
    ]
    assert len(specs["engineering_occupation"]["series"]) == 2
    for i, code in enumerate(ALL):
        c = COMPANIES[code]
        assert specs["cash_intensity"]["series"][i]["values"] == pytest.approx(
            [n / d * 100 for n, d in zip(c["cfo"], c["revenue"])]
        )
        assert specs["restricted_cash"]["series"][i]["values"] == pytest.approx(
            [n / d * 100 for n, d in zip(c["restricted"], c["cash"])]
        )
        assert specs["profit_index"]["series"][i]["values"][0] == 100
    assert specs["payout_cfo"]["series"][0]["values"][0] is None
    assert "机制参照" in specs["cash_intensity"]["series"][2]["name"]
    assert "四川3.97%、隧道5.16%、泸州1.52%" in specs["restricted_cash"]["reading"]


def test_themes_group_tables_charts_and_company_roles():
    draft, review, final = bodies()
    ids = [c["id"] for c in chart_specs()]
    parsed = [yaml.safe_load(block) for block in presentation.CHART.findall(review)]
    assert [p["id"] for p in parsed] == ids
    assert len(parsed) == 12
    headings = [s for s in final.splitlines() if s.startswith("## ")]
    assert not any(any(c["name"] in s for c in COMPANIES.values()) for s in headings)
    assert all(
        t in final
        for t in ["增长与回报", "利润与现金", "占款机制", "资本配置", "估值条件"]
    )
    revenue_rows = [s for s in final.splitlines() if s.startswith("| 营业收入（亿元）")]
    assert len(revenue_rows) == 3
    assert all(name(c) in row for c, row in zip(ALL, revenue_rows))
    assert re.findall(r"assets/review_chart_(\d+)\.png", final) == list(
        map(str, range(1, 13))
    )
    for value in [
        "110.84%",
        "128.67%",
        "78.48%",
        "119.33%",
        "12.75%",
        "13.08%",
        "FY2023",
        "无覆盖意义：CFO为负",
        "市值",
    ]:
        assert value in final
    first = yaml.safe_load(presentation.CHART.findall(draft)[5])
    corrected = parsed[5]
    assert first["id"] == corrected["id"] == "cash_intensity"
    assert first["series"][0]["values"][-1] != corrected["series"][0]["values"][-1]


def test_full_theme_example_and_prompts_reach_all_three_requests(tmp_path):
    sources = SOURCES
    folder = tmp_path / "thematic"
    plan = inputs.prepare(sources, folder, CONFIG, budget=2)
    replies, calls = bodies(), []
    config = run.io.load(CONFIG)
    example = (inputs.REPO / config["prompts"][-1]).read_text(
        encoding="utf8"
    )

    def post(url, **kwargs):
        index = len(calls)
        value = json.loads(kwargs["data"])
        system = value["messages"][0]["content"]
        assert example in system
        assert (inputs.REPO / config["rounds"][index]["prompt"]).read_text(
            encoding="utf8"
        ) in system
        assert "同一指标" in system and "本轮公司清单" in system
        assert "工程两家深入" not in system
        content = value["messages"][1]["content"]
        images = (
            []
            if isinstance(content, str)
            else [p for p in content if p["type"] == "image_url"]
        )
        assert len(images) == (0 if index == 0 else 12)
        for part, (label, path) in zip(
            images, rounds.images(folder, plan, index).items()
        ):
            assert (
                base64.b64decode(part["image_url"]["url"].split(",", 1)[1])
                == path.read_bytes()
            )
            assert label in str(content)
        calls.append(value)
        return Response(replies[index], False)

    result = run.execute(folder, post=post, key="offline-theme-only")
    assert result["simulated"] and result["requests_sent"] == 3
    assert not result["presentation"]["missing_image_references"]
    assert (folder / "正文.md").read_bytes() == replies[2].encode()
    assert run.io.sha(folder / "views/draft/assets/chart_6.png") != run.io.sha(
        folder / "views/review/assets/chart_6.png"
    )
    for i in range(1, 13):
        assert (folder / "assets" / f"review_chart_{i}.png").read_bytes() == (
            folder / "views/review/assets" / f"chart_{i}.png"
        ).read_bytes()
