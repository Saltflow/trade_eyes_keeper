"""Bot reads registered datasets without switching configuration or writing data."""

import json
from unittest.mock import Mock

import pytest

from src.data.dataset_catalog import DatasetCatalog
from src.interactive.assistant.research import ResearchRunner
from src.interactive.assistant.service import FeishuAssistant
from src.interactive.assistant.store import ProposalStore


@pytest.fixture
def catalog_bot(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    (config / "config.yaml").write_text(
        "point_in_time_data:\n  output_dir: data/point_in_time\n", encoding="utf-8"
    )
    dataset = tmp_path / "data/reference_universe/example/dataset"
    (dataset / "market").mkdir(parents=True)
    (dataset / "fundamentals").mkdir()
    (dataset / "market/000001.csv").write_text(
        "date,raw_open,raw_high,raw_low,raw_close,qfq_open,qfq_high,qfq_low,"
        "qfq_close,qfq_factor,volume,tradable\n"
        "2024-01-02,10,11,9,10,10,11,9,10,1,100,True\n",
        encoding="utf-8",
    )
    (dataset / "market/000001.actions.json").write_text("[]", encoding="utf-8")
    (dataset / "market/000001.meta.json").write_text(
        json.dumps({"code": "000001", "source": "unit-test", "currency": "CNY"}),
        encoding="utf-8",
    )
    (dataset / "fundamentals/000001.statements.json").write_text(
        json.dumps(
            {
                "code": "000001",
                "statements": [
                    {
                        "period_end": "2023-09-30",
                        "published_at": "2023-10-30",
                        "source": "unit-test",
                        "net_income_parent": 100,
                        "source_url": "https://static.cninfo.com.cn/example.pdf",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    catalog = DatasetCatalog(tmp_path)
    catalog.build()
    matching = []
    for row in catalog.list_datasets(limit=100)["items"]:
        try:
            if catalog.resolve_dataset_root(row["dataset_id"]) == dataset:
                matching.append(row["dataset_id"])
        except ValueError:
            pass
    assert len(matching) == 1
    bot = FeishuAssistant(
        {},
        Mock(return_value=(True, "ok")),
        project_root=tmp_path,
        configuration=Mock(),
        research=ResearchRunner(tmp_path, {"stock_data_auto_backfill": False}),
        client=Mock(),
        store=ProposalStore(tmp_path / "bot.sqlite3"),
        start_workers=False,
    )
    before = {p: p.read_bytes() for p in dataset.rglob("*") if p.is_file()}
    yield bot, catalog, matching[0], before
    assert all(p.read_bytes() == content for p, content in before.items())
    assert (config / "config.yaml").read_text(encoding="utf-8") == (
        "point_in_time_data:\n  output_dir: data/point_in_time\n"
    )
    bot.stop()


def test_bot_discovers_and_reads_a_nondefault_dataset(catalog_bot):
    bot, _catalog, dataset_id, _before = catalog_bot
    listing = bot.call_tool("chat", "owner", "list_datasets", {})
    assert dataset_id in {row["dataset_id"] for row in listing["items"]}
    files = bot.call_tool(
        "chat",
        "owner",
        "list_dataset_files",
        {
            "dataset_id": dataset_id,
            "limit": 1,
        },
    )
    assert len(files["items"]) == 1
    assert files["total"] >= 4
    assert files["items"][0]["file_id"]
    selected = bot.call_tool(
        "chat",
        "owner",
        "query_research_data",
        {
            "codes": ["000001"],
            "dataset_id": dataset_id,
        },
    )
    assert selected["dataset_id"] == dataset_id
    assert selected["instruments"][0]["market"]["rows"] == 1
    assert selected["instruments"][0]["fundamentals"]["statements"] == 1
    assert selected["completeness_checked"] is False
    default = bot.call_tool(
        "chat",
        "owner",
        "query_research_data",
        {
            "codes": ["000001"],
        },
    )
    assert default["instruments"][0]["market"]["status"] == "missing"
    bot.configuration.apply.assert_not_called()
    bot.send_file = Mock()
    docs = bot.call_tool(
        "chat",
        "owner",
        "query_dataset_documents",
        {
            "dataset_id": dataset_id,
        },
    )
    assert docs["total"] >= 1
    bot.send_file.assert_not_called()


def test_stock_fundamental_tool_searches_indexed_dataset_without_dataset_prompt(catalog_bot):
    bot, _catalog, dataset_id, _before = catalog_bot
    result = bot.call_tool(
        "chat", "owner", "query_stock_fundamentals", {"code": "000001"}
    )
    assert result["searched_all_indexed_datasets"] is True
    assert result["datasets_checked"] == 1
    assert result["selected"]["dataset_id"] == dataset_id
    assert result["selected"]["status"] == "available"
    assert result["selected"]["statement_count"] == 1
    assert result["selected"]["price_date"] == "2024-01-02"
    metrics = result["selected"]["metrics"]
    assert "roe_ttm" in metrics and "pb" in metrics
    json.dumps(result)
    schema_names = {
        row["function"]["name"] for row in bot.tool_schema()
    }
    assert {"query_stock_fundamentals", "search_web"} <= schema_names


def test_stock_lookup_distinguishes_reference_code_metadata_from_market_data(
    catalog_bot,
):
    bot, catalog, _dataset_id, _before = catalog_bot
    metadata = bot.root / "data/reference_universe/reference_batches/2026/codes/600150.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(json.dumps({"code": "600150", "name": "示例公司"}), encoding="utf-8")
    catalog.build()

    result = bot.research.query_stock_fundamentals("600150")

    assert result["selected"] is None
    assert result["datasets_checked"] == 1
    assert result["dataset_results"][0]["status"] == "reference_metadata_only"
    assert result["matched_file_inventory"] == [
        {
            "dataset_id": result["dataset_results"][0]["dataset_id"],
            "relative_path": "data/reference_universe/reference_batches/2026/codes/600150.json",
            "kind": "metadata",
            "extension": ".json",
        }
    ]


def test_model_dataset_pages_keep_paths_once_and_fit_conversation_budget(catalog_bot):
    from src.interactive.assistant.harness import compact_messages
    from src.interactive.assistant.store import canonical_json

    bot, catalog, _, _ = catalog_bot
    original = catalog.list_datasets()["items"][0]
    pages = []
    for offset, count in ((0, 100), (100, 36)):
        rows = []
        for number in range(offset, offset + count):
            row = dict(original)
            root = "data/dataset_imports/local_" + "a" * 64 + "/dataset/"
            root += "data/server_imports/20260921/dataset/" + row["logical_root"]
            row.update(dataset_id=f"ds_{number}", physical_root=root, title=root)
            rows.append(row)
        raw = {"items": rows, "offset": offset, "limit": 100, "total": 136}
        value = bot._model_value(raw)
        assert all("title" not in row for row in value["items"])
        assert all("title" in row for row in raw["items"])
        assert value["items"][0]["physical_root"] == root
        pages.append({"role": "tool", "content": canonical_json(value)})
    messages = [{"role": "system", "content": "x" * 14000}, *pages]
    assert compact_messages(messages, 96000) == messages


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("list_datasets", {"limit": True}),
        ("list_datasets", {"limit": 101}),
        ("list_dataset_files", {"offset": -1}),
        ("get_dataset_details", {"dataset_id": "../../config"}),
        ("list_dataset_files", {"path": "config/.env"}),
        ("query_dataset_documents", {"url": "http://localhost/secret"}),
        ("query_research_data", {"codes": ["000001"], "dataset_id": "../data"}),
        ("read_dataset_text", {"file_id": "../config/.env"}),
        ("read_dataset_text", {"file_id": "abc", "line_count": 121}),
    ],
)
def test_dataset_tools_reject_unregistered_paths_and_invalid_parameters(
    catalog_bot, name, arguments
):
    bot, _catalog, _dataset_id, _before = catalog_bot
    with pytest.raises((ValueError, TypeError)):
        bot.call_tool("chat", "owner", name, arguments)
