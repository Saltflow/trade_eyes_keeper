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
from datetime import datetime
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
    from src.interactive.assistant.settings import AssistantSettings

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
    parser.add_argument("--live-screenshot-flow", action="store_true")
    args = parser.parse_args()
    result = {"scope": check_scope()}
    if args.live_model or args.live_market or args.live_screenshot_flow:
        from src.core.config_store import ConfigStore

        config = ConfigStore().load_runtime()
        if args.live_market:
            result["live_market"] = probe_market(config)
        if args.live_model:
            result["live_model"] = probe_model(config)
        if args.live_screenshot_flow:
            result["live_screenshot_flow"] = probe_screenshot_flow(config)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    checks = [result["scope"].get("passed", False)]
    checks += [row["passed"] for row in result.get("live_model", [])]
    checks += [row["status"] == "passed" for row in result.get("live_market", [])]
    if "live_screenshot_flow" in result:
        checks.append(result["live_screenshot_flow"]["passed"])
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
