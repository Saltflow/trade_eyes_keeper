"""Offline contract and actual presentation-path tests; sockets are forbidden."""

import json
import socket
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import yaml

from scripts import research_inputs as inputs
from scripts import research_render as presentation
from scripts import research_report as run
from tests.research_release_support import use_fixture_market

CONFIG = inputs.REPO / "config/research_report.json"
CHART_DATA = {
    "id": "example",
    "type": "line",
    "title": "离线测试图（虚构）",
    "unit": "%",
    "scope": "示例集团",
    "period_basis": "年度指标",
    "version": "格式测试",
    "source": "测试样例，非公司数据",
    "x": ["FY2023", "FY2024", "FY2025"],
    "series": [{"name": "测试指标", "values": [22.51, None, 23.0]}],
}


def block(data):
    return "```chart\n" + yaml.safe_dump(data, allow_unicode=True) + "```\n"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    use_fixture_market(monkeypatch, inputs)

    def denied(*args, **kwargs):
        pytest.fail("Unexpected network call")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)

    class FrozenDate(datetime):
        @classmethod
        def now(cls, tz=None):
            fixed = datetime(2026, 10, 24, 9, tzinfo=timezone.utc)
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

    monkeypatch.setattr(run, "datetime", FrozenDate)
    monkeypatch.setattr(inputs, "datetime", FrozenDate)
    monkeypatch.setattr(run.io, "datetime", FrozenDate)


@pytest.fixture
def source(tmp_path):
    source = tmp_path / "source.md"
    source.write_bytes("# 已有材料\r\n百分比22.51%，区间10–20，未定。".encode())
    return source


def test_one_entry_offline_prompt_order_seven_questions_and_original_bytes(
    source, tmp_path
):
    folder = tmp_path / "prepared"
    run.main(["--source", str(source), "--output", str(folder)])
    plan = run.io.load(folder / "plan.json")
    request = run.io.load(folder / "request.json")
    system = request["messages"][0]["content"]
    config = run.io.load(CONFIG)
    positions = [
        system.index((inputs.REPO / p).read_text(encoding="utf8"))
        for p in config["prompts"]
    ]
    assert positions == sorted(positions)
    for question in run.io.load(inputs.REPO / config["questions"])["questions"]:
        assert question in system
    assert source.read_bytes().decode() in request["messages"][1]["content"]
    assert (folder / "inputs/1_source.md").read_bytes() == source.read_bytes()
    assert plan["authorized_usd"] is None and plan["maximum_calls"] == 3
    assert not (folder / "jobs").exists()
    with pytest.raises(ValueError, match="explicit_budget_required"):
        run.execute(
            folder, key="offline-mock", post=lambda *a, **k: pytest.fail("send")
        )
    assert not (folder / "execution_started.json").exists()


def test_separation_no_overwrite_no_truncation_budget_and_cli_guards(source, tmp_path):
    output = tmp_path / "prepared"
    inputs.prepare([source], output, CONFIG)
    with pytest.raises(FileExistsError):
        inputs.prepare([source], output, CONFIG)
    with pytest.raises(ValueError, match="output_must_be_separate"):
        inputs.prepare([tmp_path], tmp_path / "nested", CONFIG)
    for budget in ("0.01", "NaN", "Infinity", "-1"):
        with pytest.raises(ValueError, match="invalid_or_insufficient_budget"):
            inputs.prepare([source], tmp_path / "budget", CONFIG, budget=budget)
    config = run.io.load(CONFIG)
    config["input_waterline"] = 1
    custom = tmp_path / "tiny_config.json"
    custom.write_text(json.dumps(config), encoding="utf8")
    with pytest.raises(ValueError, match="input_over_waterline_no_truncation"):
        inputs.prepare([source], tmp_path / "overload", custom)
    assert not (tmp_path / "overload").exists()
    for argv in (
        ["--source", str(source), "--output", str(output), "--paid"],
        ["--execute", str(output)],
        ["--execute", str(output), "--paid", "--focus", "changed"],
    ):
        with pytest.raises(SystemExit):
            run.main(argv)


