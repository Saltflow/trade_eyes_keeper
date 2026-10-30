"""Offline model reading material; no imports from the frozen extraction chain."""

import json
from collections import Counter, defaultdict
from copy import deepcopy

WORDS = {
    "unit": "单位",
    "currency": "币种",
    "scope": "范围",
    "accounting_basis": "会计基础",
    "period": "期间",
    "statistic": "统计口径",
    "point": "时点",
    "period_total": "期间总额",
    "period_average": "期间平均",
    "period_measure": "期间指标",
    "rate": "比率",
    "unknown": "未定",
    "group": "集团",
    "parent": "母公司",
    "regulatory": "监管并表",
    "kind": "类型",
    "entity": "主体",
    "start": "开始",
    "end": "结束",
    "coverage": "覆盖",
    "full_year": "全年",
    "instant": "时点",
    "duration": "期间",
    "method": "方法",
    "state": "状态",
    "text": "正文",
    "annualized": "年化",
    "yes": "是",
    "no": "否",
    "not_specified": "未说明",
    "not_applicable": "不适用",
    "not_disclosed": "未披露",
    "not_found": "未找到",
    "context_uncertain": "口径未定",
    "reason": "原因",
    "detail": "说明",
    "original": "原始值",
    "restated": "重述值",
    "status": "状态",
    "adjustment": "调整",
    "comparison": "比较",
    "base_period": "基期",
    "definition": "定义",
    "label": "标签",
    "reported_in": "披露版本",
    "issues": "未定事项",
    "module": "模块",
    "topic": "主题",
    "VALUE": "数值",
    "YOY": "同比",
    "QOQ": "环比",
    "company": "公司",
}


