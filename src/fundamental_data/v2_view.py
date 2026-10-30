"""Views of explicit records. Unmapped content is shown, never guessed."""

import csv
import html
import io
import json

import bleach
import markdown

from .v2_schema import attribute_context, context_index, stable
from .v2_transport import event, save

REASONS = {
    "not_applicable": "报告明确不适用",
    "not_disclosed": "报告明确未披露",
    "not_found": "本次未找到",
    "context_uncertain": "口径未确定",
}
STATISTICS = {
    "point": "时点",
    "period_total": "期间总额",
    "period_average": "期间平均",
    "period_measure": "期间指标",
    "unknown": "未定",
}


def cell(value):
    if value is None:
        return "n/a"
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    return html.escape(str(value)).replace("|", "\\|").replace("\n", "<br>")


def _grid(headers, rows):
    lines = [
        "| " + " | ".join(map(cell, headers)) + " |",
        "|" + "---|" * len(headers),
    ]
    lines += ["| " + " | ".join(map(cell, row)) + " |" for row in rows]
    return "\n".join(lines) + "\n\n"


def _period(period):
    if not isinstance(period, dict):
        return "期间未定"
    if period.get("kind") == "instant":
        return str(period.get("end") or "时点未定") + " 时点"
    if period.get("kind") == "duration":
        return str(period.get("start")) + " 至 " + str(period.get("end"))
    return "期间未定"


def _statistic(statistic):
    if not isinstance(statistic, dict):
        return "未定"
    text = STATISTICS.get(statistic.get("kind"), "未定")
    method = statistic.get("method", {})
    if method.get("state") == "stated":
        text += "；" + str(method.get("text"))
    if statistic.get("annualized") in {"yes", "no", "not_specified"}:
        text += (
            "；"
            + {
                "yes": "年化",
                "no": "未年化",
                "not_specified": "年化口径未说明",
            }[statistic["annualized"]]
        )
    return text


def _version(version):
    if not isinstance(version, dict):
        return "披露版本未定"
    return (
        str(version.get("report_period"))
        + "披露［"
        + str(version.get("report_id", ""))[:14]
        + "］"
    )


def _scope(scope):
    kind = {
        "group": "集团",
        "parent": "母公司",
        "regulatory": "监管并表",
        "subsidiary": "子公司",
        "segment": "业务分部",
        "other": "其他",
        "unknown": "范围未定",
    }.get(scope.get("kind"), str(scope.get("kind")))
    return kind + "／" + str(scope.get("entity"))


def _column(column):
    text = str(column["label"]) + "\n" + _period(column["period"])
    adjustment = column["adjustment"]
    text += (
        "\n"
        + {
            "original": "明确原始值",
            "restated": "明确调整／重述",
            "not_specified": "调整状态未说明",
        }[adjustment["status"]]
    )
    if adjustment.get("label"):
        text += "：" + adjustment["label"]
    if column.get("comparison"):
        comparison = column["comparison"]
        text += "\n比较基期：" + _period(comparison.get("base_period"))
        if comparison.get("definition"):
            text += "；" + comparison["definition"]
    if "reported_in" in column:
        text += "\n" + _version(column["reported_in"])
    return text


def _value(row, key, history=False):
    if row.get("row_kind") == "heading" and not row["values"]:
        return ""
    if key not in row["values"]:
        return "未覆盖" if history else "列键未提供"
    value = row["values"][key]
    if value is not None:
        return value
    reason = row.get("missing_reasons", {}).get(key)
    if not isinstance(reason, dict):
        return "n/a（原因未提供）"
    text = "n/a（" + REASONS.get(reason.get("reason"), "原因未定") + "）"
    if reason.get("detail"):
        text += "\n" + reason["detail"]
    return text


def _records(value):
    return value if isinstance(value, list) else [value]


def _raw_record(value):
    return (
        "格式／口径问题仅标注；该条原始内容保留：\n\n~~~json\n"
        + json.dumps(value, ensure_ascii=False, indent=2)
        + "\n~~~\n\n"
    )


