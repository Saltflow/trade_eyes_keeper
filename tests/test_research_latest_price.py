"""Local read-only prices: dates, explicit raw basis, preservation and freezing."""

import json

import pytest

from scripts import research_inputs as inputs
from scripts import research_report as run
from tests.research_release_support import SOURCES
from tests.test_research_report import CONFIG, offline  # noqa: F401


def day_file(root, name, day, close, source="tencent_raw_day", code="600039"):
    path = root / "data/fundamental_quant/public_history/ranges" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "issuer_id": "a_share:" + code,
                "source": source,
                "currency": "CNY",
                "rows": [{"date": day, "close": close}],
            }
        ),
        encoding="utf8",
    )
    return path


def test_local_price_uses_quote_day_not_file_name_and_never_adjusted(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(inputs, "REPO", tmp_path)
    day_file(tmp_path, "a_share_600039_z.json", "2026-01-01", 9)
    chosen = day_file(tmp_path, "a_share_600039_a.json", "2026-01-02", 8)
    day_file(
        tmp_path, "a_share_600039_adjusted.json", "2026-01-03", 999, "baostock_qfq"
    )
    day_file(tmp_path, "a_share_600039_future.json", "2099-01-01", 777)
    text, sources = inputs.latest_market([], ["600039", "600820"])
    assert "| 600039 | 8 | 2026-01-02 | CNY |" in text
    assert "999" not in text and "777" not in text and sources == [chosen]
    assert "| 600820 | 未取得" in text


def test_same_day_conflict_is_retained_and_malformed_file_does_not_drop_prices(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(inputs, "REPO", tmp_path)
    a = day_file(tmp_path, "a_share_600039_a.json", "2026-01-02", 8)
    b = day_file(tmp_path, "a_share_600039_b.json", "2026-01-02", 9)
    broken = a.with_name("a_share_600039_bad.json")
    broken.write_text("{", encoding="utf8")
    text, sources = inputs.latest_market([], ["600039"])
    assert text.count("同日冲突，分别保留") == 2
    assert a in sources and b in sources and "JSONDecodeError" in text
    assert "| 600039 | 8 |" in text and "| 600039 | 9 |" in text


def test_company_manifest_drives_selection_without_parsing_prose(monkeypatch, tmp_path):
    monkeypatch.setattr(inputs, "REPO", tmp_path)
    source = tmp_path / "input"
    source.mkdir()
    manifest = source / "manifest.json"
    manifest.write_text(
        json.dumps({"kind": "stage3_input_v1", "companies": [{"company": "600039"}]}),
        encoding="utf8",
    )
    quote = day_file(tmp_path, "a_share_600039_a.json", "2026-01-02", 8)
    text, protected = inputs.latest_market([source / "模型输入.md"])
    assert "| 600039 | 8 |" in text and set(protected) == {manifest, quote}
    assert inputs.latest_market([]) == ("", [])


def test_prepared_snapshot_is_copied_frozen_and_only_adds_small_input(tmp_path):
    sources = SOURCES
    folder = tmp_path / "with_market"
    plan = inputs.prepare(sources, folder, CONFIG)
    without = inputs.prepare(sources, tmp_path / "without", CONFIG, local_prices=False)
    text = (folder / "行情快照.md").read_text(encoding="utf8")
    assert "| 600039 | 8.32 | 2026-10-23" in text
    assert "| 000568 | 69.99 | 2026-10-23" in text
    assert "| 600820 | 未取得" in text
    assert text in (folder / "模型输入.md").read_text(encoding="utf8")
    assert 0 < plan["input_tokens_estimate"] - without["input_tokens_estimate"] < 1500
    assert len(list((folder / "market_sources").glob("*.json"))) == 3
    assert plan["authorized_usd"] is None and plan["maximum_calls"] == 3
    protected = run.io.load(folder / "freeze.json")["files"]
    assert str((folder / "行情快照.md").resolve()) in protected
    (folder / "行情快照.md").write_text("changed", encoding="utf8")
    with pytest.raises(ValueError):
        run.io.verify(protected)


def test_explicit_market_snapshot_overrides_local_autoload(tmp_path):
    source = tmp_path / "source.md"
    source.write_text("已有研究材料", encoding="utf8")
    snapshot = tmp_path / "chosen_market.md"
    snapshot.write_text("用户明确选定的价格日期与口径", encoding="utf8")
    folder = tmp_path / "explicit"
    run.main(
        [
            "--source",
            str(source),
            "--market-snapshot",
            str(snapshot),
            "--companies",
            "600039",
            "--output",
            str(folder),
        ]
    )
    assert not (folder / "行情快照.md").exists()
    body = (folder / "模型输入.md").read_text(encoding="utf8")
    assert "用户明确选定" in body and "本轮最新已知价" not in body


def test_current_form_only_shows_the_years_actually_provided():
    from tests.research_known_price_fixtures import bodies, chart_specs

    for chart in chart_specs()[:12]:
        assert chart["x"] == ["FY2023", "FY2024", "FY2025"]
        assert all(len(row["values"]) == 3 for row in chart["series"])
    final = bodies("本轮行情快照")[2]
    assert "FY2019" not in final and "FY2022" not in final
    assert "只画实际三年" in final and "不造空列" in final
    assert "assets/review_chart_14.png" in final
