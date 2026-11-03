"""Full durable investigation + three-round writing with mocked external IO."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.research_pipeline.common import atomic_write, file_sha, load
from src.research_pipeline.engine import run_research
from src.research_pipeline.investigation import verify_bundle
from tests.test_research_pipeline_flow import (
    Model,
    Response,
    Source,
    documents,
    fixture_spec,
    no_network,  # noqa: F401 - explicit offline IO boundaries
)


def test_review_rework_creates_new_model_and_only_three_writing_calls(
    tmp_path, monkeypatch
):
    from src.research_pipeline.model_sandbox import ModelSandbox
    from tests.research_investigation_support import sandbox_result

    def chart_result(self, inputs, output, **kwargs):
        receipt = sandbox_result(self, inputs, output, **kwargs)
        path = output / "files/results.json"
        value = load(path)
        value["charts"] = [
            {
                "id": "fixture_eps",
                "type": "bar",
                "title": "Synthetic EPS",
                "unit": "fixture",
                "scope": "fixture",
                "period_basis": "E",
                "version": "fixture",
                "source": "calculated",
                "x": [2031],
                "series": [{"name": "normal", "values": [value["eps"]]}],
            }
        ]
        atomic_write(path, value)
        return {**receipt, "files": {"results.json": file_sha(path)}}

    monkeypatch.setattr(ModelSandbox, "run", chart_result)

    class ReviewModel(Model):
        def __call__(self, url, **kwargs):
            response = super().__call__(url, **kwargs)
            request = json.loads(kwargs["data"])
            system = request["messages"][0]["content"]
            if (
                isinstance(response.content, dict)
                and request.get("tools")
                and not any(m["role"] == "assistant" for m in request["messages"])
            ):
                # Explicit fixture, not provider reasoning. Native continuity and
                # model code can exceed the old 200k character chat window.
                response.content["reasoning_content"] = "Synthetic context. " * 12000
            if "# 第二轮：" in system:
                return Response(
                    'Review fixture\n```research-rework\n{"required":true,"issues":["Correct the synthetic financing share count to three."]}\n```'
                )
            return response

    model = ReviewModel()
    spec = fixture_spec(tmp_path)
    result = run_research(spec, source=Source(documents(tmp_path)), post=model)
    assert result["state"] == "complete", result
    assert any(
        len(message.get("reasoning_content", "")) > 200000
        for request in model.calls
        for message in request["messages"]
    )
    report = next(a for a in result["artifacts"] if a["kind"] == "research")
    versions = report["metadata"]["investigation_versions"]
    assert [v["metadata"]["revision"] for v in versions] == ["initial", "review_rework"]
    assert (
        versions[0]["metadata"]["selected_model"]
        != versions[1]["metadata"]["selected_model"]
    )

    for version in versions:
        manifest = Path(version["metadata"]["bundle"])
        verify_bundle(manifest)
        assert file_sha(manifest) == version["metadata"]["bundle_sha256"]
    writes = [
        r
        for r in model.calls
        if any(
            f"# {n}轮：" in r["messages"][0]["content"]
            for n in ("第一", "第二", "第三")
        )
    ]
    assert len(writes) == 3
    final_input = json.dumps(writes[-1], ensure_ascii=False)
    assert "5.333333333333333" in final_input
    assert "assets/rework_chart_1.png" in final_input
    assert "assets/draft_chart_1.png" not in final_input
    views = spec.output / "research/views"
    assert file_sha(views / "draft/assets/chart_1.png") != file_sha(
        views / "rework/assets/chart_1.png"
    )
    assert "第二轮已审阅本次结果" in final_input  # explicitly forbidden in the handoff
    assert len([a for a in result["artifacts"] if a["kind"] == "investigation"]) == 2
    assert all(
        Path(a["bundle_path"]).exists()
        for a in result["artifacts"]
        if a["kind"] == "investigation"
    )


@pytest.mark.parametrize("missing_stage", ["initial", "review_rework"])
def test_missing_computed_model_stops_numeric_writing(
    tmp_path, monkeypatch, missing_stage
):
    from src.research_pipeline import investigation as module
    from src.research_pipeline.engine import Pipeline
    from src.research_pipeline.models import ResearchSpec

    spec = ResearchSpec(companies=("us:FIXTURE",), output=tmp_path / "run")
    calls, saved, gaps = [], [], []

    def investigate(pipeline, key, base, *, revision="initial", **kwargs):
        root = spec.output / "investigation" / revision
        atomic_write(root / "handoff.md", "Synthetic execution evidence only")
        bundle = root / "bundle.json"
        atomic_write(
            bundle,
            {
                "files": {"handoff.md": file_sha(root / "handoff.md")},
                "selected_model": None if revision == missing_stage else "computed",
                "model_status": "not_built" if revision == missing_stage else "passed",
                "error": "sandbox_unavailable" if revision == missing_stage else "",
            },
        )
        return {
            "path": str(root / "handoff.md"),
            "status": "partial" if revision == missing_stage else "complete",
            "metadata": {"bundle": str(bundle)},
        }

    def call(key, request, phase, **kwargs):
        calls.append(phase)
        capture = tmp_path / phase
        atomic_write(
            capture / "content.md",
            'review\n```research-rework\n{"required":true,"issues":["recompute"]}\n```',
        )
        return {"status": "complete", "capture_dir": str(capture)}

    def save(key, kind, text, status, metadata):
        saved.append({"status": status, "metadata": metadata})
        return {"path": str(tmp_path / "report.md"), **saved[-1]}

    pipeline = SimpleNamespace(
        spec=spec,
        store=SimpleNamespace(
            list_artifacts=lambda kind: [], update_run=lambda *a, **k: None
        ),
        subjects={},
        company_artifacts={},
        quotes=[],
        coverage=dict,
        phase=lambda value: None,
        gap=lambda kind, **fields: gaps.append(kind),
        stopped=lambda: False,
        final_artifacts=[],
        blocked=[],
        gaps=gaps,
        run_id="fixture",
        adapters=SimpleNamespace(
            research_request=lambda *a: {},
            call=call,
            render_charts=lambda *a: {},
            save_text_artifact=save,
            finalize_images=lambda *a: None,
        ),
    )
    peer_path = tmp_path / "peer.md"
    atomic_write(peer_path, "Synthetic public evidence")
    monkeypatch.setattr(module, "investigate", investigate)
    monkeypatch.setattr(module, "model_charts", lambda artifact: [])
    result = Pipeline.research(
        pipeline, {"path": str(peer_path), "metadata": {}, "status": "complete"}, "key"
    )
    if missing_stage == "initial":
        assert calls == [] and result is None
        assert gaps == ["investigation_model_unavailable_no_writing"]
    else:
        assert calls == ["research_draft", "research_review"]
        assert result["status"] == "partial"
        assert result["metadata"]["delivered_round"] == "review"
        assert gaps == ["rework_model_unavailable_no_final_write"]


def test_explicit_reinvestigate_reuses_upstream_and_preserves_original_report(tmp_path):
    docs, model = documents(tmp_path), Model()
    first = run_research(fixture_spec(tmp_path), source=Source(docs), post=model)
    old = next(a for a in first["artifacts"] if a["kind"] == "research")
    digest = file_sha(Path(old["source"]))
    count = len(model.calls)
    second = run_research(
        fixture_spec(tmp_path, name="new_study", reinvestigate=True),
        source=Source(docs),
        post=model,
    )
    new = next(a for a in second["artifacts"] if a["kind"] == "research")
    assert second["state"] == "complete"
    assert new["key"] != old["key"]
    assert new["metadata"]["reinvestigate"] is True
    assert len(model.calls) == count + 8
    assert all("[block:" not in json.dumps(r) for r in model.calls[count:])
    assert file_sha(Path(old["source"])) == digest
    before = len(model.calls)
    cached = run_research(
        fixture_spec(tmp_path, name="reuse", budget="0"),
        source=Source(docs),
        post=model,
    )
    assert len(model.calls) == before and cached["budget"]["known_usage_usd"] == "0"
    assert (
        next(a for a in cached["artifacts"] if a["kind"] == "research")["key"]
        == old["key"]
    )
    assert (
        load(Path(old["metadata"]["investigation_versions"][0]["metadata"]["bundle"]))[
            "financial_validation"
        ]
        == "not_independently_verified"
    )
