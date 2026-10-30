"""Local acceptance evidence. Never starts a Bot or sends a real message.

--live-model calls the configured model with an explicitly synthetic image and
isolated task data. This cannot establish real brokerage screenshot accuracy.
--live-market reads public raw prices only. No production state is touched.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "data" / "acceptance" / "personal_monitor_20261021"
TZ = ZoneInfo("Asia/Shanghai")


def write_json(name, value):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def probe_market(config):
    from src.personal_monitor_runtime import SourcedObservationProvider

    provider = SourcedObservationProvider(config)
    rows = []
    for code in ("601088", "01088", "AAPL"):
        try:
            quote = provider.screenshot_quote(code, "2026-10-20")
            rows.append({"code": code, "status": "passed", "quote": quote})
        except Exception as exc:  # noqa: BLE001 - persist each failed acceptance
            rows.append({"code": code, "status": "failed", "error": str(exc)})
    write_json("live_market.json", rows)
    return rows


def probe_model(config):
    from PIL import Image, ImageDraw, ImageFont

    from src.interactive.assistant.client import DeepSeekAssistantClient
    from src.interactive.assistant.harness import ConversationHarness
    from src.interactive.assistant.personal_tools import (
        PERSONAL_PROMPT,
        PersonalTools,
        personal_tool_schema,
    )
    from src.interactive.assistant.settings import AssistantSettings
    from src.personal_monitor import MonitorStore

    OUT.mkdir(parents=True, exist_ok=True)
    # Independent truth is known because this is a rendered synthetic fixture.
    image = Image.new("RGB", (1500, 650), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 32)
    lines = [
        "SYNTHETIC ACCEPTANCE FIXTURE / 非真实账户",
        "账户: acceptance-isolated",
        "数据日期: 2026-10-20",
        "代码       名称       数量(股)   价格(元)   市值(元)    币种",
        "601088    中国神华    1000        51.30      51300.00    CNY",
        "01088     中国神华    500         41.60      20800.00    HKD",
    ]
    for i, line in enumerate(lines):
        draw.text((30, 28 + i * 82), line, fill="black", font=font)
    image_path = OUT / "synthetic_holdings.png"
    image.save(image_path)
    settings = AssistantSettings(
        enabled=True,
        request_retries=0,
        max_tokens=4096,
        max_tool_rounds=8,
        conversation_timeout_seconds=180,
    )
    client = DeepSeekAssistantClient(config, settings)
    result = {"image_kind": "synthetic_not_brokerage", "cases": []}
    try:
        document = client.read_position_images(
            [
                {
                    "image": image_path.read_bytes(),
                    "mime_type": "image/png",
                }
            ],
            context="这是隔离测试用合成持仓表，仅提取图片上清楚的信息。",
        )
        result["ocr_document"] = document
        by_code = {row.get("code"): row for row in document["positions"]}
        expected = {
            "601088": (1000, 51.3, 51300, "CNY"),
            "01088": (500, 41.6, 20800, "HKD"),
        }
        correct = len(by_code) == 2 and all(
            code in by_code
            and tuple(
                by_code[code].get(key)
                for key in ("quantity", "price", "market_value", "currency")
            )
            == values
            for code, values in expected.items()
        )
        result["cases"].append({"name": "synthetic_live_ocr", "passed": correct})
    except Exception as exc:  # noqa: BLE001 - retain failure and run remaining cases
        result["cases"].append(
            {"name": "synthetic_live_ocr", "passed": False, "error": str(exc)}
        )
    # A unique isolated DB on every run; never open the application runtime DB.
    db_path = OUT / (
        "live_semantic_" + datetime.now(TZ).strftime("%H%M%S%f") + ".sqlite3"
    )
    store = MonitorStore(db_path)
    personal = PersonalTools(store, client=client)
    owner, chat = "acceptance-test-owner", "acceptance-test-chat"
    base = datetime.now(TZ).replace(microsecond=0)
    scenarios = [
        ("create", "28天后提醒我决定是否滚存逆回购，只建这一条。"),
        ("edit", "把刚才决定是否滚存逆回购那条，改为从现在起30天后提醒我。"),
        ("duplicate", "我重复一下，刚才说的那笔逆回购到期提醒就是已有那条，别重复建。"),
        ("another", "另建一条，与已有提醒同一时间，内容是复核资金余额。"),
        ("cancel", "把复核资金余额那条提醒取消，决定是否滚存那条保留。"),
    ]
    history = []
    for index, (name, text) in enumerate(scenarios):
        context = {
            "id": f"live-{index}",
            "message_id": f"live-{index}",
            "effective_time": base,
            "original_text": text,
        }
        events = []
        harness = ConversationHarness(client, settings, ROOT, threading.Event())
        messages = [
            {
                "role": "system",
                "content": PERSONAL_PROMPT
                + "\n当前消息时刻："
                + base.isoformat()
                + "；当前会话："
                + chat,
            },
            *history,
            {"role": "user", "content": text},
        ]
        before = store.status(owner)
        try:
            answer = harness.run(
                messages,
                personal_tool_schema(),
                lambda action, args, ctx=context: personal.invoke(
                    owner, chat, action.removeprefix("personal_monitor_"), args, ctx
                ),
                lambda kind, data, sink=events: sink.append(
                    {"kind": kind, "data": data}
                ),
                lambda value: value,
                lambda answer: [],
            )
            after = store.status(owner)
            raw_model_answer = answer
            if context.get("personal_receipts"):
                answer = personal.render_receipts(context)
            reminders = after["reminders"]
            if name == "create":
                passed = len(reminders) == 1 and datetime.fromisoformat(
                    reminders[0]["due_at"]
                ) == base + timedelta(days=28)
            elif name == "edit":
                passed = len(reminders) == 1 and datetime.fromisoformat(
                    reminders[0]["due_at"]
                ) == base + timedelta(days=30)
            elif name == "duplicate":
                passed = before["reminders"] == reminders
            elif name == "another":
                passed = (
                    len(reminders) == 2 and len({r["due_at"] for r in reminders}) == 1
                )
            else:
                passed = (
                    len(reminders) == 2
                    and sum(row["state"] == "cancelled" for row in reminders) == 1
                )
            result["cases"].append(
                {
                    "name": name,
                    "passed": passed,
                    "answer": answer,
                    "raw_model_answer": raw_model_answer,
                    "state": after,
                    "operations": context.get("personal_receipts", []),
                    "events": events,
                }
            )
            history += [
                {"role": "user", "content": text},
                {"role": "assistant", "content": answer},
            ]
        except Exception as exc:  # noqa: BLE001 - retain failure and remaining frontier
            result["cases"].append(
                {"name": name, "passed": False, "error": str(exc), "events": events}
            )
        write_json("live_model.json", result)
    client.stop()
    return [{"name": row["name"], "passed": row["passed"]} for row in result["cases"]]


def check_scope():
    baseline_path = OUT / "baseline.json"
    if not baseline_path.exists():
        return {"status": "missing_baseline"}
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    mismatches = []
    for name, expected in baseline["protected"].items():
        path = ROOT / name
        actual = (
            hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
        )
        if actual != expected:
            mismatches.append(name)
    result = {
        "protected_count": len(baseline["protected"]),
        "mismatches": mismatches,
        "passed": not mismatches,
        "deployed": False,
        "real_bot_started": False,
        "real_feishu_messages_sent": 0,
    }
    write_json("scope_check.json", result)
    return result


def probe_rule_model(config):
    """Live natural-language configuration, controlled financial observations."""
    from src.interactive.assistant.client import DeepSeekAssistantClient
    from src.interactive.assistant.harness import ConversationHarness
    from src.interactive.assistant.personal_tools import (
        PERSONAL_PROMPT,
        PersonalTools,
        personal_tool_schema,
    )
    from src.interactive.assistant.settings import AssistantSettings
    from src.personal_monitor import MonitorEngine, MonitorStore
    from src.personal_monitor_runtime import bound_cashflow_return

    settings = AssistantSettings(
        enabled=True,
        request_retries=0,
        max_tokens=8192,
        max_tool_rounds=10,
        conversation_timeout_seconds=180,
    )
    client = DeepSeekAssistantClient(config, settings)
    store = MonitorStore(
        OUT / ("live_rule_" + datetime.now(TZ).strftime("%H%M%S%f") + ".sqlite3")
    )
    personal = PersonalTools(store)
    now = datetime.now(TZ).replace(microsecond=0)
    context = {"id": "live-rule", "message_id": "live-rule", "effective_time": now}
    text = (
        "请直接在个人隔离测试账户qa-case记录当前持仓601088中国神华1000股，币种CNY，"
        "创建预期年化回报小于6%和小于4%各提醒一次的规则，不提卖出比例。"
        "这些是假设验收输入，不是真实持仓。研究使用我明确选定的测试版本qa-v1、场景base，"
        "模型equity_cashflow_dcf_v1，来源是本条用户提供的隔离测试假设："
        "cashflow_per_share=3，growth_first=0.02，growth_second=0.02，terminal_growth=0.02；"
        "发送回当前会话。直接执行这些个人监控操作并返回结果。"
    )
    context["original_text"] = text
    events = []
    result = {"kind": "live_model_configuration_with_controlled_financial_fixtures"}
    try:
        answer = ConversationHarness(client, settings, ROOT, threading.Event()).run(
            [
                {
                    "role": "system",
                    "content": PERSONAL_PROMPT
                    + "\n当前消息时间："
                    + now.isoformat()
                    + "；当前会话acceptance-test-chat",
                },
                {"role": "user", "content": text},
            ],
            personal_tool_schema(),
            lambda action, args: personal.invoke(
                "acceptance-owner",
                "acceptance-test-chat",
                action.removeprefix("personal_monitor_"),
                args,
                context,
            ),
            lambda kind, data: events.append({"kind": kind, "data": data}),
            lambda value: value,
            lambda answer: [],
        )
        account = store.status("acceptance-owner", "qa-case")["accounts"][0]
        rule = account["rules"][0]
        definition = rule["definition"]
        assert definition["metric"] == "expected_return"
        assert sorted(t["value"] for t in definition["tiers"]) == [0.04, 0.06]
        assert all(t["operator"] == "lt" for t in definition["tiers"])
        binding = definition["bindings"]["601088"]
        assert binding["version"] == "qa-v1" and binding["scenario"] == "base"
        assert account["positions"][0]["quantity"] == 1000
        engine = MonitorEngine(store)
        evaluations = []
        for price in (100, 100, 200):
            expected_return = bound_cashflow_return(binding, price, "CNY")
            observation = {
                "ready": True,
                "expected_return": expected_return,
                "quote": {
                    "price": price,
                    "currency": "CNY",
                    "as_of": now.isoformat(),
                    "source": "controlled_financial_fixture",
                    "market_open": True,
                },
                "research": {
                    key: binding[key] for key in ("model", "version", "scenario")
                },
            }
            evaluated = engine.evaluate(
                "acceptance-owner", "qa-case", {"601088": observation}, now
            )
            evaluations.append(evaluated)
            entry = engine.claim_delivery("acceptance-worker", now)
            if entry:
                assert engine.mark_submitted(entry["id"], entry["token"], now=now)
                assert engine.finish_delivery(
                    entry["id"],
                    entry["token"],
                    "sent",
                    now=now,
                    metadata={"transport": "controlled_no_real_send"},
                )
        assert [len(e["enqueued"]) for e in evaluations] == [1, 0, 1]
        result.update(
            passed=True,
            answer=personal.render_receipts(context),
            raw_model_answer=answer,
            evaluations=evaluations,
            state=store.status("acceptance-owner"),
        )
    except Exception as exc:  # noqa: BLE001 - failure is an acceptance result
        result.update(
            passed=False, error=str(exc), state=store.status("acceptance-owner")
        )
    finally:
        client.stop()
    result["events"] = events
    write_json("live_rule_model.json", result)
    return {"passed": result["passed"], "error": result.get("error")}


def probe_screenshot_flow(config):
    """Real vision/model/quotes/SQLite; controlled image transport and replies."""
    from src.interactive.assistant.client import DeepSeekAssistantClient
    from src.interactive.assistant.service import FeishuAssistant
    from src.interactive.assistant.settings import AssistantSettings

    image_path = OUT / "synthetic_holdings.png"
    if not image_path.is_file():
        return {
            "passed": False,
            "error": "Run --live-model to prepare the synthetic fixture",
        }
    workspace = OUT / ("live_screenshot_" + datetime.now(TZ).strftime("%H%M%S%f"))
    workspace.mkdir()
    captures = []

    def capture_reply(route, text):
        captures.append({"route": route, "text": text})
        return True, "controlled_local_capture_no_real_send"

    settings = AssistantSettings(
        enabled=True,
        request_retries=0,
        max_tokens=8192,
        conversation_timeout_seconds=240,
        max_tool_rounds=8,
    )
    service_config = {"interactive": {"feishu": {"assistant": settings.dict()}}}
    service = FeishuAssistant(
        service_config,
        capture_reply,
        project_root=workspace,
        client=DeepSeekAssistantClient(config, settings),
        start_workers=False,
        image_loader=lambda message, image: {
            "image": image_path.read_bytes(),
            "mime_type": "image/png",
        },
        personal_route_allowed=lambda chat: chat == "acceptance-test-chat",
    )
    result = {
        "boundaries": {
            "vision_model": "real",
            "semantic_model": "real",
            "quote_lookup": "real_public_sources",
            "sqlite": "real_isolated",
            "image_transport": "local_fixture",
            "notification": "local_capture",
        }
    }
    try:
        service.handle(
            "acceptance-test-chat",
            "acceptance-owner",
            "将这张合成验收截图的持仓导入独立个人测试账户。图片上账户与日期明确时直接导入；"
            "只导入持仓，不创建提醒，不创建交易。",
            "live-image-flow",
            attachments=[{"message_id": "local-fixture", "image_key": "synthetic"}],
            message_time={"create_time": str(int(datetime.now(TZ).timestamp() * 1000))},
        )
        state = service.personal_store.status("acceptance-owner")
        result["initial_state"] = state
        result["manual_correction_required"] = False
        if state["accounts"] and state["accounts"][0].get("pending_issues"):
            # The fixture intentionally retains a generic '元' column header.
            # A conservative model may flag the HK row; test the agreed manual
            # correction path instead of forcing that uncertainty into a fact.
            result["manual_correction_required"] = True
            service.handle(
                "acceptance-test-chat",
                "acceptance-owner",
                "手工更正刚才截图的问题行：账户acceptance-isolated，证券01088中国神华，"
                "数量500股，币种HKD，价格41.60港元，市值20800港元。"
                "数据时间明确为2026-10-20T00:00:00+08:00，请直接更新这一行，其他行保留。",
                "live-image-manual-correction",
            )
            state = service.personal_store.status("acceptance-owner")
        assert len(state["accounts"]) == 1
        account = state["accounts"][0]
        assert account["account"] == "acceptance-isolated"
        positions = {row["instrument"]: row for row in account["positions"]}
        assert positions["601088"]["quantity"] == 1000
        assert positions["01088"]["quantity"] == 500
        assert positions["601088"]["quote_evidence"]["source"] == "qq_raw_daily"
        assert positions["01088"]["currency"] == "HKD"
        if positions["01088"]["source"] == "screenshot":
            assert positions["01088"]["quote_evidence"]["source"] == "qq_raw_daily"
        else:
            assert (
                result["manual_correction_required"]
                and positions["01088"]["source"] == "manual"
            )
        assert not account["pending_issues"]
        assert not state["reminders"] and not state["outbox"]
        assert any("实际操作回执" in row["text"] for row in captures)
        result.update(passed=True, state=state, captures=captures)
    except Exception as exc:  # noqa: BLE001 - store evidence instead of claiming acceptance
        result.update(
            passed=False,
            error=str(exc),
            state=service.personal_store.status("acceptance-owner"),
            captures=captures,
        )
    finally:
        service.stop()
    write_json("live_screenshot_flow.json", result)
    return {"passed": result["passed"], "error": result.get("error")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-model", action="store_true")
    parser.add_argument("--live-market", action="store_true")
    parser.add_argument("--live-rule-model", action="store_true")
    parser.add_argument("--live-screenshot-flow", action="store_true")
    args = parser.parse_args()
    result = {"scope": check_scope()}
    if (
        args.live_model
        or args.live_market
        or args.live_rule_model
        or args.live_screenshot_flow
    ):
        from src.core.config_store import ConfigStore

        config = ConfigStore().load_runtime()
        if args.live_market:
            result["live_market"] = probe_market(config)
        if args.live_model:
            result["live_model"] = probe_model(config)
        if args.live_rule_model:
            result["live_rule_model"] = probe_rule_model(config)
        if args.live_screenshot_flow:
            result["live_screenshot_flow"] = probe_screenshot_flow(config)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    checks = [result["scope"].get("passed", False)]
    checks += [row["passed"] for row in result.get("live_model", [])]
    checks += [row["status"] == "passed" for row in result.get("live_market", [])]
    if "live_rule_model" in result:
        checks.append(result["live_rule_model"]["passed"])
    if "live_screenshot_flow" in result:
        checks.append(result["live_screenshot_flow"]["passed"])
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