def key(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def describe(value, labels=None):
    """Readable, complete fallback for unexpected fields, never a JSON dump."""
    labels = {**WORDS, **(labels or {})}
    if value is None:
        return "未定"
    if isinstance(value, dict):
        return (
            "；".join(
                str(labels.get(k, k)) + "：" + describe(v, labels)
                for k, v in value.items()
            )
            or "无"
        )
    if isinstance(value, list):
        return " / ".join(describe(v, labels) for v in value) or "无"
    return str(labels.get(value, value)) if isinstance(value, str) else str(value)


def period_text(period):
    if not isinstance(period, dict):
        return "期间未定"
    if period.get("kind") == "instant":
        return str(period.get("end") or "未定") + "时点"
    if period.get("kind") == "duration":
        return (
            str(period.get("start") or "未定") + "至" + str(period.get("end") or "未定")
        )
    return describe(period)


def effective_column(table, row, name):
    columns = table.get("columns", {})
    column = deepcopy(columns.get(name, {})) if isinstance(columns, dict) else {}
    overrides = row.get("period_overrides", {})
    if isinstance(overrides, dict) and name in overrides:
        column["period"] = deepcopy(overrides[name])
    return column


def column_identity(column):
    # A declared role and period can replace a calendar-only display label.
    # Duplicate semantic columns are rejected for alignment, never disambiguated by row.
    return key({k: v for k, v in column.items() if k != "label"})


def known(value):
    return isinstance(value, str) and value not in {"", "unknown"}


def row_identity(row):
    stat = row.get("statistic")
    if not known(row.get("identity_code")) or not known(row.get("unit_code")):
        return None
    if not isinstance(stat, dict) or not known(stat.get("kind")):
        return None
    if (
        stat["kind"] in {"period_average", "average"}
        and stat.get("method", {}).get("state") != "stated"
    ):
        return None
    if stat["kind"] in {"rate", "period_measure"} and stat.get("annualized") in {
        None,
        "not_specified",
    }:
        return None
    fields = {
        k: v
        for k, v in row.items()
        if k
        not in {
            "path",
            "values",
            "missing_reasons",
            "period_overrides",
            "issues",
        }
    }
    if isinstance(fields.get("dimensions"), list):
        fields["dimensions"] = sorted(fields["dimensions"], key=key)
    return key(fields)


def alignment_reason(record, table):
    if record.get("schema_version") != "4.0":
        return "非4.0契约，原样独立保留"
    company = record.get("company") or {}
    if not (known(company.get("code")) or known(company.get("name"))):
        return "原记录公司身份未定，不参与跨年匹配"
    if not isinstance(table, dict) or table.get("kind") != "numeric":
        return "原表独立保留"
    scope = table.get("scope") or {}
    if not all(
        known(v)
        for v in [
            table.get("identity_code"),
            scope.get("kind"),
            scope.get("entity_code"),
            table.get("accounting_basis_code"),
        ]
    ):
        return "表身份或范围未定，原表独立保留"
    if not isinstance(table.get("columns"), dict) or not isinstance(
        table.get("rows"), list
    ):
        return "表结构未定，内容独立保留"
    for row in table["rows"]:
        if not isinstance(row, dict) or not isinstance(row.get("values"), dict):
            return "行结构未定，原表独立保留"
        semantics = []
        for name in row["values"]:
            col = effective_column(table, row, name)
            p = col.get("period") or {}
            version = col.get("reported_in") or {}
            if not (
                known(col.get("identity_code"))
                and known(version.get("report_id"))
                and p.get("kind") in {"instant", "duration"}
                and known(p.get("end"))
                and (p["kind"] == "instant" or known(p.get("start")))
            ):
                return "列身份或实际期间未定，原表独立保留"
            if col["identity_code"] in {"YOY", "QOQ", "OTHER_COMPARISON"}:
                base = (col.get("comparison") or {}).get("base_period") or {}
                if not known(base.get("end")) or (
                    base.get("kind") == "duration" and not known(base.get("start"))
                ):
                    return "比较基期未定，原表独立保留"
            semantics.append(column_identity(col))
        if len(set(semantics)) != len(semantics):
            return "同期间多个列角色无法区分，保留原列，不逐行造列"
    return None


def table_identity(table):
    fields = {
        k: v
        for k, v in table.items()
        if k
        not in {
            "rows",
            "columns",
            "issues",
            "reported_in",
        }
    }
    # Fixed declared contract codes, not inferred synonyms. Regulatory definitions
    # must still agree verbatim. Original display names remain in the model text.
    if table.get("accounting_basis_code") in {"CAS", "IFRS", "HKFRS", "US_GAAP"}:
        fields.pop("accounting_basis", None)
    return key(fields)


def assemble(entries):
    """Build sections and source-cell mappings; no repair, coercion or checks gate values."""
    groups, sections = {}, []
    for source, record in entries:
        for ti, table in enumerate(record.get("tables", [])):
            reason = alignment_reason(record, table)
            group = None if reason else table_identity(table)
            if group is None or group not in groups:
                section = {
                    "table": deepcopy(table),
                    "inputs": [],
                    "rows": [],
                    "columns": {},
                    "aligned": reason is None,
                    "reason": reason,
                }
                sections.append(section)
                if group is not None:
                    groups[group] = section
            else:
                section = groups[group]
            section["inputs"].append((source, ti, table))
    mappings = []
    for si, section in enumerate(sections):
        occurrences = Counter(
            (source, row_identity(row))
            for source, _, table in section["inputs"]
            for row in (table.get("rows", []) if isinstance(table, dict) else [])
            if isinstance(row, dict)
        )
        rows = {}
        for source, ti, table in section["inputs"]:
            source_rows = table.get("rows", []) if isinstance(table, dict) else []
            if not isinstance(source_rows, list):
                continue
            for ri, original in enumerate(source_rows):
                if not isinstance(original, dict) or not isinstance(
                    original.get("values"), dict
                ):
                    section["rows"].append({"raw": deepcopy(original)})
                    continue
                identity = row_identity(original) if section["aligned"] else None
                if identity is None or occurrences[(source, identity)] > 1:
                    identity = key([source, ti, ri])
                if identity not in rows:
                    row = {
                        "fields": {
                            k: deepcopy(v)
                            for k, v in original.items()
                            if k
                            not in {
                                "values",
                                "missing_reasons",
                                "period_overrides",
                                "path",
                                "issues",
                            }
                        },
                        "names": [],
                        "issues": [],
                        "cells": {},
                        "columns": {},
                    }
                    if section["aligned"] and (
                        row_identity(original) is None
                        or occurrences[(source, row_identity(original))] > 1
                    ):
                        row["alignment_note"] = source + "；行身份未定或重复，独立保留"
                    rows[identity] = row
                    section["rows"].append(row)
                row = rows[identity]
                name = original.get("path", [])
                if name not in row["names"]:
                    row["names"].append(deepcopy(name))
                for issue in original.get("issues", []):
                    if issue not in row["issues"]:
                        row["issues"].append(deepcopy(issue))
                for name, value in original["values"].items():
                    column = effective_column(table, original, name)
                    semantic = column_identity(column) if section["aligned"] else name
                    if semantic not in section["columns"]:
                        section["columns"][semantic] = deepcopy(column)
                    row["columns"][semantic] = column
                    variants = row["cells"].setdefault(semantic, [])
                    missing = original.get("missing_reasons", {})
                    observation = {
                        "value": deepcopy(value),
                        "missing": deepcopy(missing.get(name))
                        if isinstance(missing, dict)
                        else deepcopy(missing),
                    }
                    variant = next(
                        (v for v in variants if v["observation"] == observation), None
                    )
                    if variant is None:
                        variant = {"observation": observation, "origins": []}
                        variants.append(variant)
                    origin = [source, ti, ri, name]
                    variant["origins"].append(origin)
                    mappings.append(
                        {
                            "source": origin,
                            "target": [
                                si,
                                section["rows"].index(row),
                                semantic,
                                variants.index(variant),
                            ],
                        }
                    )
    return {"sections": sections, "mappings": mappings}


def value_text(observation, labels=None):
    value = observation["value"]
    if value is not None:
        text = value if isinstance(value, str) else describe(value, labels)
        if observation.get("missing") is not None:
            text += "〔原缺失说明：" + describe(observation["missing"], labels) + "〕"
        return text
    reason = observation.get("missing")
    if isinstance(reason, dict):
        text = describe(reason.get("reason"), labels)
        if reason.get("detail") is not None:
            text += "：" + str(reason["detail"])
        extra = {k: v for k, v in reason.items() if k not in {"reason", "detail"}}
        if extra:
            text += "；" + describe(extra, labels)
    else:
        text = describe(reason, labels) if reason is not None else "原因未提供"
    return "n/a〔" + text + "〕"


def escape(value):
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r", "")
        .replace("\n", "<br>")
    )