class Response:
    status_code = 200

    def __init__(self, text, partial):
        self.text, self.partial = text, partial
        self.headers = {"Content-Type": "text/event-stream"}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_content(self, **kwargs):
        def packet(value):
            return b"data: " + json.dumps(value, ensure_ascii=False).encode() + b"\n\n"

        yield packet(
            {
                "choices": [
                    {"delta": {"content": self.text, "reasoning_content": "mock"}}
                ]
            }
        )
        if self.partial:
            raise run.io.requests.exceptions.ChunkedEncodingError("mock interrupted")
        yield packet(
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 500,
                    "prompt_cache_hit_tokens": 0,
                    "prompt_cache_miss_tokens": 1000,
                },
            }
        )
        yield b"data: [DONE]\n\n"


@pytest.mark.parametrize("partial", [False, True])
def test_fixed_rounds_raw_partial_cost_and_no_retry(source, tmp_path, partial):
    folder = tmp_path / "run"
    plan = inputs.prepare([source], folder, CONFIG, budget=Decimal(2))
    raw = '# 离线模拟响应\r\n22.51%；区间10–20；文本保留。\n{"未闭合":'
    calls = []

    def post(url, **kwargs):
        job = plan["jobs"][len(calls)]["id"]
        assert kwargs["data"] == (folder / "requests" / f"{job}.json").read_bytes()
        assert kwargs["allow_redirects"] is False
        calls.append(url)
        return Response(raw, partial)

    result = run.execute(folder, key="offline-mock", post=post)
    assert len(calls) == (1 if partial else 3)
    assert result["receipt"]["status"] == ("partial" if partial else "complete")
    assert (folder / "正文.md").read_bytes() == raw.encode()
    assert raw in (folder / "最终报告.md").read_bytes().decode()
    assert result["raw_content_preserved"] and result["request_bytes_match"]
    assert result["receipt"]["cost"]["usd"] == (None if partial else "0.00045")
    assert Decimal(plan["reserved_usd_peak"]) == Decimal("1.4155776")
    with pytest.raises(FileExistsError):
        run.execute(folder, key="offline-mock", post=post)
    assert len(calls) == (1 if partial else 3)
    assert (folder / "jobs/draft/response.raw").is_file()


@pytest.mark.parametrize("changed", ["source", "request", "plan", "freeze"])
def test_drift_blocked_before_any_call(source, tmp_path, changed):
    folder = tmp_path / "run"
    inputs.prepare([source], folder, CONFIG, budget=2)
    path = source if changed == "source" else folder / f"{changed}.json"
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        run.execute(
            folder, key="offline-mock", post=lambda *a, **k: pytest.fail("send")
        )
    assert not (folder / "execution_started.json").exists()


def test_expired_price_plan_stops_before_network(source, tmp_path, monkeypatch):
    folder = tmp_path / "run"
    inputs.prepare([source], folder, CONFIG, budget=2)

    class LaterDate(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 26, tzinfo=timezone.utc)

    monkeypatch.setattr(run, "datetime", LaterDate)
    with pytest.raises(ValueError, match="outside_frozen_pricing_dates"):
        run.execute(
            folder, key="offline-mock", post=lambda *a, **k: pytest.fail("send")
        )


@pytest.mark.parametrize("kind", ["line", "bar"])
def test_charts_are_actual_local_images_and_feedback_keeps_numbers(tmp_path, kind):
    folder = tmp_path / "report"
    folder.mkdir()
    data = dict(CHART_DATA, type=kind)
    raw = "# 离线格式演示，非研究结论\n\n" + block(data) + "\n结论保留。"
    result = presentation.render(folder, raw)
    assert not result["chart_issues"]
    assert (folder / "assets/chart_1.png").read_bytes().startswith(b"\x89PNG")
    assert 'src="assets/chart_1.png"' in (folder / "最终报告.html").read_text(
        encoding="utf8"
    )
    assert data["source"] in (folder / "最终报告.md").read_text(encoding="utf8")
    feedback = tmp_path / "feedback.txt"
    feedback.write_text("检查反例，不改变原数。", encoding="utf8")
    output = tmp_path / "next_round"
    inputs.prepare([folder], output, CONFIG, feedback=[feedback])
    body = (output / "模型输入.md").read_text(encoding="utf8")
    assert "22.51" in body and "null" in body and "用户反馈材料" in body
    assert (output / "inputs/2_图表数据.yaml").is_file()
    assert len(list((output / "assets").glob("*.png"))) == 1
    assert "assets/chart_1.png" not in body
    assert "assets/" + run.io.sha(folder / "assets/chart_1.png") + ".png" in body