def _issues(items):
    result = []
    for item in _records(items):
        if not isinstance(item, dict) or not isinstance(
            item.get("reason"), (str, type(None))
        ):
            result.append(_raw_record(item))
            continue
        result.append(
            "- "
            + cell(item.get("target", ""))
            + "："
            + cell(REASONS.get(item.get("reason"), item.get("reason")))
            + "；"
            + cell(item.get("detail") or "")
        )
        if item.get("retained_value") is not None:
            result.append("  保留内容：" + cell(item["retained_value"]))
    return "\n".join(result) + "\n\n" if result else ""


def _context_text(context):
    fields = []
    labels = {
        "unit": "单位",
        "currency": "币种",
        "scope": "范围",
        "accounting_basis": "会计／监管基础",
        "period": "期间",
        "statistic": "统计口径",
        "adjustment": "调整状态",
    }
    for key in (
        "unit",
        "currency",
        "scope",
        "accounting_basis",
        "period",
        "statistic",
        "adjustment",
    ):
        if key not in context:
            continue
        value = context[key]
        if key == "period":
            value = _period(value)
        elif key == "statistic":
            value = _statistic(value)
        elif key == "scope" and isinstance(value, dict):
            value = _scope(value)
        elif key == "adjustment" and isinstance(value, dict):
            value = (
                {
                    "original": "明确原始值",
                    "restated": "明确调整／重述",
                    "not_specified": "调整状态未说明",
                }.get(value.get("status"), "调整状态未定")
                + "；"
                + str(value.get("label") or "")
            )
        fields.append(labels[key] + "=" + str(value))
    return "；".join(fields)


def _explicit_table(table):
    """Display only supplied column keys and values, without repairing the table."""
    parts = ["原始内容保留；按明确列键展示，不修改格式或口径。", ""]
    if isinstance(table, dict):
        parts.insert(0, "### " + cell(table.get("title", "原始表格")))
        columns, rows = table.get("columns"), table.get("rows")
        if isinstance(columns, dict) and isinstance(rows, list):
            keys = list(columns)
            for row in rows:
                values = row.get("values") if isinstance(row, dict) else None
                if isinstance(values, dict):
                    keys.extend(key for key in values if key not in keys)
            headers = (
                ["原科目／路径"]
                + [
                    columns[key].get("label", key)
                    if isinstance(columns.get(key), dict)
                    else "未声明列键：" + key
                    for key in keys
                ]
                + ["原始行信息"]
            )
            records = []
            for row in rows:
                values = row.get("values") if isinstance(row, dict) else None
                records.append(
                    [
                        row.get("path") if isinstance(row, dict) else None,
                        *[
                            values.get(key, "列键未提供")
                            if isinstance(values, dict)
                            else "值映射未提供"
                            for key in keys
                        ],
                        {k: v for k, v in row.items() if k not in {"path", "values"}}
                        if isinstance(row, dict) and isinstance(values, dict)
                        else row,
                    ]
                )
            parts.append(_grid(headers, records))
            parts += [
                "原始表头与上下文：",
                "",
                "~~~json",
                json.dumps(
                    {k: v for k, v in table.items() if k != "rows"},
                    ensure_ascii=False,
                    indent=2,
                ),
                "~~~",
                "",
            ]
            return "\n".join(parts) + "\n\n"
    return (
        "\n".join(parts)
        + "\n~~~json\n"
        + json.dumps(table, ensure_ascii=False, indent=2)
        + "\n~~~\n\n"
    )