def grid(headers, rows):
    return (
        "\n".join(
            [
                "| " + " | ".join(map(escape, headers)) + " |",
                "|" + "---|" * len(headers),
                *["| " + " | ".join(map(escape, row)) + " |" for row in rows],
            ]
        )
        + "\n\n"
    )


class SharedText:
    """Two-pass literal reuse: definitions stay inside this same model document."""

    def __init__(self):
        self.items, self.ready = {}, False

    def ref(self, kind, value, text):
        identity = (kind, key(value))
        if not self.ready:
            item = self.items.setdefault(
                identity, {"count": 0, "text": text, "kind": kind}
            )
            item["count"] += 1
        item = self.items[identity]
        return "〔" + item["id"] + "〕" if item.get("id") else text

    def seal(self):
        counts = Counter()
        groups = defaultdict(list)
        for item in self.items.values():
            groups[(item["kind"], item["text"])].append(item)
        for (kind, text), items in groups.items():
            count = sum(i["count"] for i in items)
            label = kind + str(counts[kind] + 1)
            if count > 1 and (
                kind == "M" or (count - 1) * len(text) > (count + 1) * (len(label) + 2)
            ):
                counts[kind] += 1
                for item in items:
                    item["id"] = label
        self.ready = True

    def definitions(self):
        names = {
            "C": "共同口径",
            "M": "缺失及原缺失说明",
            "N": "重复说明",
            "G": "原科目路径前缀",
        }
        lines = [
            "## 共用定义",
            "",
            "下列引用只复用完全相同的原内容；每份公司正文均包含自己的完整定义。",
            "",
        ]
        for kind, title in names.items():
            values = list(
                {
                    i["id"]: i
                    for i in self.items.values()
                    if i.get("id") and i["kind"] == kind
                }.values()
            )
            if values:
                lines += ["### " + title, ""]
                lines += [i["id"] + "：" + i["text"] + "\n" for i in values]
        return "\n".join(lines) + "\n"


def shared_context(value, labels, shared):
    return shared.ref("C", value, context_text(value, labels)) if value else ""


def compact_value(observation, labels, shared):
    if observation["value"] is None:
        return "n/a" + shared.ref(
            "M", observation.get("missing"), value_text(observation, labels)[3:]
        )
    text = (
        observation["value"]
        if isinstance(observation["value"], str)
        else describe(observation["value"], labels)
    )
    if observation.get("missing") is not None:
        text += "〔原缺失说明〕" + shared.ref(
            "M", observation["missing"], describe(observation["missing"], labels)
        )
    return text


