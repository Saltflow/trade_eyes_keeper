"""Default CLI prompt closure must not impose a historical case or company count."""

import json

import pytest

from scripts import research_inputs as inputs
from scripts import research_report as run
from tests.test_research_report import (
    CONFIG,
    Response,
    offline,  # noqa: F401 - autouse fixture
)

HISTORICAL_CASE_RULES = (
    "四川路桥",
    "隧道股份",
    "泸州老窖",
    "工程两家",
    "工程深入",
    "白酒参照",
    "白酒机制参照",
    "三家公司",
    "四家公司",
    "四大行",
    "工商银行",
    "农业银行",
    "建设银行",
    "中国银行",
)


def test_default_config_entire_prompt_closure_is_case_independent():
    config = run.io.load(CONFIG)
    paths = config["prompts"] + [s["prompt"] for s in config["rounds"]]
    for name in paths:
        text = (inputs.REPO / name).read_text(encoding="utf8")
        assert not any(rule in text for rule in HISTORICAL_CASE_RULES), name
    assert config["version"] == "research-report-v6-input-driven-scope"
    assert "docs/design/research_report_example_v6.md" in config["prompts"]
    assert [s["id"] for s in config["rounds"]] == ["draft", "review", "write"]


@pytest.mark.parametrize(
    "companies,focus",
    [
        (
            ["工商银行", "农业银行", "建设银行", "中国银行"],
            "四家均为研究对象，按银行业务证据横比。",
        ),
        (["保险样本甲", "保险样本乙"], "按本轮保险业务材料研究。"),
        (["能源样本甲"], "只有一家对象，研究自身历史和未来情景。"),
        (
            ["制造样本甲", "制造样本乙", "零售样本丙"],
            (
                "用户明确角色：制造样本甲与制造样本乙为同行；"
                "零售样本丙仅作机制参照。"
            ),
        ),
    ],
    ids=["four-banks", "two-insurers", "one-energy", "explicit-mixed-roles"],
)
def test_default_cli_preserves_actual_scope_through_all_rounds(
    tmp_path, companies, focus
):
    source = tmp_path / "scope.md"
    material = (
        "# 离线身份与角色交接测试，不是实际财务资料\n\n"
        + "本轮公司清单："
        + "、".join(companies)
        + "。\n仅提供FY2022—FY2023两个年度身份，不补造财务数值。\n"
    )
    source.write_bytes(material.encode("utf8"))
    folder = tmp_path / "default_cli"
    assert run.main(
        [
            "--source",
            str(source),
            "--focus",
            focus,
            "--output",
            str(folder),
            "--budget-limit-usd",
            "2",
        ]
    ) == 0
    assert not (folder / "jobs").exists()
    prepared = run.io.load(folder / "plan.json")
    calls = []

    def post(url, **kwargs):
        request = json.loads(kwargs["data"])
        system = request["messages"][0]["content"]
        assert not any(rule in system for rule in HISTORICAL_CASE_RULES)
        assert "本轮公司清单" in system
        assert "没有明确参照角色时，不自动设置参照公司" in system
        content = request["messages"][1]["content"]
        text = (
            content
            if isinstance(content, str)
            else "\n".join(p["text"] for p in content if p["type"] == "text")
        )
        assert material in text and focus in text
        assert all(company in text for company in companies)
        job = prepared["jobs"][len(calls)]["id"]
        assert kwargs["data"] == (folder / "requests" / f"{job}.json").read_bytes()
        calls.append(job)
        return Response(f"# 离线{job}交接模拟\n\n非研究结论，原文保留。", False)

    result = run.execute(folder, post=post, key="offline-scope-test-only")
    assert result["simulated"] and result["requests_sent"] == 3
    assert calls == ["draft", "review", "write"]
    assert result["automatic_retries"] == 0
    assert (folder / "inputs/1_scope.md").read_bytes() == source.read_bytes()
    run.io.verify(run.io.load(folder / "freeze.json")["files"])