def table_view(table, history=False, notes=None):
    annotations = ""
    if notes:
        annotations = (
            "格式／口径检查（仅标注，原值保留）：\n\n"
            + "\n".join("- " + cell(note) for note in notes)
            + "\n\n"
        )
    try:
        scope = table["scope"]
        parts = [
            "### " + cell(table["title"]),
            "",
            "范围："
            + cell(_scope(scope))
            + "；会计／监管基础："
            + cell(table["accounting_basis"]),
            "",
        ]
        if table.get("part"):
            parts += ["子表：" + cell(table["part"]), ""]
        columns = table["columns"]
        if not isinstance(columns, dict):
            raise TypeError("legacy_positional_table")
        if any(
            isinstance(row, dict)
            and row.get("alignment") != "independent"
            and isinstance(row.get("values"), dict)
            and set(row["values"]) - set(columns)
            for row in table["rows"]
        ):
            raise ValueError("unmapped_column_keys")
        if table["kind"] == "numeric":
            rows, independent = [], []
            for row in table["rows"]:
                if isinstance(row, dict) and row.get("alignment") == "independent":
                    independent.append(_raw_record(row))
                    continue
                dimensions = "；".join(
                    d["axis"] + "：" + d["member"] for d in row["dimensions"]
                )
                path = "／".join(row["path"])
                if row.get("definition"):
                    path += "\n定义：" + row["definition"]
                rows.append(
                    [
                        path,
                        dimensions,
                        row["unit"] if row["unit"] is not None else "单位未定",
                        row["currency"] if row["currency"] is not None else "—",
                        _statistic(row["statistic"]),
                        "；".join(
                            key
                            + "："
                            + (_period(period) if period is not None else "未知")
                            for key, period in row.get("period_overrides", {}).items()
                        )
                        or "沿用列期间",
                        *(_value(row, key, history) for key in columns),
                    ]
                )
            parts.append(
                _grid(
                    ["科目及层级", "维度", "单位", "币种", "统计口径", "实际期间例外"]
                    + [_column(column) for column in columns.values()],
                    rows,
                )
            )
            parts.extend(independent)
        elif table["kind"] == "attributes":
            parts.append(
                _grid(
                    ["字段", "类型", "默认口径"],
                    [
                        [
                            column["label"],
                            column["value_type"],
                            _context_text(
                                attribute_context(table, {"cell_context": {}}, key)
                            ),
                        ]
                        for key, column in columns.items()
                    ],
                )
            )
            rows = []
            for row in table["rows"]:
                values = []
                for key in columns:
                    value = _value(row, key, history)
                    override = row.get("cell_context", {}).get(key)
                    if override:
                        value = str(value) + "\n明确例外：" + _context_text(override)
                    values.append(value)
                rows.append(["／".join(row["path"]), *values])
            parts.append(_grid(["项目"] + [c["label"] for c in columns.values()], rows))
            if "reported_in" in table:
                parts.append("披露版本：" + cell(_version(table["reported_in"])))
        else:
            raise ValueError("unknown_table_kind")
        parts.append(_issues(table.get("issues", [])))
        for row in table["rows"]:
            parts.append(_issues(row.get("issues", [])))
        return "\n".join(parts) + "\n\n" + annotations
    except (KeyError, TypeError, ValueError, AttributeError):
        return _explicit_table(table) + annotations


