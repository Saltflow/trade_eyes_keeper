"""Price recorded provider usage without calling a model or changing its budget."""

from __future__ import annotations

import argparse
import html
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path


def render(root):
    auth = json.loads((root / "authorization.json").read_text("utf8"))
    rates = auth["rate_profile"]
    calls, total_low, total_high = [], Decimal(0), Decimal(0)
    for path in sorted((root / "private_calls").glob("*/state.json")):
        state = json.loads(path.read_text("utf8"))
        if not state.get("reserved"):
            continue
        usage = state.get("usage", {})
        inputs, outputs = usage.get("input_tokens"), usage.get("output_tokens")
        hits = usage.get("provider_cache_usage", {}).get("prompt_cache_hit_tokens")
        row = {
            key: state.get(key)
            for key in ("call_id", "status", "started_at", "received_at")
        }
        row.update(
            input_tokens=inputs, output_tokens=outputs, input_cache_hit_tokens=hits
        )
        if type(inputs) is int and type(outputs) is int and type(hits) is int:
            moment = datetime.fromisoformat(state["started_at"]).astimezone(
                timezone.utc
            )
            maybe_peak = moment.weekday() < 5 and (
                1 <= moment.hour < 4 or 6 <= moment.hour < 10
            )
            periods = ["off_peak", "peak"] if maybe_peak else ["off_peak"]
            fees = []
            for period in periods:
                price = rates[period]
                fees.append(
                    (
                        Decimal(hits) * Decimal(price["input_hit"])
                        + Decimal(inputs - hits) * Decimal(price["input_miss"])
                        + Decimal(outputs) * Decimal(price["output"])
                    )
                    / Decimal(rates["per_tokens"])
                )
            low, high = min(fees), max(fees)
            row.update(
                fee_lower=str(low),
                fee_upper=str(high),
                billing_window="off_peak"
                if not maybe_peak
                else "holiday_status_not_bound",
            )
            total_low += low
            total_high += high
        else:
            row["fee_status"] = "pending_or_unknown_usage_not_zero"
        calls.append(row)
    unknown = [c["call_id"] for c in calls if "fee_status" in c]
    balances = {}
    for name in ("before", "after"):
        path = root / ("balance_" + name + ".json")
        if path.is_file():
            balances[name] = json.loads(path.read_text("utf8"))
    delta = None
    if (root / "terminal.json").is_file() and all(
        name in balances for name in ("before", "after")
    ):
        matched = []
        for name in ("before", "after"):
            matched.append(
                next(
                    (
                        b
                        for b in balances[name].get("balance_infos", [])
                        if b.get("currency") == rates["currency"]
                    ),
                    None,
                )
            )
        if all(matched):
            delta = str(
                Decimal(matched[0]["total_balance"])
                - Decimal(matched[1]["total_balance"])
            )
    result = {
        "campaign_id": auth["id"],
        "currency": rates["currency"],
        "status": "known_usage_only_total_unknown"
        if unknown
        else "provider_usage_priced_at_bound_tariff_not_invoice",
        "known_usage_fee_lower": str(total_low),
        "known_usage_fee_upper": str(total_high),
        "model_calls_reserved": len(calls),
        "unknown_or_pending_calls": unknown,
        "input_tokens": sum(c.get("input_tokens") or 0 for c in calls),
        "output_tokens": sum(c.get("output_tokens") or 0 for c in calls),
        "input_cache_hit_tokens": sum(
            c.get("input_cache_hit_tokens") or 0 for c in calls
        ),
        "account_balance_decrease": delta,
        "account_balance_scope": "includes_other_account_activity",
        "rate_profile": rates,
        "calls": calls,
    }
    (root / "costs.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8"
    )
    body = """<!doctype html><meta charset='utf-8'><title>Pi 研究真实用量与费用</title>
<style>body{max-width:1100px;margin:35px auto;padding:20px;font:16px/1.8 system-ui;color:#183347}
table{border-collapse:collapse;width:100%}td,th{padding:8px;border-bottom:1px solid #ddd}a{color:#087ba6}</style>
<h1>本轮真实用量与费用</h1>"""
    body += "<p>真实输入 {:,}、缓存命中 {:,}、输出 {:,} token；已预留调用 {} 次。</p>".format(
        result["input_tokens"],
        result["input_cache_hit_tokens"],
        result["output_tokens"],
        len(calls),
    )
    body += f"<p>按本轮绑定费率计，已知用量费用：{total_low:.4f}—{total_high:.4f} 元。"
    body += (
        "尚有处理中或未知用量的请求，不能把这些请求记作零费用。</p>"
        if unknown
        else "全部调用用量已返回；此处是用量计价，未冒充服务商发票。</p>"
    )
    if delta is not None:
        body += f"<p>账户余额减少 {html.escape(delta)} 元；该数包含同账户其他并发活动，不能直接全部归因本轮。</p>"
    body += "<table><tr><th>调用</th><th>状态</th><th>输入</th><th>命中缓存</th><th>输出</th><th>费用（元）</th></tr>"
    for c in calls:
        values = [
            c["call_id"],
            c["status"],
            c["input_tokens"],
            c["input_cache_hit_tokens"],
            c["output_tokens"],
            c.get("fee_lower", "未知或处理中"),
        ]
        body += (
            "<tr>"
            + "".join("<td>" + html.escape(str(v)) + "</td>" for v in values)
            + "</tr>"
        )
    body += (
        "</table><p><a href='costs.json'>完整逐调用记录</a> · <a href='"
        + html.escape(rates["source"], quote=True)
        + "'>费率来源</a></p>"
    )
    (root / "costs.html").write_text(body, encoding="utf8")
    return {k: v for k, v in result.items() if k not in {"calls", "rate_profile"}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    print(json.dumps(render(parser.parse_args().root), ensure_ascii=False))