def context_text(fields, labels):
    result = []
    for name, value in fields.items():
        text = None
        if name == "scope" and isinstance(value, dict):
            text = (
                describe(value.get("kind"), labels)
                + "／"
                + str(value.get("entity") or "主体未定")
            )
            extra = {
                k: v
                for k, v in value.items()
                if k not in {"kind", "entity", "entity_code"}
            }
            if extra:
                text += "；" + describe(extra, labels)
        elif name == "statistic" and isinstance(value, dict):
            text = describe(value.get("kind"), labels)
            method = value.get("method")
            if isinstance(method, dict) and method.get("state") != "not_applicable":
                text += "；方法=" + describe(method, labels)
            annual = value.get("annualized")
            if annual is not None and annual != "not_applicable":
                text += "；年化=" + describe(annual, labels)
            extra = {
                k: v
                for k, v in value.items()
                if k not in {"kind", "method", "annualized"}
            }
            if extra:
                text += "；" + describe(extra, labels)
        elif name == "period":
            text = period_text(value)
        result.append(
            describe(name, labels)
            + "："
            + (text if text is not None else describe(value, labels))
        )
    return "；".join(result)


def column_text(column, labels, original=False):
    if original:
        result = str(column.get("label") or "原列名未提供")
    else:
        result = period_text(column.get("period"))
        if column.get("identity_code") != "VALUE":
            result += "／" + describe(column.get("identity_code"), labels)
    version = column.get("reported_in")
    if isinstance(version, dict):
        result += "〔" + str(version.get("report_period") or "披露期间未定") + "披露〕"
    extras = {
        k: v
        for k, v in column.items()
        if k
        not in {
            "label",
            "identity_code",
            "reported_in",
            "period",
            "context",
            "value_type",
            "adjustment",
        }
        and v is not None
    }
    if original and "period" in column:
        result += "；" + period_text(column["period"])
    adjustment = column.get("adjustment")
    if isinstance(adjustment, dict):
        result += "；" + describe(adjustment.get("status"), labels)
        if adjustment.get("label") is not None:
            result += "：" + str(adjustment["label"])
        extra = {k: v for k, v in adjustment.items() if k not in {"status", "label"}}
        if extra:
            result += "；" + describe(extra, labels)
    if extras:
        result += "；" + describe(extras, labels)
    return result


def row_title(row, labels, shared, versions):
    names = row["names"]
    if (
        names
        and all(isinstance(n, list) and n for n in names)
        and len({str(n[-1]) for n in names}) == 1
    ):
        prefixes = ["／".join(map(str, n[:-1])) for n in names]
        prefix = (
            shared.ref("G", prefixes, " / ".join(p or "（无前缀）" for p in prefixes))
            if any(prefixes)
            else ""
        )
        title = (prefix + "／" if prefix else "") + str(names[0][-1])
    else:
        title = "；原名亦为：".join(
            "／".join(map(str, n)) if isinstance(n, list) else str(n) for n in names
        )
    if row.get("alignment_note"):
        source = row["alignment_note"].split("；", 1)[0]
        title += "〔独立：" + versions[source] + "〕"
    fields = row["fields"]
    if fields.get("dimensions"):
        title += "〔" + describe(fields["dimensions"], labels) + "〕"
    if fields.get("definition"):
        title += "；定义：" + str(fields["definition"])
    extra = {
        k: v
        for k, v in fields.items()
        if k
        not in {
            "row_kind",
            "identity_code",
            "unit_code",
            "unit",
            "currency",
            "statistic",
            "dimensions",
            "definition",
            "cell_context",
        }
    }
    if extra:
        title += "；" + describe(extra, labels)
    return title


def display_columns(section, columns):
    grouped = {}
    for name, column in columns:
        fields = {k: v for k, v in column.items() if k not in {"label", "reported_in"}}
        adjustment = column.get("adjustment") or {}
        fold = section["aligned"] and adjustment.get("status") in {
            "original",
            "restated",
        }
        identity = key(fields) if fold else name
        grouped.setdefault(identity, []).append((name, column))
    result = list(grouped.values())
    if section["aligned"]:
        result.sort(
            key=lambda g: (
                str((g[0][1].get("period") or {}).get("end") or ""),
                str((g[0][1].get("period") or {}).get("start") or ""),
                str(g[0][1].get("identity_code") or ""),
            )
        )
    return result


