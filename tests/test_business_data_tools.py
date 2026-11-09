"""Offline acceptance of bounded, read-only business data projections."""

import json
import sqlite3
import struct

import pandas as pd
import pyarrow as pa
import pytest
from pyarrow import feather, parquet

from src.data.dataset_catalog import DatasetCatalog
from src.data_hub.materials import DataHub
from src.interactive.assistant import data_tools as module
from src.interactive.assistant.data_tools import (
    BusinessDataTools,
    paginate_rows,
    read_path,
)
from src.platform_composition import authority_for


def write(root, relative, value):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return path


def test_xls_reader_with_valid_legacy_biff_workbook(tmp_path):
    """Synthetic BIFF records exercise xlrd without adding a writer dependency."""

    def record(opcode, data=b""):
        return struct.pack("<HH", opcode, len(data)) + data

    def label(row, column, text):
        raw = text.encode("ascii")
        return record(4, struct.pack("<HH3sB", row, column, b"\0\0\0", len(raw)) + raw)

    path = tmp_path / "synthetic.xls"
    path.write_bytes(
        record(9, struct.pack("<HH", 2, 0x10))
        + record(0x42, struct.pack("<H", 1252))
        + label(0, 0, "code")
        + label(0, 1, "value")
        + label(1, 0, "SYNTHETIC")
        + label(1, 1, "2")
        + record(10)
    )
    tables = read_path(path)["tables"]
    assert tables == ["Sheet 1"]
    assert read_path(path, table=tables[0])["items"] == [
        {"code": "SYNTHETIC", "value": "2"}
    ]


def test_existing_weekly_markdown_is_read_without_weekly_generator(tmp_path):
    write(tmp_path, "reports/2026_weekly.md", "# Historical weekly\nSYNTHETIC fixture")
    tools = BusinessDataTools(tmp_path)
    item = tools.find(domain="reports", report_type="weekly")["items"][0]
    assert "Historical weekly" in str(tools.read(item["data_id"])["items"])
    assert not (tmp_path / "data/runtime").exists()


@pytest.mark.parametrize(
    "ext,body",
    [
        ("json", '[{"code":"01339","value":2}]'),
        ("jsonl", '{"code":"01339","value":2}\n'),
        ("csv", "code,value\n01339,2\n"),
        ("tsv", "code\tvalue\n01339\t2\n"),
        ("yaml", "- code: '01339'\n  value: 2\n"),
        ("xml", '<rows><row code="01339">2</row></rows>'),
        ("txt", "01339 value 2\n"),
        ("md", "# 01339 value 2\n"),
        (
            "html",
            '<!--receiver:test@example.org--><h1>日报</h1><table><tr><td>01339</td><td>2</td></tr></table><img src="https://never.fetch/image"><script>secret</script>',
        ),
    ],
)
def test_supported_text_formats(tmp_path, ext, body):
    path = write(tmp_path, f"data/analysis/01339.{ext}", body)
    catalog = DatasetCatalog(tmp_path)
    catalog.build()
    tools = BusinessDataTools(tmp_path, catalog=catalog)
    found = tools.find(query=path.name)
    assert len(found["items"]) == 1
    result = tools.read(found["items"][0]["data_id"])
    assert result["scan_complete"]
    assert "01339" in str(result["items"])
    assert "test@example" not in str(result)
    assert "never.fetch" not in str(result)


@pytest.mark.parametrize("ext", ["parquet", "feather", "arrow", "xlsx"])
def test_binary_structured_formats(tmp_path, ext):
    path = tmp_path / f"table.{ext}"
    data = pa.table({"code": ["01339"], "value": [2]})
    if ext == "parquet":
        parquet.write_table(data, path)
    elif ext == "feather":
        feather.write_feather(data, path)
    elif ext == "arrow":
        with pa.OSFile(str(path), "wb") as target, pa.ipc.new_stream(
            target, data.schema
        ) as writer:
            writer.write_table(data)
    else:
        pd.DataFrame(data.to_pydict()).to_excel(path, index=False)
        assert read_path(path)["tables"] == ["Sheet1"]
    result = read_path(path, **({"table": "Sheet1"} if ext == "xlsx" else {}))
    assert result["items"] == [{"code": "01339", "value": 2}]


