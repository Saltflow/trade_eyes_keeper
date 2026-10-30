"""Natural-language valuation research must not be hijacked by retired archives."""

from unittest.mock import Mock

import pytest

from src.interactive.assistant.service import FeishuAssistant
from src.interactive.assistant.store import ProposalStore


@pytest.fixture
def assistant(tmp_path):
    bot = FeishuAssistant(
        {"interactive": {"feishu": {"assistant": {"enabled": True}}}},
        Mock(return_value=(True, "ok")),
        project_root=tmp_path,
        configuration=Mock(),
        research=Mock(),
        client=Mock(),
        store=ProposalStore(tmp_path / "assistant.sqlite3"),
        start_workers=False,
    )
    bot.research.query_stock_fundamentals.return_value = {
        "selected": None,
        "status": "unavailable",
        "code": "600036",
    }
    bot.harness.run = Mock(return_value="本轮资料仍有缺口，不能据此判断买入。")
    yield bot
    bot.stop()


@pytest.mark.parametrize(
    "question",
    [
        "600036的ROIC和正常化盈利怎么样",
        "查看价投候选池",
        "fundamental 600036",
        "600036的 FCF yield，直接判断值得买吗",
        "600036 implied Ke 和安全边际",
        "600036正常化盈利缺数据也猜一下",
        "600036值得买吗",
        "600036估值是多少",
        "美的合理价是多少",
        "AAPL fair value",
        "推荐便宜股票",
    ],
)
def test_valuation_question_enters_research_instead_of_retired_archive(
    assistant, question
):
    assistant.handle("chat", "owner", question)
    assistant.harness.run.assert_called_once()
    assert assistant.harness.run.call_args.kwargs["require_retrieval"] is True
    prompt = str(assistant.harness.run.call_args.args[0])
    assert "不猜测数值" in prompt
    assert "旧价投执行器退役" in prompt
    assert "只能引用Fundamental Quant当前有效核验记录" not in prompt
    reply = assistant.send_message.call_args.args[1]
    assert reply == "本轮资料仍有缺口，不能据此判断买入。"
    assert "价投研究已退役；查询仅返回历史" not in reply


def test_followup_retains_question_and_owner_scoped_research_context(assistant):
    assistant.handle("chat", "owner", "600036的ROIC如何")
    assistant.handle("chat", "owner", "那继续看看")
    args = assistant.harness.run.call_args
    assert args.kwargs["require_retrieval"] is True
    assert any(m.get("content") == "600036的ROIC如何" for m in args.args[0])
    assistant.handle("chat", "other-owner", "那继续看看")
    messages = assistant.harness.run.call_args.args[0]
    assert not any(m.get("content") == "600036的ROIC如何" for m in messages)


@pytest.mark.parametrize("question", ["你好", "谢谢", "回测600036", "把日报改成每周"])
def test_chat_and_proposals_still_use_existing_confirmation_path(assistant, question):
    assistant.handle("chat", "owner", question)
    assistant.harness.run.assert_called_once()
    assistant.configuration.apply.assert_not_called()
    assert assistant._jobs.empty()


def test_clear_context_does_not_restore_old_research_topic(assistant):
    assistant.handle("chat", "owner", "600036的ROIC")
    assistant.handle("chat", "owner", "清空对话")
    assistant.handle("chat", "owner", "那继续看看")
    messages = assistant.harness.run.call_args.args[0]
    assert not any(m.get("content") == "600036的ROIC" for m in messages)