def render_section(section, labels, shared, versions, section_index):
    table = section["table"]
    if not isinstance(table, dict):
        return "### 未定表结构\n\n" + describe(table, labels) + "\n\n", []
    sources = list(dict.fromkeys(s for s, _, _ in section["inputs"]))
    reason = (
        ("明确身份跨年对齐" if len(sources) > 1 else "未匹配到其他年度同表，独立保留")
        if section["aligned"]
        else str(section["reason"])
    )
    parts = [
        "### "
        + str(table.get("title") or "未命名表")
        + ("／" + str(table["part"]) if table.get("part") else ""),
        "",
        "输入：" + "、".join(sources),
        "整理：" + shared.ref("N", reason, reason),
        "",
    ]
    if not isinstance(table.get("rows"), list) or not isinstance(
        table.get("columns"), dict
    ):
        return "\n".join(parts) + "\n" + describe(table, labels) + "\n\n", []
    base = {k: table[k] for k in ("scope", "accounting_basis") if k in table}
    if isinstance(table.get("context"), dict):
        base.update(table["context"])
    normal = [r for r in section["rows"] if "raw" not in r and r["cells"]]
    common = {
        name: normal[0]["fields"][name]
        for name in ("unit", "currency", "statistic")
        if normal
        and name in normal[0]["fields"]
        and all(r["fields"].get(name) == normal[0]["fields"][name] for r in normal)
    }
    base.update(common)
    if base:
        parts += ["口径：" + shared_context(base, labels, shared), ""]
    basis_names = {s: t.get("accounting_basis") for s, _, t in section["inputs"]}
    if len({key(v) for v in basis_names.values()}) > 1:
        parts += [
            "准则原表述："
            + "；".join(s + "=" + str(v) for s, v in basis_names.items()),
            "",
        ]
    for source, _, original in section["inputs"]:
        used_names = {
            name
            for row in original.get("rows", [])
            if isinstance(row, dict)
            for name in (row.get("values") or {})
        }
        for name, column in original.get("columns", {}).items():
            if name not in used_names:
                parts += [
                    "未使用原列「"
                    + source
                    + "／"
                    + name
                    + "」："
                    + describe(column, labels),
                    "",
                ]
        for index, row in enumerate(original.get("rows", [])):
            if not isinstance(row, dict):
                continue
            for field in ("missing_reasons", "period_overrides", "cell_context"):
                if isinstance(row.get(field), dict):
                    orphan = {
                        k: v
                        for k, v in row[field].items()
                        if k not in (row.get("values") or {})
                    }
                    if orphan:
                        parts += [
                            source
                            + "／行"
                            + str(index + 1)
                            + "未对应数值的原字段："
                            + field
                            + "="
                            + describe(orphan, labels),
                            "",
                        ]
    blocks, cell_records = {}, []
    for ri, row in enumerate(section["rows"]):
        if "raw" in row:
            parts += [describe(row["raw"], labels), ""]
            continue
        title = row_title(row, labels, shared, versions)
        fields = row["fields"]
        if row["issues"]:
            parts += [
                "项目说明「"
                + title
                + "」："
                + shared.ref("N", row["issues"], describe(row["issues"], labels)),
                "",
            ]
        if not row["cells"]:
            local = {
                k: fields[k]
                for k in ("unit", "currency", "statistic")
                if k in fields and fields[k] != base.get(k)
            }
            parts += [
                "原表分组："
                + title
                + ("；口径：" + shared_context(local, labels, shared) if local else ""),
                "",
            ]
            continue
        columns = [
            (k, row["columns"][k]) for k in section["columns"] if k in row["cells"]
        ]
        local = {
            k: fields[k]
            for k in ("unit", "currency", "statistic")
            if k in fields and k not in common
        }
        block_key = key([local, columns])
        block = blocks.setdefault(
            block_key,
            {
                "local": local,
                "groups": display_columns(section, columns),
                "rows": [],
                "records": [],
            },
        )
        values, row_records = [], []
        for group in block["groups"]:
            observations = {}
            for name, _ in group:
                for variant in row["cells"][name]:
                    for origin in variant["origins"]:
                        contexts = fields.get("cell_context") or {}
                        exception = (
                            {"context": contexts[origin[3]]}
                            if origin[3] in contexts
                            else {}
                        )
                        identity = key([variant["observation"], exception])
                        item = observations.setdefault(
                            identity,
                            {
                                "observation": variant["observation"],
                                "origins": [],
                                "exception": exception,
                            },
                        )
                        item["origins"].append(origin)
            texts = []
            for item in observations.values():
                value = compact_value(item["observation"], labels, shared)
                if item["exception"]:
                    value += (
                        "〔例外："
                        + shared_context(item["exception"], labels, shared)
                        + "〕"
                    )
                if len(observations) > 1:
                    value = (
                        "/".join(dict.fromkeys(versions[o[0]] for o in item["origins"]))
                        + ":"
                        + value
                    )
                texts.append(value)
            value = " / ".join(texts)
            values.append(value)
            row_records.append(
                [
                    {
                        "origins": item["origins"],
                        "observation": item["observation"],
                        "display": value,
                        "target_row": [section_index, ri],
                    }
                    for item in observations.values()
                ]
            )
        block["rows"].append([title, *values])
        block["records"].append(row_records)
    for block in blocks.values():
        if block["local"]:
            parts += ["本组口径：" + shared_context(block["local"], labels, shared), ""]
        headers = ["项目"]
        for group in block["groups"]:
            column = group[0][1]
            overrides = {
                k: v
                for k, v in (column.get("context") or {}).items()
                if k not in base or base[k] != v
            }
            if overrides:
                parts += [
                    "字段口径「"
                    + str(column.get("label", group[0][0]))
                    + "」："
                    + shared_context(overrides, labels, shared),
                    "",
                ]
            if len(group) == 1:
                header = column_text(column, labels, not section["aligned"])
            else:
                header = column_text(
                    {k: v for k, v in column.items() if k != "reported_in"}, labels
                )
                origins = {
                    o[0]
                    for row in normal
                    for name, _ in group
                    for v in row["cells"].get(name, [])
                    for o in v["origins"]
                }
                header += (
                    "〔"
                    + "/".join(versions[s] for s in versions if s in origins)
                    + "〕"
                )
            headers.append(header)
        text = grid(headers, block["rows"])
        start = sum(len(p) + 1 for p in parts)
        header_display = text.splitlines()[0]
        row_offset = len(header_display) + 1 + len(text.splitlines()[1]) + 1
        for values, groups in zip(block["rows"], block["records"]):
            escaped = list(map(escape, values))
            line = "| " + " | ".join(escaped) + " |"
            for ci, records in enumerate(groups, 1):
                cell_start = (
                    start + row_offset + 2 + sum(len(v) + 3 for v in escaped[:ci])
                )
                for record in records:
                    record.update(
                        span=[cell_start, cell_start + len(escaped[ci])],
                        row_span=[start + row_offset, start + row_offset + len(line)],
                        header_span=[start, start + len(header_display)],
                        row_display=line,
                        header_display=header_display,
                        display_column=ci,
                    )
                    cell_records.append(record)
            row_offset += len(line) + 1
        parts.append(text)
    notes = []
    for _, _, source_table in section["inputs"]:
        for note in source_table.get("issues", []):
            if note not in notes:
                notes.append(note)
    if notes:
        parts += ["表级说明：" + shared.ref("N", notes, describe(notes, labels)), ""]
    known_fields = {
        "kind",
        "role",
        "module",
        "topic",
        "title",
        "part",
        "scope",
        "accounting_basis",
        "accounting_basis_code",
        "identity_code",
        "context",
        "columns",
        "rows",
        "issues",
        "reported_in",
    }
    extras = {k: v for k, v in table.items() if k not in known_fields}
    if extras:
        parts += ["其他原字段：" + describe(extras, labels), ""]
    return "\n".join(parts) + "\n", cell_records


