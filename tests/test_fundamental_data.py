"""Independent data acceptance boundaries, including rejected/stale inputs."""

import ast
import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from scripts import build_fundamental_data as cli
from src.fundamental_data.disclosure import digest, minimum_document, write_json
from src.fundamental_data.disclosure.contract import MINIMUM_FIELDS, TOPICS
from src.fundamental_data.history import assemble_history
from src.fundamental_data.metadata import build_metadata


@pytest.fixture
def frozen(tmp_path):
    """Small source contracts for adversarial checks; real replay is separate."""
    root = tmp_path / "packs"
    entries = []

    def add(
        issuer="a_share:1",
        year=2024,
        policy="相同政策",
        kind="annual",
        scope="consolidated",
    ):
        key = hashlib.sha256(
            f"{issuer}/{year}/{policy}/{scope}/{kind}".encode()
        ).hexdigest()
        meta = {
            "issuer_id": issuer,
            "period_end": f"{year}-12-31",
            "kind": kind,
            "published_at": f"{year + 1}-03-01",
            "company_name": issuer,
            "sha256": key,
            "scope": scope,
        }
        sections = [
            {
                "id": "policy",
                "kind": "policy",
                "title": "收入",
                "text": policy,
                "pages": [1],
            },
            {
                "id": "mda",
                "kind": "mda",
                "title": "管理层讨论",
                "text": "本期经营变化",
                "pages": [2],
            },
        ]
        source = {"metadata": meta, "pages": {"1": policy, "2": "本期经营变化"}}
        source_hash = digest(
            json.dumps(source, sort_keys=True, ensure_ascii=False).encode()
        )
        pack = {
            "metadata": meta,
            "pack_key": key,
            "source_json_sha256": source_hash,
            "financial_fields": {
                k: {"status": "not_located", "scope": scope} for k in MINIMUM_FIELDS
            },
            "fcf_proxy": {"status": "not_located", "label": "非正常化FCF"},
            "management_discussion": {"status": "obtained", "section_refs": ["mda"]},
            "accounting_topics": {
                k: {
                    "status": "obtained",
                    "policy_sections": ["policy"],
                    "note_sections": [],
                }
                for k in TOPICS
            },
            "supplements": {},
            "sections": sections,
        }
        folder = root / "packs" / key
        write_json(folder / "source.json", source)
        write_json(folder / "pack.json", pack)
        write_json(
            folder / "receipt.json",
            {
                "pack_sha256": digest((folder / "pack.json").read_bytes()),
                "source_json_sha256": source_hash,
            },
        )
        entries.append({"pack_key": key})
        write_json(root / "latest_run.json", {"entries": entries})
        return pack

    return root, add


def test_minimum_projection_keeps_gaps_not_zero_and_original_precision(frozen):
    _, add = frozen
    pack = add()
    pack["financial_fields"]["cfo"] = {
        "status": "obtained",
        "amount": "-10.0001",
        "unit": "万元",
        "currency": "CNY",
        "source_ref": {"pages": [3]},
        "candidates": ["large"],
    }
    pack["financial_fields"]["not_requested"] = {"amount": "999"}
    result = minimum_document(pack)
    assert tuple(result["financial_fields"]) == MINIMUM_FIELDS
    assert result["financial_fields"]["cfo"]["amount"] == "-10.0001"
    assert result["financial_fields"]["cfo"]["unit"] == "万元"
    assert result["financial_fields"]["capex"] == {
        "status": "not_located",
        "scope": "consolidated",
    }
    assert "candidates" not in result["financial_fields"]["cfo"]
    assert result["semantic_acceptance"] == "pending"


def test_history_deduplicates_only_exact_policy_and_keeps_all_sources(frozen):
    root, add = frozen
    first, second = add(year=2023), add(year=2025)
    result = assemble_history(root, max_chars=100000, as_of="2026-10-23")
    assert result["status"] == "ready"
    assert result["repeat_occurrences_removed"] == 1
    policy = next(b for b in result["blocks"] if b["kind"] == "policy")
    assert {r["pack_key"] for r in policy["occurrences"]} == {
        first["pack_key"],
        second["pack_key"],
    }
    assert len([b for b in result["blocks"] if b["kind"] == "mda"]) == 2
    assert result["coverage"][0]["annual_years_not_in_selected_inputs"] == [2024]


@pytest.mark.parametrize("change", ["text", "company", "scope"])
def test_history_keeps_changed_policy_and_different_basis(frozen, change):
    root, add = frozen
    add(year=2023)
    kwargs = {"year": 2024}
    kwargs.update(
        {
            "text": {"policy": "相同政策。新增例外"},
            "company": {"issuer": "a_share:2"},
            "scope": {"scope": "parent"},
        }[change]
    )
    add(**kwargs)
    if change == "company":
        with pytest.raises(ValueError, match="requires_one_company"):
            assemble_history(root, max_chars=100000, as_of="2026-10-23")
        return
    assert (
        assemble_history(root, max_chars=100000, as_of="2026-10-23")[
            "repeat_occurrences_removed"
        ]
        == 0
    )


