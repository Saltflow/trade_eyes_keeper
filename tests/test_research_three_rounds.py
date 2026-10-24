"""Offline acceptance of the real three-round path and explicit failure cases."""

import base64
import json
import re
from decimal import Decimal

import pytest

from scripts import research_inputs as inputs
from scripts import research_render as presentation
from scripts import research_report as run
from scripts import research_rounds as rounds
from tests.research_release_support import SOURCES
from tests.research_round_fixtures import FIXTURE, bodies
from tests.test_research_report import (
    CHART_DATA,
    CONFIG,
    Response,
    block,
    offline,  # noqa: F401 - autouse fixture
)


def test_actual_three_company_materials_scale_history_and_revised_pngs(tmp_path):
    sources = SOURCES
    folder = tmp_path / "three_companies"
    plan = inputs.prepare(sources, folder, CONFIG, budget=2)
    replies, calls = bodies(), []

    def post(url, **kwargs):
        index = len(calls)
        job = plan["jobs"][index]["id"]
        value = json.loads(kwargs["data"])
        assert (folder / "requests" / f"{job}.freeze.json").is_file()
        content = value["messages"][1]["content"]
        text = (
            content
            if isinstance(content, str)
            else "\n".join(p["text"] for p in content if p["type"] == "text")
        )
        for name in [
            "四川路桥",
            "隧道股份",
            "泸州老窖",
            "FY2023",
            "FY2024",
            "FY2025",
            "403.33",
            "273.42",
        ]:
            assert name in text
        image_parts = (
            []
            if isinstance(content, str)
            else [p for p in content if p["type"] == "image_url"]
        )
        assert len(image_parts) == (12 if index else 0)
        expected = rounds.images(folder, plan, index)
        for part, (label, image) in zip(image_parts, expected.items()):
            assert (
                base64.b64decode(part["image_url"]["url"].split(",", 1)[1])
                == image.read_bytes()
            )
            assert label in text
        if index:
            assert replies[index - 1] in text
        calls.append(value)
        return Response(replies[index], False)

    result = run.execute(folder, post=post, key="offline-only")
    assert result["status"] == "complete" and result["simulated"]
    assert result["requests_sent"] == len(calls) == 3
    assert Decimal(plan["reserved_usd_peak"]) == Decimal("1.4155776")
    body = (folder / "最终报告.md").read_text(encoding="utf8")
    for value in [
        "1.52%",
        "3.97%",
        "5.16%",
        "119.33%",
        "110.84%",
        "市值",
        "FY2023",
        "FY2025",
    ]:
        assert value in body
    assert not result["presentation"]["missing_image_references"]
    assert result["presentation"]["unreviewed_chart_blocks"] == 0
    for i in range(1, 13):
        assert (folder / "assets" / f"review_chart_{i}.png").read_bytes() == (
            folder / "views/review/assets" / f"chart_{i}.png"
        ).read_bytes()
    assert run.io.sha(folder / "views/draft/assets/chart_7.png") != run.io.sha(
        folder / "views/review/assets/chart_7.png"
    )
    assert "72.23" in (folder / "jobs/draft/content.md").read_text(encoding="utf8")
    assert "77.23" in (folder / "图表数据.yaml").read_text(encoding="utf8")
    assert (folder / "正文.md").read_bytes() == replies[2].encode()


def test_late_empty_failure_delivers_last_nonempty_draft_and_stops(tmp_path):
    source = tmp_path / "source.md"
    source.write_text("冻结资料22.51%", encoding="utf8")
    folder = tmp_path / "run"
    inputs.prepare([source], folder, CONFIG, budget=2)
    calls = []

    def post(*args, **kwargs):
        calls.append(1)
        if len(calls) == 3:
            raise run.io.requests.exceptions.ConnectionError("offline failure")
        return Response("# 已有完整稿\n\n22.51%\n\n" + block(CHART_DATA), False)

    result = run.execute(folder, post=post, key="offline")
    assert result["status"] == "failed" and len(calls) == 3
    assert result["delivered_round"] == "review"
    assert "已有完整稿" in (folder / "最终报告.md").read_text(encoding="utf8")
    assert (folder / "jobs/write/content.md").read_bytes() == b""
    assert (folder / "assets/review_chart_1.png").is_file()
    assert result["cost_usd"] is None


def test_failed_plot_residue_is_not_sent_as_image(tmp_path, monkeypatch):
    view = tmp_path / "views/draft"
    view.mkdir(parents=True)

    def broken(data, folder, index):
        (folder / "assets").mkdir(exist_ok=True)
        (folder / "assets/chart_1.png").write_bytes(b"broken png")
        raise RuntimeError("offline plot failure after creation")

    monkeypatch.setattr(presentation, "chart", broken)
    presentation.render(view, "前文\n\n" + block(CHART_DATA) + "后文")
    assert rounds.images(tmp_path, {"jobs": [{"id": "draft"}]}, 1) == {}
    assert "后文" in (view / "最终报告.md").read_text(encoding="utf8")