def reading(entries, plan, labels=None):
    shared = SharedText()
    _reading(entries, plan, labels or {}, shared)
    shared.seal()
    plan["definitions"] = list(
        {
            v["id"]: {"id": v["id"], "text": v["text"]}
            for v in shared.items.values()
            if v.get("id")
        }.values()
    )
    return _reading(entries, plan, labels or {}, shared)


def _reading(entries, plan, labels, shared):
    labels = labels or {}
    parts = [
        "# 多年财报模型输入正文",
        "",
        "仅整理已有提取：不补值、不换算、不修复；不代表财务数值复核。",
        "同期间、角色、调整和口径完全相同的披露列共显；不同值逐版本并存，不替选版本。",
        "n/a引用M定义保留各原缺失原因；独立标记表示行身份未定或重复，独立保留，不按出现次序配对。",
        "G定义为原科目路径前缀，引用后的末级科目分别接每个前缀；无前缀表示原路径仅含该科目。",
        "无法明确对齐的表按输入年度独立保留，不将其解释为未披露。",
        "",
    ]
    cells = []
    versions = {source: "V" + str(i + 1) for i, (source, _) in enumerate(entries)}
    for source, record in entries:
        report_labels = list(
            dict.fromkeys(
                str(c.get("reported_in", {}).get("report_period"))
                for t in record.get("tables", [])
                if isinstance(t, dict)
                for c in t.get("columns", {}).values()
                if isinstance(c, dict) and isinstance(c.get("reported_in"), dict)
            )
        )
        parts += [
            versions[source]
            + "="
            + source
            + "；原披露标记="
            + "/".join(report_labels)
            + "；原记录公司="
            + describe(record.get("company"), labels),
            "",
        ]
    if shared.ready:
        parts += [shared.definitions(), ""]
    for si, section in enumerate(plan["sections"]):
        text, retained = render_section(section, labels, shared, versions, si)
        offset = sum(len(p) + 1 for p in parts)
        for cell in retained:
            for name in ("span", "row_span", "header_span"):
                cell[name] = [v + offset for v in cell[name]]
        parts.append(text)
        cells.extend(retained)
    stats = {}
    for collection, title in [
        ("policies", "政策与口径版本"),
        ("changes", "披露变化"),
        ("events", "股份与分派事项"),
        ("metadata", "逐期公司元数据"),
        ("management", "管理层披露"),
        ("issues", "原记录未定事项"),
    ]:
        grouped = {}
        total = 0
        for source, record in entries:
            items = record.get(collection, [])
            items = items if isinstance(items, list) else [items]
            for item in items:
                total += 1
                fields = deepcopy(item)
                issues = []
                if isinstance(fields, dict):
                    fields.pop("reported_in", None)
                    if collection == "policies":
                        issues = fields.pop("issues", [])
                k = key(fields)
                group = grouped.setdefault(
                    k, {"fields": fields, "sources": [], "issues": []}
                )
                if source not in group["sources"]:
                    group["sources"].append(source)
                for issue in issues:
                    if issue not in group["issues"]:
                        group["issues"].append(issue)
        stats[collection] = {"input": total, "versions": len(grouped)}
        if grouped:
            parts += ["## " + title, ""]
        for group in grouped.values():
            fields = deepcopy(group["fields"])
            context = {
                k: fields.pop(k)
                for k in ("scope", "accounting_basis")
                if isinstance(fields, dict) and k in fields
            }
            if (
                collection == "policies"
                and isinstance(fields, dict)
                and isinstance(fields.get("text"), str)
            ):
                fields["text"] = shared.ref(
                    "N", ["literal_policy", fields["text"]], fields["text"]
                )
            parts += [
                "出现于：" + "、".join(group["sources"]),
                "",
                (
                    "口径：" + shared_context(context, labels, shared) + "\n\n"
                    if context
                    else ""
                )
                + describe(fields, labels),
                "",
            ]
            if group["issues"]:
                parts += [
                    "说明："
                    + shared.ref(
                        "N", group["issues"], describe(group["issues"], labels)
                    ),
                    "",
                ]
    for source, record in entries:
        unknown = record.get("unstructured_blocks", {})
        if unknown:
            parts += ["## " + source + " 未完成及额外内容", ""]
            for name, content in unknown.items():
                if isinstance(content, dict) and content.get("run_status") in {
                    "failed",
                    "truncated",
                }:
                    parts += [
                        name
                        + "："
                        + content["run_status"]
                        + "；原始返回另存未解析附件，不修复或隐藏。",
                        "",
                    ]
                else:
                    parts += [name + "：" + describe(content, labels), ""]
        extras = {
            k: v
            for k, v in record.items()
            if k
            not in {
                "schema_version",
                "stage",
                "company",
                "report_period",
                "reports",
                "tables",
                "policies",
                "changes",
                "events",
                "metadata",
                "management",
                "issues",
                "unstructured_blocks",
                "block_status",
            }
        }
        if extras:
            parts += [source + " 其他原字段：" + describe(extras, labels), ""]
    stats["formatting"] = {
        "shared_definitions": dict(
            Counter(
                k[0] for k in {i["id"] for i in shared.items.values() if i.get("id")}
            )
        ),
        "display_positions": len({tuple(c["span"]) for c in cells}),
        "shared_source_cells": sum(len(c["origins"]) - 1 for c in cells),
    }
    return "\n".join(parts), cells, stats