def reading(data, labels):
    if not isinstance(data, dict):
        return "原始返回（非规范对象）\n\n~~~json\n" + stable(data) + "\n~~~\n"
    if data.get("_raw_only"):
        return (
            "# 未完成或不符合新合约的原始解析内容\n\n"
            "内容完整保留；没有补列、修改口径或作为完整结果整合。\n\n~~~json\n"
            + json.dumps(
                {key: value for key, value in data.items() if key != "_raw_only"},
                ensure_ascii=False,
                indent=2,
            )
            + "\n~~~\n"
        )
    company = data.get("company", {})
    if not isinstance(company, dict):
        company = {}
    parts = [
        "# "
        + cell(company.get("name") or "原始记录／公司未定")
        + " "
        + cell(data.get("report_period", "")),
        "",
        "结构化提取／整理结果；未做财务数值复核。",
        "",
    ]
    if data.get("reports"):
        parts += ["披露版本：" + "；".join(_version(r) for r in data["reports"]), ""]
    if data.get("_format_notes"):
        parts += [
            "格式／口径检查有意见，数据继续展示；完整记录见 format_notes.json。",
            "",
        ]
    tables = data.get("tables", [])
    tables = tables if isinstance(tables, list) else []
    for index, table in enumerate(tables):
        notes = list(data.get("_table_notes", {}).get(str(index), []))
        notes += [
            item.get("detail") or "检查说明字段未提供，原记录保留"
            for item in _records(data.get("issues") or [])
            if isinstance(item, dict) and item.get("target") == f"tables/{index}"
        ]
        parts.append(
            table_view(table, history=data.get("stage") == "history", notes=notes)
        )
    policies = data.get("shared_policies", data.get("policies", []))
    if policies:
        parts += ["## 政策与口径", ""]
    for policy in _records(policies):
        try:
            parts += [
                "**"
                + cell(labels.get(policy["module"], policy["module"]))
                + "／"
                + cell(labels.get(policy["topic"], policy["topic"]))
                + "**",
                "",
                "范围："
                + cell(_scope(policy["scope"]))
                + "；会计／监管基础："
                + cell(policy["accounting_basis"]),
                "",
                cell(policy["text"]),
                "",
            ]
            if "appears_in" in policy:
                parts += [
                    "出现于报告："
                    + "；".join(_version(r) for r in policy["appears_in"]),
                    "",
                ]
            elif "reported_in" in policy:
                parts += ["披露版本：" + cell(_version(policy["reported_in"])), ""]
            parts.append(_issues(policy.get("issues", [])))
        except (KeyError, TypeError, ValueError, AttributeError):
            parts.append(_raw_record(policy))
    if data.get("changes"):
        parts += ["## 已披露变化", ""]
        for change in _records(data["changes"]):
            try:
                parts += [
                    cell(labels.get(change["topic"], change["topic"]))
                    + "："
                    + cell(change["text"]),
                    "",
                ]
                parts += ["范围：" + cell(_scope(change["scope"])), ""]
                if "reported_in" in change:
                    parts += ["披露版本：" + cell(_version(change["reported_in"])), ""]
            except (KeyError, TypeError, ValueError, AttributeError):
                parts.append(_raw_record(change))
    for event_item in _records(data.get("events", [])):
        try:
            parts += [
                "## 股份与分派事项",
                "",
                cell(
                    {
                        "share_issue": "发行股份",
                        "share_transfer": "股份转增",
                        "split": "拆股",
                        "reverse_split": "合股",
                        "repurchase": "回购",
                        "cancellation": "注销",
                        "dividend": "利润分派",
                        "other_equity_distribution": "其他权益分派",
                        "other_equity_change": "其他权益变化",
                    }.get(event_item["event_type"], event_item["event_type"])
                )
                + "／"
                + cell(
                    {
                        "proposal": "方案",
                        "approved": "通过",
                        "declared": "宣告",
                        "implemented": "实施",
                        "paid": "实付",
                        "unknown": "状态未定",
                    }[event_item["phase"]]
                ),
                "",
                "日期："
                + cell(event_item["date"]["value"])
                + "（"
                + cell(event_item["date"]["precision"])
                + "精度）",
                "",
                cell(event_item["description"]),
                "",
                "范围：" + cell(_scope(event_item["scope"])),
                "",
            ]
            if "reported_in" in event_item:
                parts += ["披露版本：" + cell(_version(event_item["reported_in"])), ""]
            parts.append(_issues(event_item["issues"]))
            if event_item["terms"] is not None:
                parts.append(table_view(event_item["terms"]))
        except (KeyError, TypeError, ValueError, AttributeError):
            parts.append(_raw_record(event_item))
    if data.get("metadata"):
        parts += ["## 公司元数据：逐期保留", ""]
        for snapshot in _records(data["metadata"]):
            try:
                parts += [
                    "### " + cell(_period(snapshot["period"])),
                    "",
                    _grid(
                        ["字段", "披露内容"],
                        [
                            [
                                labels.get(key, key),
                                _period(value)
                                if key == "period"
                                else _scope(value)
                                if key == "scope"
                                else "；".join(value)
                                if isinstance(value, list)
                                and all(isinstance(v, str) for v in value)
                                else value,
                            ]
                            for key, value in snapshot.items()
                            if key not in {"issues", "reported_in"}
                        ],
                    ),
                ]
                if "reported_in" in snapshot:
                    parts += [
                        "披露版本：" + cell(_version(snapshot["reported_in"])),
                        "",
                    ]
                parts.append(_issues(snapshot["issues"]))
            except (KeyError, TypeError, ValueError, AttributeError):
                parts.append(_raw_record(snapshot))
    if data.get("management"):
        parts += ["## 管理层披露", ""]
        for item in _records(data["management"]):
            try:
                parts += [
                    cell(labels.get(item["topic"], item["topic"]))
                    + "（"
                    + cell(
                        {
                            "fact": "披露事实",
                            "management_explanation": "管理层解释",
                            "management_plan": "管理层计划",
                        }.get(item["kind"], item["kind"])
                    )
                    + "）："
                    + cell(item["text"]),
                    "",
                    "期间："
                    + cell(_period(item["period"]))
                    + "；范围："
                    + cell(_scope(item["scope"])),
                    "",
                ]
                if "reported_in" in item:
                    parts += ["披露版本：" + cell(_version(item["reported_in"])), ""]
                parts.append(_issues(item["issues"]))
            except (KeyError, TypeError, ValueError, AttributeError):
                parts.append(_raw_record(item))
    if data.get("peer_pairs"):
        parts += ["## 同行候选与比较范围", ""]
        parts.append(
            _grid(
                ["对象", "判断", "范围", "理由", "需考虑", "缺口"],
                [
                    [
                        "／".join(pair["companies"]),
                        {
                            "candidate": "同行候选",
                            "different": "不宜直接比较",
                            "undetermined": "未定",
                        }[pair["relation"]],
                        pair["scope"],
                        pair["reason"],
                        "；".join(pair["consider"]),
                        "；".join(pair["missing"]),
                    ]
                    for pair in data["peer_pairs"]
                ],
            )
        )
    if data.get("groups"):
        parts += [
            "## 同行候选组",
            "",
            _grid(
                ["组", "成员", "范围"],
                [
                    [g["name"], "／".join(g["companies"]), g["scope"]]
                    for g in data["groups"]
                ],
            ),
        ]
    for key, value in data.get("research_scope", {}).items():
        parts += ["**" + cell(labels.get(key, key)) + "**：" + cell(value), ""]
    if data.get("conflicts"):
        parts += [
            "## 同版本结构冲突：全部保留",
            "",
            "~~~json",
            json.dumps(data["conflicts"], ensure_ascii=False, indent=2),
            "~~~",
            "",
        ]
    if data.get("issues"):
        parts += ["## 未定事项", "", _issues(data["issues"])]
    if data.get("context_index"):
        parts += [
            "## 上下文目录（完整明细保存在主数据）",
            "",
            _grid(
                ["主题", "表", "行数", "列数"],
                [
                    [
                        labels.get(i["topic"], i["topic"]),
                        i["title"],
                        i["rows"],
                        i["columns"],
                    ]
                    for i in data["context_index"]
                ],
            ),
        ]
    raw = data.get("unstructured_blocks", {})
    if raw:
        parts += [
            "## 未按新合约整合的原始内容",
            "",
            "以下内容完整保留，不按位置补列、不迁移、不猜单位及期间。",
            "",
            "~~~json",
            json.dumps(raw, ensure_ascii=False, indent=2),
            "~~~",
            "",
        ]
    # Per-block pages and unexpected roots remain inspectable without adapters.
    if not data.get("reports") and not data.get("tables"):
        parts += [
            "## 原始解析内容",
            "",
            "~~~json",
            json.dumps(data, ensure_ascii=False, indent=2),
            "~~~",
            "",
        ]
    return "\n".join(parts)


