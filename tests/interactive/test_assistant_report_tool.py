"""The Feishu tool exposes only validated, official A-share reports."""

from unittest.mock import Mock

import pytest

from src.interactive.assistant.service import FeishuAssistant
from src.interactive.assistant.settings import AssistantSettings
from src.interactive.assistant.store import ProposalStore


@pytest.fixture
def report_bot(tmp_path):
    bot = FeishuAssistant(
        {},
        Mock(return_value=(True, "ok")),
        project_root=tmp_path,
        configuration=Mock(),
        research=Mock(),
        client=Mock(),
        store=ProposalStore(tmp_path / "assistant.sqlite3"),
        start_workers=False,
    )
    yield bot
    bot.stop()


def test_report_tool_is_advertised_for_natural_language_requests(report_bot):
    tools = report_bot.tool_schema()
    tool = next(
        item["function"]
        for item in tools
        if item["function"]["name"] == "fetch_a_share_report"
    )
    assert tool["parameters"]["required"] == ["code", "report_type"]
    assert "DeepSeek 视觉" in tool["description"]
    assert any(item["function"]["name"] == "search_report_text" for item in tools)


def test_report_tool_validates_inputs_before_discovery(report_bot):
    with pytest.raises(ValueError, match="六位"):
        report_bot.call_tool(
            "chat",
            "sender",
            "fetch_a_share_report",
            {"code": "123", "report_type": "annual"},
        )
    with pytest.raises(ValueError, match="未知"):
        report_bot.call_tool(
            "chat",
            "sender",
            "fetch_a_share_report",
            {
                "code": "600519",
                "report_type": "annual",
                "url": "https://evil.test/a.pdf",
            },
        )


def test_report_tool_routes_to_server_discovery_and_returns_indexed_result(
    report_bot, monkeypatch
):
    from src.data import cninfo_reports

    fetch = Mock(
        return_value={
            "status": "succeeded",
            "code": "002594",
            "report_year": 2025,
            "text_file_id": "file_report_text",
        }
    )
    service = Mock()
    service.fetch = fetch
    monkeypatch.setattr(
        cninfo_reports, "CninfoReportService", Mock(return_value=service)
    )
    result = report_bot.call_tool(
        "chat",
        "sender",
        "fetch_a_share_report",
        {"code": "002594", "report_type": "half_year", "year": 2025},
    )
    fetch.assert_called_once_with("002594", "half_year", 2025)
    assert result["text_file_id"] == "file_report_text"


def test_vision_client_sends_page_images_to_flash_as_user_content(monkeypatch):
    from src.interactive.assistant import client as client_module

    response = Mock()
    response.status_code = 200
    response.json.return_value = {
        "choices": [{"message": {"content": "[PAGE 3] 资产总计"}}]
    }
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    request = Mock(return_value=response)
    monkeypatch.setattr(client_module.requests, "post", request)
    client = client_module.DeepSeekAssistantClient(
        {"llm": {"api_key": "test-key"}},
        AssistantSettings(request_retries=0),
    )
    result = client.read_report_images([{"page": 3, "image": b"jpeg-bytes"}])
    payload = request.call_args.kwargs["json"]
    assert result.startswith("[PAGE 3]")
    assert payload["model"] == "deepseek-flash"
    content = payload["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
