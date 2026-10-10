"""Two material coverage gates; never an investment/publication approval."""

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .fr_pack_contract import FIELDS

CONFIG = Path(__file__).resolve().parents[2] / "config/fr_pack_gate.json"


def load_policy():
    policy = json.loads(CONFIG.resolve().read_text(encoding="utf8"))
    critical = policy["critical_fields"]
    threshold = Decimal(str(policy["minimum_fill_rate"]))
    if (
        not critical
        or len(set(critical)) != len(critical)
        or not set(critical) <= set(FIELDS)
        or not threshold.is_finite()
        or not 0 <= threshold <= 1
    ):
        raise ValueError("invalid_material_gate_policy")
    return policy


def assess_materials(
    fields, metadata, source, *, invalid_fields=(), reviewed_exclusions=(), policy=None
):
    """Exclusions must come from source review, never model self-certification.

    A table quote need not preserve text extraction order. Numeric existence is
    checked in its original pages; known factual errors invalidate the field.
    Coverage is not a new claim that all accounting interpretations are correct.
    """
    policy = policy or load_policy()
    rows = list(fields.values()) if isinstance(fields, dict) else fields
    usable, excluded, missing = [], [], {}
    critical = set(policy["critical_fields"])
    for metric in FIELDS:
        matches = [f for f in rows if f.get("metric") == metric]
        if len(matches) != 1:
            missing[metric] = "missing_or_duplicate"
            continue
        field = matches[0]
        if metric in invalid_fields:
            missing[metric] = "confirmed_error"
            continue
        pages = field.get("pages") or field.get("source_ref", {}).get("pages", [])
        valid_pages = bool(pages) and all(str(p) in source["pages"] for p in pages)
        original = (
            "".join(source["pages"][str(p)] for p in pages) if valid_pages else ""
        )
        normalized = lambda text: (
            re.sub(r"\s+", "", str(text))
            .replace(",", "")
            .replace("，", "")
            .replace("−", "-")
            .replace("－", "-")
        )
        quote = field.get("quote")
        if (
            metric not in critical
            and metric in reviewed_exclusions
            and field.get("status") in {"not_disclosed", "not_applicable"}
            and quote
            and valid_pages
            and normalized(quote) in normalized(original)
        ):
            excluded.append(metric)
            continue
        if field.get("status") != "obtained":
            missing[metric] = field.get("status", "missing")
            continue
        if (
            field.get("period_end") != metadata.get("period_end")
            or field.get("scope", "consolidated") != "consolidated"
        ):
            missing[metric] = "period_or_scope_mismatch"
            continue
        try:
            raw = normalized(field.get("value"))
            if raw.startswith("(") and raw.endswith(")"):
                raw = "-" + raw[1:-1]
            value = Decimal(raw)
            if not value.is_finite() or field.get("unit") not in {"元", "万元", "亿元"}:
                raise ValueError("invalid_value_unit")
        except (InvalidOperation, ValueError, TypeError):
            missing[metric] = "invalid_value_unit"
            continue
        # Compare complete numeric tokens, so 123 does not match 1234.
        numeric_tokens = re.findall(
            r"(?<![\d.])(?:\(\d+(?:\.\d+)?\)|-?\d+(?:\.\d+)?)(?![\d.])",
            original.replace(",", "")
            .replace("，", "")
            .replace("−", "-")
            .replace("－", "-"),
        )
        literal = any(
            Decimal("-" + token[1:-1] if token.startswith("(") else token) == value
            for token in numeric_tokens
        )
        if not valid_pages or not quote or not literal:
            missing[metric] = "source_value_not_located"
            continue
        usable.append(metric)
    denominator = len(FIELDS) - len(excluded)
    rate = Decimal(len(usable)) / Decimal(denominator) if denominator else Decimal(0)
    missing_critical = sorted(critical - set(usable))
    gates = {
        "critical_fields": {
            "passed": not missing_critical,
            "required": sorted(critical),
            "missing": missing_critical,
        },
        "fill_rate": {
            "passed": rate >= Decimal(str(policy["minimum_fill_rate"])),
            "numerator": len(usable),
            "denominator": denominator,
            "rate": float(rate),
            "minimum": policy["minimum_fill_rate"],
        },
    }
    return {
        "policy": policy,
        "gates": gates,
        "passed": all(x["passed"] for x in gates.values()),
        "usable_fields": usable,
        "excluded_fields": excluded,
        "missing_fields": missing,
        "source_digest": hashlib.sha256(
            json.dumps(source, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest(),
        "meaning": "资料完整性；管理层解释及公司披露FCF不参与门槛；不是投资结论或全项会计审查通过",
    }


def gate_html(gate):
    from .fr_pack_render import esc

    keys = gate["gates"]["critical_fields"]
    fill = gate["gates"]["fill_rate"]
    missing = "、".join(FIELDS[k][0] for k in keys["missing"]) or "无"
    return (
        f'<section id="material-gate"><h2>资料完整性：{"通过" if gate["passed"] else "未通过"}</h2>'
        f"<p>关键项：{len(keys['required']) - len(keys['missing'])}/{len(keys['required'])}；缺少：{esc(missing)}</p>"
        f"<p>有效填充率：{fill['numerator']}/{fill['denominator']} = {fill['rate']:.2%}；要求 ≥ {fill['minimum']:.0%}</p>"
        "<p>仅以上两个资料门槛。未披露的管理层解释列作限制；公司未披露FCF由代码计算。已确认错误的字段不计入有效填充；既有引用和会计问题继续展示。</p>"
        '<p><a href="material_gate.json">字段明细与门槛版本</a></p></section>'
    )