def test_sqlite_readonly_and_private_tables(tmp_path):
    path = tmp_path / "business.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE prices(code TEXT,value REAL); INSERT INTO prices VALUES('01339',2); CREATE TABLE accounts(owner TEXT); CREATE TABLE mixed(code TEXT,chat_id TEXT);"
        )
    assert read_path(path)["tables"] == ["prices", "mixed"]
    assert read_path(path, table="prices")["items"][0]["value"] == 2
    with pytest.raises(ValueError, match="private"):
        read_path(path, table="mixed")
    with pytest.raises(ValueError, match="unknown or private"):
        read_path(path, table="prices; DROP TABLE prices")
    with pytest.raises(ValueError, match="unsupported"):
        read_path(path, sql="DELETE FROM prices")


def test_filter_projection_aggregate_and_incomplete_scan(monkeypatch):
    rows = [{"code": "01339", "value": n} for n in range(10)]
    result = paginate_rows(
        rows,
        fields=["value"],
        filters=[{"field": "value", "op": "gte", "value": 2}],
        limit=2,
    )
    assert result["items"] == [{"value": 2}, {"value": 3}]
    assert result["truncated"] and result["next_offset"] == 2
    assert (
        paginate_rows(rows, aggregate={"op": "sum", "field": "value"})["aggregate"][
            "value"
        ]
        == 45
    )
    monkeypatch.setattr(module, "MAX_SCAN_ROWS", 3)
    result = paginate_rows(rows, aggregate={"op": "count"})
    assert result["aggregate"] == {
        "op": "count",
        "field": None,
        "value": 3,
        "complete": False,
    }
    with pytest.raises(TypeError):
        paginate_rows(rows, sql="SELECT 1")


def test_redacts_secrets_but_retains_authenticated_holdings():
    row = {
        "accounts": [
            {"holdings": [{"code": "01339", "quantity": 5}], "owner": "other"}
        ],
        "text": "password=abcdef test@example.org",
    }
    assert "accounts" not in paginate_rows(row)["items"][0]
    result = paginate_rows(row, authenticated=True)["items"][0]
    assert result["accounts"][0]["holdings"][0]["quantity"] == 5
    assert "owner" not in result["accounts"][0]
    assert "abcdef" not in result["text"] and "test@example.org" not in result["text"]