@pytest.mark.parametrize(
    "bad",
    ["invalid_yaml", "22.51%", "range", "missing_context", "bad_shape", "bad_type"],
)
def test_bad_chart_is_local_annotation_rest_of_report_survives(tmp_path, bad):
    data = json.loads(json.dumps(CHART_DATA))
    if bad == "missing_context":
        del data["unit"]
    elif bad == "bad_shape":
        data["series"][0]["values"] = [1]
    elif bad == "bad_type":
        data["type"] = "execute_python"
    else:
        data["series"][0]["values"][0] = bad
    failed = "```chart\nx: [\n```\n" if bad == "invalid_yaml" else block(data)
    raw = (
        "前文22.51%，区间10–20。\n\n"
        + failed
        + "\n后文全部保留。\n\n"
        + block(CHART_DATA)
    )
    result = presentation.render(tmp_path, raw)
    body = (tmp_path / "最终报告.md").read_text(encoding="utf8")
    assert len(result["chart_issues"]) == 1 and failed in body
    assert "前文22.51%，区间10–20。" in body and "后文全部保留。" in body
    assert (tmp_path / "assets/chart_2.png").is_file()


def test_html_never_loads_remote_or_arbitrary_local_images():
    body = "![ok](assets/chart_1.png) ![remote](https://example.com/x.png) ![escape](../x.png)<script>alert(1)</script>"
    html = presentation.html(body)
    assert 'src="assets/chart_1.png"' in html
    assert 'src="https:' not in html and 'src="../' not in html
    assert "<script>" not in html


@pytest.mark.parametrize(
    "error", [ImportError("mock dependency"), RuntimeError("mock draw failure")]
)
def test_renderer_dependency_failure_keeps_whole_document(tmp_path, monkeypatch, error):
    def failed(*args):
        raise error

    monkeypatch.setattr(presentation, "chart", failed)
    raw = "前文\n\n" + block(CHART_DATA) + "后文仍在。"
    result = presentation.render(tmp_path, raw)
    body = (tmp_path / "最终报告.md").read_text(encoding="utf8")
    assert "前文" in body and "后文仍在。" in body and block(CHART_DATA) in body
    assert len(result["chart_issues"]) == 1


def test_feedback_image_is_copied_and_frozen(source, tmp_path):
    folder = tmp_path / "feedback"
    (folder / "assets").mkdir(parents=True)
    image = folder / "assets/example.png"
    image.write_bytes(b"offline image transport fixture")
    feedback = folder / "feedback.md"
    feedback.write_text("![反馈示例](assets/example.png)", encoding="utf8")
    output = tmp_path / "next"
    inputs.prepare([source], output, CONFIG, feedback=[feedback])
    name = run.io.sha(image) + ".png"
    assert (output / "assets" / name).read_bytes() == image.read_bytes()
    assert "assets/" + name in (output / "模型输入.md").read_text(encoding="utf8")
    assert str(image.resolve()) in run.io.load(output / "freeze.json")["files"]


@pytest.mark.parametrize(
    "status,code", [("complete", 0), ("partial", 2), ("failed", 2)]
)
def test_cli_receipt_status_has_nonzero_failure_exit(monkeypatch, status, code):
    monkeypatch.setattr(run, "execute", lambda path: {"status": status})
    assert run.main(["--execute", "unused_mock_directory", "--paid"]) == code
