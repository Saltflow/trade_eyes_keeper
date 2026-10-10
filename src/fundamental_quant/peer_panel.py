"""Offline, provenance-preserving time/company panel from immutable FR packs."""

import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from .fr_pack import extract_field, read_json, verified_pack, write_json
from .fr_pack_gate import assess_materials

METRICS = [
    "revenue",
    "cost",
    "net_profit_parent",
    "net_profit_total",
    "cfo",
    "capex",
    "parent_equity",
    "inventory",
    "receivables",
    "payables",
    "contract_liabilities",
    "investment_income",
    "interest_expense",
]
VERSION = "peer-panel/1"


def ratio(numerator, denominator):
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def cagr(start, end, intervals):
    if start is None or end is None or start <= 0 or end <= 0 or intervals <= 0:
        return None
    return (end / start) ** (1 / intervals) - 1


def editorial_digest(panel):
    return hashlib.sha256(
        json.dumps(
            {k: panel[k] for k in ["rows", "cost_anchors", "reviewed_source_facts"]},
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


def build_panel(pack_root: Path, output: Path):
    output.mkdir(parents=True, exist_ok=True)
    observations, documents = [], []
    for entry in read_json(pack_root / "latest_run.json")["entries"]:
        if "pack_key" not in entry:
            documents.append(
                {
                    "document": entry.get("document"),
                    "errors": entry.get("errors"),
                    "unavailable": True,
                }
            )
            continue
        folder = pack_root / "packs" / entry["pack_key"]
        pack, source = verified_pack(folder)
        meta = pack["metadata"]
        fields = dict(pack["financial_fields"])
        fields["net_profit_total"] = extract_field(
            "net_profit_total",
            ("合并净利润", "income", "净利润"),
            pack["sections"],
            meta,
        )
        gate = assess_materials(pack["financial_fields"], meta, source)
        documents.append(
            {
                "document": meta["short_id"],
                "company": meta["company_name"],
                "issuer": meta["issuer_id"],
                "period": meta["period_end"],
                "kind": meta["kind"],
                "published_at": meta["published_at"],
                "pack_key": entry["pack_key"],
                "source_sha256": meta["sha256"],
                "source_path": meta["path"],
                "pack_folder": str(folder.resolve()),
                "gate": gate,
            }
        )
        for metric in METRICS:
            field = fields[metric]
            if field["status"] != "obtained":
                continue
            for column in ("current", "comparative"):
                amount = field.get(
                    "amount" if column == "current" else "comparative_amount"
                )
                period = field.get(
                    "period_end" if column == "current" else "comparative_period_end"
                )
                if amount is None or not period:
                    continue
                original_period = period
                if metric in {
                    "parent_equity",
                    "inventory",
                    "receivables",
                    "payables",
                    "contract_liabilities",
                } and period.endswith("01-01"):
                    period = (
                        date.fromisoformat(period) - timedelta(days=1)
                    ).isoformat()
                kind = (
                    "FY"
                    if period.endswith("12-31")
                    else "H1"
                    if period.endswith("06-30")
                    else "unknown"
                )
                observations.append(
                    {
                        "issuer": meta["issuer_id"],
                        "company": meta["company_name"],
                        "metric": metric,
                        "period": period,
                        "kind": kind,
                        "amount": amount,
                        "unit": "元",
                        "currency": field.get("currency"),
                        "scope": "consolidated",
                        "document": meta["short_id"],
                        "published_at": meta["published_at"],
                        "column": column,
                        "original_column_date": original_period,
                        "pages": field["source_ref"]["pages"],
                        "quote": field["quote"],
                        "header": field["header"],
                        "origin": "immutable_pack"
                        if metric != "net_profit_total"
                        else "same_source_supplement",
                        "pack_key": entry["pack_key"],
                    }
                )
    reviewed = (
        read_json(output / "review_source_facts.json")
        if (output / "review_source_facts.json").exists()
        else {}
    )
    sources = {}
    for doc in documents:
        if "pack_folder" in doc:
            sources[doc["document"]] = read_json(
                Path(doc["pack_folder"]) / "source.json"
            )

    def verify_reviewed(fact):
        source = sources[fact["document_id"]]
        if fact.get("issuer_id") != source["metadata"]["issuer_id"]:
            raise ValueError("reviewed_fact_wrong_issuer")
        if (
            fact.get("source_pdf_sha256", source["metadata"]["sha256"])
            != source["metadata"]["sha256"]
        ):
            raise ValueError("reviewed_fact_wrong_source")
        if fact["quote"] not in source["pages"][str(fact["page"])]:
            raise ValueError("reviewed_fact_quote_not_found")

    for fact in reviewed.get("history_2021", []):
        verify_reviewed(fact)
        observations.append(
            {
                "issuer": fact["issuer_id"],
                "company": fact["company_name"],
                "metric": {"parent_net_profit": "net_profit_parent"}.get(
                    fact["metric"], fact["metric"]
                ),
                "period": fact["period_end"],
                "kind": "FY",
                "amount": str(Decimal(fact["raw_value"].replace(",", ""))),
                "unit": "元",
                "currency": fact["currency"],
                "scope": fact["scope"],
                "document": fact["document_id"],
                "published_at": fact["published_at"],
                "column": "reviewed_2021_comparative",
                "original_column_date": fact["period_end"],
                "pages": [fact["page"]],
                "quote": fact["quote"],
                "header": "2023年报三年摘要；适用重述后",
                "origin": "manual_source_review",
                "restated": fact.get("restated"),
            }
        )
    for fact in reviewed.get("shares_2025", []) + reviewed.get(
        "management_statements", []
    ):
        verify_reviewed(fact)
    anchors = []
    for group in reviewed.get("cost_anchors_2025", []):
        values = {}
        for fact in group["fields"]:
            verify_reviewed(fact)
            if (
                fact["scope"] != group["scope"]
                or fact["period_end"] != group["period_end"]
            ):
                raise ValueError("cost_anchor_scope_or_period_mismatch")
            scale = {"元": 1, "万元": 10000, "亿元": 100000000, "吨": 1}[fact["unit"]]
            values[fact["metric"]] = float(
                Decimal(fact["raw_value"].replace(",", "")) * scale
            )
        volume = values.get("sales_volume")
        anchors.append(
            {
                **group,
                "calculated": {
                    "revenue_per_tonne": ratio(values.get("segment_revenue"), volume),
                    "cost_per_tonne": ratio(values.get("segment_cost"), volume),
                    "segment_gross_margin": 1
                    - values["segment_cost"] / values["segment_revenue"]
                    if values.get("segment_revenue", 0) > 0
                    else None,
                },
            }
        )
    # Keep disagreements visible; a later filing is not sufficient proof to erase them.
    grouped = {}
    for observation in observations:
        grouped.setdefault(
            (observation["issuer"], observation["period"], observation["metric"]), []
        ).append(observation)
    rows, conflicts = {}, []
    for (issuer, period, metric), facts in sorted(grouped.items()):
        row = rows.setdefault(
            (issuer, period),
            {
                "issuer": issuer,
                "company": facts[0]["company"],
                "period": period,
                "kind": facts[0]["kind"],
                "values": {},
                "refs": {},
            },
        )
        numbers = {Decimal(f["amount"]) for f in facts}
        row["refs"][metric] = facts
        if len(numbers) != 1:
            row["values"][metric] = None
            conflicts.append(
                {
                    "issuer": issuer,
                    "period": period,
                    "metric": metric,
                    "observations": facts,
                }
            )
        else:
            row["values"][metric] = float(next(iter(numbers)))
    for (issuer, period), row in rows.items():
        v = row["values"]
        rv = v.get
        row["calculated"] = {
            "gross_margin": ratio(rv("revenue") - rv("cost"), rv("revenue"))
            if rv("revenue") is not None and rv("cost") is not None
            else None,
            "net_margin_consolidated": ratio(rv("net_profit_total"), rv("revenue")),
            "cfo_conversion": ratio(rv("cfo"), rv("net_profit_total")),
            "fcf_proxy": rv("cfo") - rv("capex")
            if rv("cfo") is not None and rv("capex") is not None
            else None,
            "capex_intensity": ratio(rv("capex"), rv("revenue")),
            "inventory_cost_multiple": ratio(rv("inventory"), rv("cost"))
            if row["kind"] == "FY"
            else None,
        }
        previous_period = str(int(period[:4]) - 1) + period[4:]
        previous = rows.get((issuer, previous_period), {}).get("values", {})
        for metric in ("revenue", "net_profit_parent", "cfo"):
            relative = ratio(rv(metric), previous.get(metric))
            row["calculated"][metric + "_yoy"] = (
                relative - 1 if relative is not None else None
            )
        # Average parent equity, never consolidated earnings/parent equity.
        start_equity = None
        if row["kind"] == "FY":
            candidates = [
                f
                for f in observations
                if f["issuer"] == issuer
                and f["metric"] == "parent_equity"
                and f["column"] == "comparative"
                and f["period"] == previous_period
                and any(
                    d["document"] == f["document"] and d["period"] == period
                    for d in documents
                )
            ]
            unique = {Decimal(f["amount"]) for f in candidates}
            if len(unique) == 1:
                start_equity = float(next(iter(unique)))
            end_equity = rv("parent_equity")
            avg = (
                (start_equity + end_equity) / 2
                if start_equity is not None and end_equity is not None
                else None
            )
            row["calculated"]["simple_roe"] = ratio(rv("net_profit_parent"), avg)
    for fact in reviewed.get("shares_2025", []):
        row = rows[(fact["issuer_id"], fact["period_end"])]
        shares = float(Decimal(fact["raw_value"].replace(",", "")))
        row["values"]["shares"] = shares
        row["refs"]["shares"] = [fact]
        row["calculated"]["bvps"] = ratio(row["values"].get("parent_equity"), shares)
    growth = []
    for issuer in sorted({row["issuer"] for row in rows.values()}):
        annual = sorted(
            [
                row
                for row in rows.values()
                if row["issuer"] == issuer and row["kind"] == "FY"
            ],
            key=lambda x: x["period"],
        )
        for metric in ("revenue", "net_profit_parent", "cfo", "fcf_proxy"):
            valid = [
                (
                    int(row["period"][:4]),
                    row["calculated"].get(metric)
                    if metric == "fcf_proxy"
                    else row["values"].get(metric),
                )
                for row in annual
            ]
            valid = [(y, value) for y, value in valid if value is not None]
            if len(valid) >= 2:
                first, last = valid[0], valid[-1]
                growth.append(
                    {
                        "issuer": issuer,
                        "company": annual[0]["company"],
                        "metric": metric,
                        "start_year": first[0],
                        "end_year": last[0],
                        "intervals": last[0] - first[0],
                        "value": cagr(first[1], last[1], last[0] - first[0]),
                    }
                )
    panel = {
        "version": VERSION,
        "growth": growth,
        "cost_anchors": anchors,
        "reviewed_source_facts": reviewed,
        "documents": documents,
        "rows": list(rows.values()),
        "conflicts": conflicts,
        "model_calls": 0,
        "external_requests": 0,
        "formulas": {
            "fcf_proxy": "合并CFO - 购建长期资产支付现金；不是正常化股东FCF",
            "cfo_conversion": "合并CFO / 合并净利润；不是归母现金转换率",
            "simple_roe": "归母净利润 / ((年初归母净资产 + 年末归母净资产)/2)；不是披露加权ROE",
            "gross_margin": "(营业收入 - 营业成本) / 营业收入；不使用包含财务公司利息的营业总收入",
            "inventory_cost_multiple": "期末存货 / 当年营业成本；不是周转天数",
            "yoy": "同公司同期间本期/上期-1；负基数不输出百分比",
            "cagr": "(终值/起值)^(1/实际年度间隔)-1；非正值不计算",
        },
        "limitations": [
            "资料完整性门槛保留；未通过的资料可展示已核对单项事实，不能被标成整包或投资研究验收通过。",
            "多数非茅台报表仅标元，列报币种未在表头重复声明；保留currency为空，不做换汇。",
            "CFO包含财务公司、少数股东及营运资本影响，不据此横向排名股东现金质量。",
            "2026H1独立同比2025H1，不乘二，也不进入年度CAGR。",
        ],
    }
    basis = output / "editorial_basis.json"
    if basis.exists() and read_json(basis)["sha256"] != editorial_digest(panel):
        raise ValueError("editorial_evidence_changed_preserving_previous_panel")
    write_json(output / "panel.json", panel)
    return panel
