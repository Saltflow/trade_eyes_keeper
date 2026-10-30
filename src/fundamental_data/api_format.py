"""Fixed output contracts, source checks and mechanical Markdown/CSV views."""

import csv
import io
import re
from decimal import Decimal
from itertools import combinations

METRICS = ("cfo", "capex", "investment_income")
TOPICS = (
    "revenue",
    "inventory",
    "ppe",
    "intangibles",
    "income_tax",
    "investment_income",
    "finance_cost",
)
FIELDS = (
    "industry",
    "business",
    "segments",
    "geographies",
    "business_model",
    "accounting_standard",
)


def validate(stage, data, sources, expected=None):
    issues = []
    if not isinstance(data, dict):
        return ["output_is_not_object"]
    required = {
        "extract": {
            "issuer_id",
            "company_name",
            "period_end",
            "facts",
            "mda",
            "topics",
            "fcf_boundary",
            "company",
            "gaps",
        },
        "history": {"documents", "common_policies", "changes", "management", "gaps"},
        "metadata": {"companies", "peer_pairs", "coverage_limits"},
    }[stage]
    if not required <= data.keys():
        return ["schema_missing_keys:" + ",".join(sorted(required - data.keys()))]
    if stage == "extract":
        if any(
            not isinstance(data[k], dict) for k in ("facts", "topics", "company")
        ) or any(not isinstance(v, dict) for v in data["facts"].values()):
            return ["schema_invalid_field_object"]
        if (
            set(data["facts"]) != set(METRICS)
            or set(data["topics"]) != set(TOPICS)
            or set(data["company"]) != set(FIELDS)
        ):
            return ["schema_wrong_metric_topic_or_metadata_set"]
        if expected and any(
            expected.get(k) and data.get(k) != expected[k]
            for k in ("issuer_id", "period_end")
        ):
            return ["schema_pdf_identity_disagrees_with_manifest"]
        if not re.fullmatch(
            r"[a-z_]+:[A-Za-z0-9]+", str(data["issuer_id"])
        ) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(data["period_end"])):
            return ["schema_invalid_company_or_period"]
        for metric, field in data["facts"].items():
            if field.get("status") == "obtained":
                value = field.get("amount")
                if (
                    not isinstance(value, str)
                    or not re.fullmatch(r"-?\d+(?:\.\d+)?", value)
                    or not Decimal(value).is_finite()
                ):
                    issues.append("invalid_decimal:" + metric)
                if field.get("scope") != "consolidated" or not field.get("unit"):
                    issues.append("missing_consolidated_basis_or_unit:" + metric)
                quotes = " ".join(
                    str(r.get("quote", ""))
                    for r in field.get("evidence", [])
                    if isinstance(r, dict)
                )
                numbers = re.findall(r"[−-]?\d[\d,]*(?:\.\d+)?", quotes)
                if (
                    isinstance(value, str)
                    and re.fullmatch(r"-?\d+(?:\.\d+)?", value)
                    and not any(
                        Decimal(n.replace(",", "").replace("−", "-")) == Decimal(value)
                        for n in numbers
                    )
                ):
                    issues.append("amount_not_in_quoted_row:" + metric)
            else:
                issues.append("minimum_metric_not_obtained:" + metric)
    if stage == "history":
        if any(not isinstance(data[k], list) for k in required):
            return ["schema_invalid_history_array"]
        if set(data["documents"]) != set(sources) or len(data["documents"]) != len(
            sources
        ):
            issues.append("history_does_not_cover_all_source_documents")
    if stage == "metadata":
        if any(not isinstance(data[k], list) for k in required) or any(
            not isinstance(c, dict) or "issuer_id" not in c for c in data["companies"]
        ):
            return ["schema_invalid_metadata_arrays"]
        if any(
            not isinstance(p, dict)
            or not isinstance(p.get("issuers"), list)
            or len(p["issuers"]) != 2
            for p in data["peer_pairs"]
        ):
            return ["schema_invalid_peer_pair"]
        ids = [c["issuer_id"] for c in data["companies"]]
        pairs = [tuple(sorted(p["issuers"])) for p in data["peer_pairs"]]
        if (
            set(ids) != set(expected)
            or len(ids) != len(set(ids))
            or set(pairs) != set(combinations(sorted(ids), 2))
            or len(pairs) != len(set(pairs))
        ):
            issues.append("metadata_company_or_pair_coverage_mismatch")
        if any(set(c.get("metadata", {})) != set(FIELDS) for c in data["companies"]):
            issues.append("metadata_fields_missing")
        if any(
            p.get("relation") not in {"candidate", "not_comparable", "undetermined"}
            for p in data["peer_pairs"]
        ):
            issues.append("invalid_peer_relation")

    def visit(node):
        if isinstance(node, list):
            for item in node:
                visit(item)
        elif isinstance(node, dict):
            if node.get("status") == "obtained" and not node.get("evidence"):
                issues.append("obtained_item_has_no_evidence")
            refs = node.get("evidence", [])
            if not isinstance(refs, list) or any(not isinstance(r, dict) for r in refs):
                issues.append("invalid_evidence_array")
                refs = []
            for ref in refs:
                doc = sources.get(ref.get("document_id"))
                page, quote = ref.get("page"), ref.get("quote")
                if (
                    not doc
                    or type(page) is not int
                    or str(page) not in doc["pages"]
                    or not isinstance(quote, str)
                    or not quote.strip()
                ):
                    issues.append("invalid_source_reference")
                elif re.sub(r"\s+", "", quote) not in re.sub(
                    r"\s+", "", doc["pages"][str(page)]
                ):
                    issues.append(f"quote_not_literal:{ref['document_id']}:{page}")
            for key, item in node.items():
                if key != "evidence":
                    visit(item)

    visit(data)
    return sorted(set(issues))


def financial_csv(records):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(
        [
            "issuer_id",
            "period",
            "metric",
            "amount",
            "unit",
            "currency",
            "scope",
            "status",
            "document_id",
            "physical_pages",
        ]
    )
    for doc_id, record in records.items():
        for metric, field in record.get("facts", {}).items():
            writer.writerow(
                [
                    record["issuer_id"],
                    record["period_end"],
                    metric,
                    field.get("amount"),
                    field.get("unit"),
                    field.get("currency"),
                    field.get("scope"),
                    field.get("status"),
                    doc_id,
                    ",".join(
                        str(ref.get("page", ""))
                        for ref in field.get("evidence", [])
                        if isinstance(ref, dict)
                    ),
                ]
            )
    return stream.getvalue()


def reading(stage, data, records=None):
    parts = [
        "# "
        + {
            "extract": "单份披露首次提取",
            "history": "多年整合首次结果",
            "metadata": "公司及同行首次整理",
        }[stage],
        "候选数据，来源及语义验收见相邻check.json；模型原文未修改。",
    ]
    if records:
        parts.append("```csv\n" + financial_csv(records) + "```")

    def lines(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "evidence" and isinstance(item, list):
                    for ref in item:
                        if isinstance(ref, dict):
                            yield f"> {ref.get('quote', '')}（{ref.get('document_id', '')} 物理页{ref.get('page', '')}）"
                elif isinstance(item, (list, dict)):
                    yield "**" + key + "**"
                    yield from lines(item)
                else:
                    yield f"- {key}：{item if item is not None else '缺失'}"
        elif isinstance(value, list):
            for index, item in enumerate(value, 1):
                yield f"条目{index}"
                yield from lines(item)
        else:
            yield str(value)

    for key, value in data.items():
        parts.extend(["## " + key, "\n\n".join(lines(value))])
    return "\n\n".join(parts) + "\n"