def test_archives_auto_register_date_filter_and_update(tmp_path):
    path = write(
        tmp_path,
        "data/email_archive/20261104_120000_股票量化日报.html",
        "<p>01339 支付日 2026-11-06</p>",
    )
    tools = BusinessDataTools(tmp_path)
    found = tools.find(
        domain="reports", report_type="daily", code="01339", date_from="2026-11-04"
    )
    assert len(found["items"]) == 1
    data_id = found["items"][0]["data_id"]
    assert tools.read(data_id)["delivery_status"] == "unknown"
    path.write_text("<p>updated</p>", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        tools.read(data_id)
    assert tools.find(domain="reports")["items"][0]["data_id"] == data_id
    assert "updated" in str(tools.read(data_id)["items"])
    assert not tools.find(domain="reports", date_from="2026-11-05")["items"]


def test_output_roots_private_paths_and_private_callback_paging(tmp_path):
    write(tmp_path, "outputs/run/result.json", '{"code":"01339"}')
    write(tmp_path, "outputs/private/holdings.json", '{"quantity":100}')
    write(tmp_path, "outputs/research_runs/run/result.json", '{"business":2}')
    write(tmp_path, "outputs/shared_research/result.json", '{"business":3}')
    write(tmp_path, "outputs/personal_monitor/result.json", '{"business":4}')
    write(
        tmp_path,
        "outputs/run/private_origin.json",
        '{"origin":{"channel":"feishu","chat":"private-chat","sender":"private-user"},"text":"secret analysis"}',
    )

    def private_find(**kwargs):
        return {
            "items": [
                {"data_id": f"private:{i}"}
                for i in range(
                    kwargs["offset"], min(2, kwargs["offset"] + kwargs["limit"])
                )
            ]
        }

    tools = BusinessDataTools(tmp_path, private_find=private_find)
    first = tools.find(limit=1)
    assert len(first["items"]) == 1
    assert first["items"][0]["source"] == "outputs/run/result.json"
    assert tools.find(limit=1, offset=1)["items"] == [{"data_id": "private:0"}]
    assert tools.find(limit=1, offset=2)["items"] == [{"data_id": "private:1"}]
    with pytest.raises(ValueError, match="unknown"):
        tools.read("outputs/private/holdings.json")


def test_registered_material_read_no_new_parse(tmp_path):
    path = write(tmp_path, "original.txt", "01339 public material")
    authority = authority_for(tmp_path)
    hub = DataHub(authority)
    record = hub.register_original(path)
    tools = BusinessDataTools(tmp_path, material_authority=authority)
    found = tools.find(domain="material")
    assert found["items"][0]["data_id"] == "material:" + record["sha256"]
    assert "01339 public" in str(tools.read(found["items"][0]["data_id"])["items"])
    assert not (hub.root / "parsed" / record["sha256"] / "parsed.json").exists()


def test_oversized_document_fails_explicitly_and_no_xml_entities(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "MAX_BYTES", 20)
    with pytest.raises(ValueError, match="not scanned"):
        read_path(write(tmp_path, "large.json", json.dumps({"data": "x" * 30})))
    monkeypatch.setattr(module, "MAX_BYTES", 1000)
    with pytest.raises(ValueError, match="entities"):
        read_path(
            write(
                tmp_path,
                "bad.xml",
                '<!DOCTYPE x [<!ENTITY x SYSTEM "file:///etc/passwd">]><x>&x;</x>',
            )
        )


def test_nested_collection_paging_and_public_business_names():
    data = {
        "data": {
            "stocks_data": {
                str(i): {
                    "code": str(i),
                    "last": i,
                    "accounting_standards": "IFRS",
                    "shareholder_holdings": 2,
                    "market_position": 3,
                }
                for i in range(23)
            }
        }
    }
    result = paginate_rows(data, collection="data.stocks_data", limit=10, offset=10)
    assert len(result["items"]) == 10
    assert result["items"][0]["record_key"] == "10"
    assert result["items"][0]["shareholder_holdings"] == 2
    assert result["items"][0]["accounting_standards"] == "IFRS"
    assert result["items"][0]["market_position"] == 3
    assert (
        paginate_rows(data, collection="data.stocks_data", aggregate={"op": "count"})[
            "aggregate"
        ]["value"]
        == 23
    )
    with pytest.raises(ValueError, match="private"):
        paginate_rows({"accounts": []}, collection="accounts")
    with pytest.raises(ValueError, match="invalid"):
        paginate_rows(data, collection="../passwd")


def test_public_reader_cannot_enable_private_fields(tmp_path):
    path = write(tmp_path, "data/analysis/safe.json", '{"accounts":[1]}')
    catalog = DatasetCatalog(tmp_path)
    catalog.build()
    tools = BusinessDataTools(tmp_path, catalog=catalog)
    data_id = tools.find(query=path.name)["items"][0]["data_id"]
    with pytest.raises(ValueError, match="unsupported"):
        tools.read(data_id, authenticated=True)


def test_csv_numeric_filter_keeps_security_code(tmp_path):
    path = write(tmp_path, "quotes.csv", "code,last\n01339,2.9\n01088,40.1\n")
    result = read_path(path, filters=[{"field": "last", "op": "lt", "value": 3}])
    assert result["items"] == [{"code": "01339", "last": 2.9}]
    assert (
        read_path(path, aggregate={"op": "sum", "field": "last"})["aggregate"]["value"]
        == 43.0
    )


@pytest.mark.parametrize("value", [7, True, None, "text"])
def test_json_scalar_preserves_value(tmp_path, value):
    path = write(tmp_path, "scalar.json", json.dumps(value))
    assert read_path(path)["items"] == [value]


def test_fetched_blob_metadata_and_cached_html_read_without_parse(
    tmp_path, monkeypatch
):
    from src.data_hub import runtime
    from src.data_hub.parsing import ParsedCache

    monkeypatch.setattr(
        runtime, "run_material_task", lambda authority, kind, callback: callback()
    )
    authority = authority_for(tmp_path)
    hub = DataHub(authority)
    url = "https://official.example/01339-dividend.html"
    original = hub.ensure_original(
        url,
        lambda target: (
            b"<html><body><!--private@example.org--><p>01339 payment 2026-11-06</p></body></html>"
        ),
        content_type="text/html",
    )
    assert original["path"].endswith("original.blob")
    hub.ensure_parsed(original["path"], "text/html")

    def forbidden(*args, **kwargs):
        raise AssertionError("data tool must not generate parses")

    monkeypatch.setattr(ParsedCache, "read", forbidden)
    tools = BusinessDataTools(tmp_path, material_authority=authority)
    found = tools.find(domain="material", code="01339", query="dividend")
    assert found["items"][0]["readable"]
    assert found["items"][0]["source"] == url
    result = tools.read(found["items"][0]["data_id"])
    assert "2026-11-06" in str(result["items"])
    assert "private@example" not in str(result)
    assert "<p>" not in str(result["items"])


def test_fundamental_public_exports_and_private_research_excluded(tmp_path):
    write(
        tmp_path,
        "data/fundamental_data/run/01339/extraction.json",
        '{"code":"01339", "revenue":42}',
    )
    write(
        tmp_path,
        "data/fundamental_data/research_runs/private/result.json",
        '{"code":"01339", "private":42}',
    )
    write(
        tmp_path,
        "data/fundamental_data/run/private.json",
        '{"origin":{"channel":"feishu","chat":"private","sender":"user"}, "text":"private research"}',
    )
    write(
        tmp_path, "data/fundamental_quant/index.html", "<p>Historical weekly report</p>"
    )
    write(tmp_path, "data/fundamental_quant/research.sqlite3", "not a public export")
    tools = BusinessDataTools(tmp_path)
    found = tools.find(limit=100)
    sources = {item.get("source") for item in found["items"]}
    assert sources == {
        "data/fundamental_data/run/01339/extraction.json",
        "data/fundamental_quant/index.html",
    }
    assert any("ownership is unresolved" in gap for gap in found["coverage_gaps"])
    for item in found["items"]:
        assert tools.read(item["data_id"])["scan_complete"]


def test_html_keeps_table_header_and_row_relationship(tmp_path):
    path = write(
        tmp_path,
        "report.html",
        "<table><tr><th>code</th><th>value</th></tr><tr><td>01339</td><td>2</td></tr></table>",
    )
    rows = [item["text"] for item in read_path(path)["items"]]
    assert "code | value" in rows
    assert "01339 | 2" in rows


def test_oversized_cached_material_parse_never_loaded(tmp_path, monkeypatch):
    authority = authority_for(tmp_path)
    hub = DataHub(authority)
    original = hub.register_original(write(tmp_path, "public.pdf", "%PDF-offline"))
    cache = hub.parsed._path(original["sha256"])
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(b"x" * 200)
    monkeypatch.setattr(module, "MAX_BYTES", 100)
    tools = BusinessDataTools(tmp_path, material_authority=authority)
    with pytest.raises(ValueError, match="not scanned"):
        tools.read("material:" + original["sha256"])


def test_scan_limit_has_no_nonprogressing_next_page(monkeypatch):
    monkeypatch.setattr(module, "MAX_SCAN_ROWS", 3)
    rows = [{"value": i} for i in range(10)]
    for options in (
        {"offset": 10},
        {"filters": [{"field": "value", "op": "gt", "value": 5}]},
    ):
        result = paginate_rows(rows, **options)
        assert result["items"] == []
        assert result["truncated"] and result["scan_limit_reached"]
        assert result["next_offset"] is None
        assert result["coverage_gaps"]


@pytest.mark.parametrize("authenticated", [False, True])
def test_email_channel_failure_survives_redaction_without_provider_identity(authenticated):
    value = {"result": {"deliveries": {
        "email": {"status": "failed", "reason": "smtp_rejected", "smtp_code": 550,
                  "recipient": "private@example.com", "password": "private-secret",
                  "detail": "private-user was rejected", "pieces": [{"status": "sent"}]},
        "feishu": {"status": "sent", "chat_id": "private-chat"},
    }}, "email": "private@example.com"}
    row = paginate_rows(value, authenticated=authenticated)["items"][0]
    assert row["result"]["deliveries"]["email"]["status"] == "failed"
    assert row["result"]["deliveries"]["email"]["reason"] == "smtp_rejected"
    assert row["result"]["deliveries"]["email"]["smtp_code"] == 550
    assert row["result"]["deliveries"]["feishu"]["status"] == "sent"
    assert "private" not in json.dumps(row)
    assert "email" not in row


def test_safe_email_status_can_be_filtered_without_opening_recipient_projection():
    value = {"result": {"deliveries": {"email": {"status": "failed", "reason": "smtp_rejected", "recipient": "hidden@example.org", "detail": "private provider response"}}}}
    result = paginate_rows(value, fields=["result.deliveries.email.status"], filters=[{"field": "result.deliveries.email.status", "op": "eq", "value": "failed"}])
    assert result["items"] == [{"result.deliveries.email.status": "failed"}]
    with pytest.raises(ValueError):
        paginate_rows(value, fields=["result.deliveries.email.recipient"])
    result = paginate_rows(value, collection="result.deliveries.email")
    assert "hidden@example" not in str(result)
    assert "private provider" not in str(result)