def test_final_new_chart_is_preserved_without_rendering(tmp_path):
    source = tmp_path / "source.md"
    source.write_text("只读资料", encoding="utf8")
    folder = tmp_path / "run"
    inputs.prepare([source], folder, CONFIG, budget=2)
    raw = "# 模拟最终稿\n\n" + block(CHART_DATA)
    run.execute(folder, post=lambda *a, **k: Response(raw, False), key="offline")
    assert (folder / "正文.md").read_bytes() == raw.encode()
    assert block(CHART_DATA) in (folder / "最终报告.md").read_text(encoding="utf8")
    assert not (folder / "assets/chart_1.png").exists()
    assert run.io.load(folder / "呈现注记.json")["unreviewed_chart_blocks"] == 1


def test_actual_later_request_over_waterline_stops_without_cutting(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.md"
    source.write_text("只读资料", encoding="utf8")
    folder = tmp_path / "run"
    inputs.prepare([source], folder, CONFIG, budget=2)
    original = rounds.request

    def capped(root, plan, index):
        if index == 1:
            plan["input_waterline"] = 1
        return original(root, plan, index)

    monkeypatch.setattr(rounds, "request", capped)
    calls = []

    def post(*a, **k):
        calls.append(1)
        return Response("# 已返回原文22.51%", False)

    result = run.execute(folder, post=post, key="offline")
    assert len(calls) == 1
    assert result["status"] == "input_over_waterline_no_truncation"
    assert "已返回原文22.51%" in (folder / "最终报告.md").read_text(encoding="utf8")


def test_explicit_market_snapshot_enters_frozen_input_without_fetch(tmp_path):
    source = tmp_path / "material.md"
    source.write_text("财报资料", encoding="utf8")
    market = tmp_path / "market.md"
    market.write_text(
        "离线虚构行情：2026-10-24；示例股类；市值100；币种CNY。非真实行情。",
        encoding="utf8",
    )
    folder = tmp_path / "preview"
    run.main(
        [
            "--source",
            str(source),
            "--market-snapshot",
            str(market),
            "--output",
            str(folder),
        ]
    )
    assert market.read_text(encoding="utf8") in (folder / "模型输入.md").read_text(
        encoding="utf8"
    )
    assert str(market.resolve()) in run.io.load(folder / "freeze.json")["files"]
    assert not (folder / "jobs").exists()


def test_mock_scale_and_history_match_frozen_source_panel():
    source = SOURCES[0].read_text(encoding="utf8")
    data = json.loads(FIXTURE.read_bytes())
    names = {
        "revenue": "营业收入",
        "profit": "归母净利",
        "cfo": "CFO",
        "capex": "完整现金Capex",
        "cash": "货币资金",
        "restricted": "受限",
        "equity": "归母权益",
        "eps": "基本EPS",
        "roe": "加权ROE",
        "dividend": "年度分红",
        "inventory": "存货",
        "receivables": "应收账款",
        "contract_assets": "合同资产",
        "contract_liabilities": "合同负债",
    }
    for company in data["companies"]:
        for key, label in names.items():
            if key not in company:
                continue
            if company["code"] == "600039" and key == "dividend":
                label = "现金分红"
            line = next(
                line
                for line in source.splitlines()
                if line.startswith("| " + company["name"] + "／" + label)
            )
            cells = line.split("|")[2:5]
            values = [
                float(re.search(r"-?\d[\d,]*(?:\.\d+)?", cell).group().replace(",", ""))
                for cell in cells
            ]
            assert all(abs(a - b) < 0.005 for a, b in zip(values, company[key])), (
                company["code"],
                key,
            )


def test_complete_rounds_without_usage_reserve_each_call(tmp_path):
    source = tmp_path / "source.md"
    source.write_text("冻结资料", encoding="utf8")
    folder = tmp_path / "run"
    plan = inputs.prepare([source], folder, CONFIG, budget=2)

    class NoUsage(Response):
        def iter_content(self, **kwargs):
            for item in super().iter_content(**kwargs):
                if item.startswith(b"data: {"):
                    value = json.loads(item[6:].strip())
                    value.pop("usage", None)
                    item = (
                        b"data: "
                        + json.dumps(value, ensure_ascii=False).encode()
                        + b"\n\n"
                    )
                yield item

    result = run.execute(
        folder, post=lambda *a, **k: NoUsage("模拟无usage正文", False), key="offline"
    )
    assert result["requests_sent"] == 3 and result["cost_usd"] is None
    assert result["budget_consumed_or_reserved_usd"] == plan["reserved_usd_peak"]


@pytest.mark.parametrize("failure", ["connection", "request_limit"])
def test_first_round_empty_failure_still_writes_delivery(
    tmp_path, monkeypatch, failure
):
    source = tmp_path / "source.md"
    source.write_text("冻结资料", encoding="utf8")
    folder = tmp_path / "run"
    inputs.prepare([source], folder, CONFIG, budget=2)
    original = rounds.request
    calls = []

    def capped(root, plan, index):
        plan["config"]["maximum_request_bytes"] = 1
        return original(root, plan, index)

    def failed(*a, **k):
        calls.append(1)
        raise run.io.requests.exceptions.ConnectionError("offline first failure")

    if failure == "request_limit":
        monkeypatch.setattr(rounds, "request", capped)
    result = run.execute(folder, post=failed, key="offline")
    assert len(calls) == (1 if failure == "connection" else 0)
    assert result["status"] != "complete" and result["delivered_round"] is None
    assert (folder / "正文.md").read_bytes() == b""
    assert (folder / "最终报告.html").is_file()
    assert (folder / "执行结果.json").is_file()