def test_history_budget_rejects_without_cutting_full_evidence(frozen):
    root, add = frozen
    add()
    result = assemble_history(root, max_chars=5, as_of="2026-10-23")
    assert result["status"] == "blocked" and result["context"] is None
    assert result["overflow_chars"] == result["context_chars"] - 5
    assert (
        next(b for b in result["blocks"] if b["kind"] == "mda")["text"]
        == "本期经营变化"
    )


def test_history_future_source_is_rejected(frozen):
    root, add = frozen
    add(year=2025)
    with pytest.raises(ValueError, match="future_disclosure"):
        assemble_history(root, max_chars=100000, as_of="2025-10-23")


def test_history_source_tamper_is_rejected_and_stale_context_removed(
    frozen, monkeypatch, tmp_path
):
    root, add = frozen
    pack = add()
    (root / "packs" / pack["pack_key"] / "source.json").write_text(
        "{}", encoding="utf8"
    )
    output = tmp_path / "output"
    output.mkdir()
    (output / "context.md").write_text("stale accepted input")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "data",
            "history",
            "--source",
            str(root),
            "--output",
            str(output),
            "--max-chars",
            "100000",
            "--as-of",
            "2026-10-23",
            "--issuer",
            "a_share:1",
        ],
    )
    with pytest.raises(ValueError, match="immutable_cached_source_corrupt"):
        cli.main()
    assert not (output / "context.md").exists()


def manifest():
    return {
        "as_of": "2026-10-23",
        "companies": [{"issuer_id": "a_share:1"}, {"issuer_id": "a_share:2"}],
        "candidate_groups": [
            {"label": "configured", "issuer_ids": ["a_share:1", "a_share:2"]}
        ],
    }


def test_metadata_config_is_only_candidate_and_missing_is_explicit(frozen):
    root, _ = frozen
    result = build_metadata(manifest(), root)
    assert all(len(c["missing"]) == 6 for c in result["companies"])
    pair = result["peer_pairs"][0]
    assert pair["status"] == "candidate" and pair["comparability"] == "pending"
    assert pair["basis"]["same_disclosed_industry"] is False


@pytest.mark.parametrize("damage", ["quote", "company", "future", "path", "page"])
def test_metadata_rejects_unsupported_or_future_claims(frozen, damage):
    root, add = frozen
    pack = add(policy="公司行业：建筑业")
    data = manifest()
    claim = {
        "issuer_id": "a_share:1",
        "field": "industry",
        "value": "建筑业",
        "source": {"pack_key": pack["pack_key"], "pages": [1], "quote": "行业：建筑业"},
    }
    if damage == "quote":
        claim["source"]["quote"] = "未披露的业务"
    elif damage == "company":
        claim["issuer_id"] = "a_share:2"
    elif damage == "future":
        data["as_of"] = "2024-01-01"
    elif damage == "page":
        claim["source"]["pages"] = [-1]
    else:
        claim["source"]["pack_key"] = "../escape"
    data["claims"] = [claim]
    with pytest.raises(ValueError):
        build_metadata(data, root)


def test_metadata_source_matches_only_nominate_peer_and_conflicts_stay_visible(frozen):
    root, add = frozen
    data = manifest()
    data["candidate_groups"] = []
    data["claims"] = []
    for issuer in ("a_share:1", "a_share:2"):
        pack = add(
            issuer=issuer, policy="公司行业：建筑业；旧分类：工程建设；地区：中国"
        )
        data["claims"].append(
            {
                "issuer_id": issuer,
                "field": "industry",
                "value": "建筑业",
                "source": {
                    "pack_key": pack["pack_key"],
                    "pages": [1],
                    "quote": "行业：建筑业",
                },
            }
        )
    result = build_metadata(data, root)
    assert result["peer_pairs"][0]["status"] == "candidate"
    assert result["peer_pairs"][0]["basis"]["same_disclosed_industry"] is True
    conflict = deepcopy(data["claims"][0])
    conflict["value"] = "工程建设"
    conflict["source"]["quote"] = "旧分类：工程建设"
    data["claims"].append(conflict)
    result = build_metadata(data, root)
    assert result["companies"][0]["metadata"]["industry"]["status"] == "conflict"
    assert result["peer_pairs"][0]["status"] == "undetermined"




def test_data_imports_never_reach_research():
    root = Path(__file__).resolve().parents[1]
    for path in (root / "src/fundamental_data").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf8"))):
            if isinstance(node, ast.ImportFrom):
                assert node.level <= 1, (path, node.module)
                assert not (node.module or "").startswith(
                    ("src.fundamental_quant", "scripts")
                )
            elif isinstance(node, ast.Import):
                assert not any(
                    n.name.startswith(("src.fundamental_quant", "scripts"))
                    for n in node.names
                )