def publish(output, data, labels, context=None):
    save(output / "data.json", data)
    event(output, "data_saved")
    view = {**(context or {}), **data} if isinstance(data, dict) else data
    text = reading(view, labels)
    save(output / "阅读.md", text.encode("utf8"))
    css = "body{font:15px/1.65 'Microsoft YaHei',sans-serif;max-width:1500px;margin:28px auto;padding:0 24px;color:#233041}.table-scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;margin:18px 0}th,td{border:1px solid #dce3eb;padding:8px 10px;vertical-align:top}th{background:#edf2f8;min-width:110px}td:first-child{min-width:180px}tr:nth-child(even){background:#fafbfd}pre{white-space:pre-wrap;overflow-wrap:anywhere}h2{margin-top:34px}"
    rendered = bleach.clean(
        markdown.markdown(text, extensions=["tables", "fenced_code"]),
        tags=[
            "h1",
            "h2",
            "h3",
            "p",
            "strong",
            "em",
            "ul",
            "ol",
            "li",
            "table",
            "thead",
            "tbody",
            "tr",
            "th",
            "td",
            "br",
            "pre",
            "code",
        ],
        attributes={},
        strip=True,
    )
    rendered = rendered.replace("<table>", '<div class="table-scroll"><table>').replace(
        "</table>", "</table></div>"
    )
    save(
        output / "阅读.html",
        (
            '<!doctype html><meta charset="utf-8"><meta name="viewport" '
            'content="width=device-width"><style>' + css + "</style>" + rendered
        ).encode("utf8"),
    )
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(
        [
            "role",
            "topic",
            "title",
            "path",
            "dimensions",
            "unit",
            "currency",
            "scope",
            "accounting_basis",
            "statistic",
            "period",
            "adjustment",
            "comparison",
            "reported_in",
            "value",
            "missing_reason",
        ]
    )
    if isinstance(data, dict) and not (context or {}).get("_raw_only"):
        tables = data.get("tables", [])
        for table in tables if isinstance(tables, list) else []:
            if not isinstance(table, dict) or table.get("kind") != "numeric":
                continue
            columns, rows = table.get("columns"), table.get("rows")
            if not isinstance(rows, list):
                continue
            for row in rows:
                if (
                    not isinstance(row, dict)
                    or row.get("alignment") == "independent"
                    or not isinstance(row.get("values"), dict)
                ):
                    continue
                for key, value in row["values"].items():
                    column = columns.get(key, {}) if isinstance(columns, dict) else {}
                    column = column if isinstance(column, dict) else {}
                    path = row.get("path")
                    missing = row.get("missing_reasons")
                    writer.writerow(
                        [
                            table.get("role"),
                            table.get("topic"),
                            table.get("title"),
                            "／".join(map(str, path))
                            if isinstance(path, list)
                            else path,
                            stable(row.get("dimensions")),
                            row.get("unit"),
                            row.get("currency"),
                            stable(table.get("scope")),
                            table.get("accounting_basis"),
                            stable(row.get("statistic")),
                            stable(
                                row["period_overrides"][key]
                                if isinstance(row.get("period_overrides"), dict)
                                and key in row["period_overrides"]
                                else column.get("period")
                            )
                            if isinstance(columns, dict) and key in columns
                            else stable({"unmapped_column_key": key}),
                            stable(column.get("adjustment")),
                            stable(column.get("comparison")),
                            stable(column.get("reported_in")),
                            value if value is not None else "n/a",
                            stable(
                                missing.get(key)
                                if isinstance(missing, dict)
                                else missing
                            ),
                        ]
                    )
        save(output / "context_index.json", context_index(data))
    else:
        save(output / "context_index.json", [])
    save(output / "financial_values.csv", stream.getvalue().encode("utf-8-sig"))
    event(
        output,
        "views_saved",
        files=["阅读.md", "阅读.html", "financial_values.csv", "context_index.json"],
    )