def audit(entries, plan, displayed, body=None):
    """Independently resolve every source mapping and the actual rendered cell inventory."""
    records = dict(entries)
    expected = {}
    for source, record in entries:
        for ti, table in enumerate(record.get("tables", [])):
            if not isinstance(table, dict) or not isinstance(table.get("rows"), list):
                continue
            for ri, row in enumerate(table["rows"]):
                if isinstance(row, dict) and isinstance(row.get("values"), dict):
                    for name, value in row["values"].items():
                        missing = row.get("missing_reasons", {})
                        expected[key([source, ti, ri, name])] = {
                            "value": value,
                            "missing": missing.get(name)
                            if isinstance(missing, dict)
                            else missing,
                        }
    actual, errors = {}, []
    positions = 0
    targets = {key(m["source"]): m["target"][:2] for m in plan["mappings"]}
    for cell in displayed:
        if body is not None:
            positions += 1
            start, end = cell.get("span", [-1, -1])
            rs, re_ = cell.get("row_span", [-1, -1])
            hs, he = cell.get("header_span", [-1, -1])
            if not (0 <= hs < he <= rs <= start <= end <= re_ <= len(body)) or (
                body[start:end] != escape(cell["display"])
                or body[rs:re_] != cell.get("row_display")
                or body[hs:he] != cell.get("header_display")
            ):
                errors.append(
                    [cell["origins"], "cell_row_or_column_missing_from_model_body"]
                )
        for origin in cell["origins"]:
            identity = key(origin)
            if identity in actual:
                errors.append([origin, "displayed_more_than_once"])
            actual[identity] = cell["observation"]
            if cell.get("target_row") != targets.get(identity):
                errors.append([origin, "display_row_mapping_changed"])
    if body is not None:
        for item in plan.get("definitions", []):
            if item["id"] + "：" + item["text"] not in body:
                errors.append([item["id"], "shared_definition_missing"])
    mapped = Counter(key(m["source"]) for m in plan["mappings"])
    for identity, observation in expected.items():
        if actual.get(identity) != observation or mapped[identity] != 1:
            errors.append(
                [json.loads(identity), "value_or_missing_reason_not_preserved"]
            )
    for mapping in plan["mappings"]:
        source, ti, ri, name = mapping["source"]
        si, target_row, semantic, vi = mapping["target"]
        section = plan["sections"][si]
        row = section["rows"][target_row]
        original = records[source]["tables"][ti]["rows"][ri]
        fields = {
            k: v
            for k, v in original.items()
            if k
            not in {
                "values",
                "missing_reasons",
                "period_overrides",
                "path",
                "issues",
            }
        }
        # Dimension order is immaterial, but every dimension member must survive.
        target_fields = deepcopy(row["fields"])
        for item in (fields, target_fields):
            if isinstance(item.get("dimensions"), list):
                item["dimensions"] = sorted(item["dimensions"], key=key)
        if target_fields != fields or original.get("path", []) not in row["names"]:
            errors.append([mapping["source"], "row_unit_statistic_or_identity_changed"])
        if row["columns"][semantic] != effective_column(
            records[source]["tables"][ti], original, name
        ):
            errors.append([mapping["source"], "effective_column_context_changed"])
        if (
            row["cells"][semantic][vi]["observation"]
            != expected[key(mapping["source"])]
        ):
            errors.append([mapping["source"], "mapped_value_changed"])
    errors.extend(
        [json.loads(k), "unexpected_display_cell"]
        for k in actual.keys() - expected.keys()
    )
    return {
        "input_cells": len(expected),
        "mapped_cells": len(plan["mappings"]),
        "displayed_input_cells": len(actual),
        "displayed_unique_cells": len(displayed),
        "displayed_positions": len({tuple(c.get("span", [])) for c in displayed}),
        "body_positions_checked": positions,
        "errors": errors,
        "passed": not errors and actual == expected,
    }
