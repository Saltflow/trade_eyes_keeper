"""Seven-year form and explicit valuation sample, never financial acceptance gates."""

import base64
import json

import pytest

from scripts import research_inputs as inputs
from scripts import research_report as run
from scripts import research_rounds as rounds
from tests import research_theme_fixtures as old
from tests.research_release_support import SOURCES
from tests.research_valuation_fixtures import (
    ASSUMPTIONS,
    FUTURE,
    YEARS,
    bodies,
    chart_specs,
    maximum_price,
    scenario,
    solve,
)
from tests.test_research_report import CONFIG, Response, offline  # noqa: F401


def test_seven_year_axes_preserve_every_existing_number_and_explicit_gaps():
    specs = chart_specs()
    assert len(YEARS) == 7 and len(FUTURE) == 5 and len(specs) == 14
    for previous, current in zip(old.chart_specs(), specs):
        assert current["x"] == YEARS
        for before, after in zip(previous["series"], current["series"]):
            assert after["values"] == [None] * 4 + before["values"]
    for raw in bodies():
        tables = [
            line for line in raw.splitlines() if line.startswith("| 同一指标/单位")
        ]
        assert len(tables) >= 4
        assert all(all(y in line for y in YEARS) for line in tables)
        revenue = [
            line for line in raw.splitlines() if line.startswith("| 营业收入（亿元）")
        ]
        assert len(revenue) == 3
        assert all(line.count("本轮未取得") == 4 for line in revenue)
        assert "FY2023=100" in raw and "当前三年端点仅两个" in raw
        assert all(
            any(f"{n:,.2f}" in row for row in revenue)
            for n in [1150.42, 741.93, 302.33]
        )
    assert specs[12]["x"] == FUTURE
    assert all("虚构U" in s["title"] for s in specs[12:])


def test_fictional_cash_equity_and_hurdle_bridge_has_no_double_count():
    rows = scenario(*ASSUMPTIONS["基准"])
    first = rows[0]
    assert first["revenue"] == 105
    assert first["working_capital"] == 1
    assert first["cfo"] == pytest.approx(11.6)
    assert first["fcfe"] == pytest.approx(8.45)
    assert first["equity"] == pytest.approx(102.05)
    assert first["roe"] == pytest.approx(10.5 / 101.025 * 100)
    assert sum(r["profit"] - r["fcfe"] for r in rows) == pytest.approx(
        rows[-1]["equity"] - 100
    )
    assert sum(
        r["capex"] - r["depreciation"] + r["working_capital"] for r in rows
    ) == pytest.approx(rows[-1]["equity"] - 100)
    cashflows = [r["dps"] for r in rows]
    cashflows[-1] += rows[-1]["eps"] * 10
    irr = solve(lambda rate: maximum_price(rows, rate), 10, -0.5, 0.8)
    assert sum(
        cf / (1 + irr) ** t for t, cf in enumerate(cashflows, 1)
    ) == pytest.approx(10)
    assert (
        maximum_price(rows, 0.08)
        > maximum_price(rows, 0.10)
        > maximum_price(rows, 0.12)
    )
    growth = solve(lambda g: maximum_price(scenario(g, 0.10), 0.10), 10, -0.05, 0.15)
    assert maximum_price(scenario(growth, 0.10), 0.10) == pytest.approx(10)
    assert 0 < growth < 0.05
    assert (
        maximum_price(rows, 0.10, 8)
        < maximum_price(rows, 0.10, 10)
        < maximum_price(rows, 0.10, 12)
    )


def test_final_layout_separates_missing_company_forecasts_from_fictional_sample():
    final = bodies()[2]
    assert "真实公司：预测位置保留，缺口明确" in final
    assert "纯虚构形式算例，不属于三家公司" in final
    assert final.index("图13：") < final.index("首年FCFE收益率") < final.index("图14：")
    assert final.index("图14：") < final.index("## 八、反证与重看")
    assert "[[" not in final
    assert final.count("assets/review_chart_") == 14
    assert not any(
        any(old.name(c) in heading for c in old.ALL)
        for heading in final.splitlines()
        if heading.startswith("## ")
    )
    assert "不是用户指定门槛" in final and "最高买价(h)" in final
    assert "不是ROE，也不是五年IRR" in final


def test_all_rounds_receive_complete_seven_year_valuation_example_and_actual_pngs(
    tmp_path,
):
    sources = SOURCES
    folder = tmp_path / "valuation_form"
    plan = inputs.prepare(sources, folder, CONFIG, budget=2)
    replies, calls = bodies(), []
    example = (inputs.REPO / run.io.load(CONFIG)["prompts"][-1]).read_text(
        encoding="utf8"
    )
    valuation_prompt = (inputs.REPO / run.io.load(CONFIG)["prompts"][1]).read_text(
        encoding="utf8"
    )

    def post(url, **kwargs):
        index = len(calls)
        request = json.loads(kwargs["data"])
        assert example in request["messages"][0]["content"]
        assert valuation_prompt in request["messages"][0]["content"]
        content = request["messages"][1]["content"]
        images = (
            []
            if isinstance(content, str)
            else [p for p in content if p["type"] == "image_url"]
        )
        assert len(images) == (0 if index == 0 else 14)
        for part, path in zip(images, rounds.images(folder, plan, index).values()):
            assert (
                base64.b64decode(part["image_url"]["url"].split(",", 1)[1])
                == path.read_bytes()
            )
        calls.append(request)
        return Response(replies[index], False)

    result = run.execute(folder, post=post, key="offline-simulation-only")
    assert result["simulated"] and result["requests_sent"] == 3
    assert (folder / "最终报告.md").read_bytes() == replies[2].encode()
    assert not result["presentation"]["missing_image_references"]
    assert not result["presentation"]["unreviewed_chart_blocks"]
    for i in range(1, 15):
        assert (folder / "assets" / f"review_chart_{i}.png").read_bytes() == (
            folder / "views/review/assets" / f"chart_{i}.png"
        ).read_bytes()
