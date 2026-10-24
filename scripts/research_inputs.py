"""Prepare a fixed three-round plan; never send or repair financial data."""

import math
import re
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from tokenizers import Tokenizer

from scripts import run_company_summary as io
from src.fundamental_data.handoff import separate

REPO = Path(__file__).resolve().parents[1]


def latest_market(sources, companies=()):
    """Read the declared companies' existing raw A-share day files, never fetch."""
    codes, protected = list(companies), []
    for source in sources:
        manifest = (source if source.is_dir() else source.parent) / "manifest.json"
        if manifest.is_file():
            data = io.load(manifest)
            if data.get("kind") == "stage3_input_v1":
                protected.append(manifest)
                if not companies:
                    codes.extend(row["company"] for row in data["companies"])
    if not codes:
        return "", protected
    text = "\n\n# 本轮最新已知价（本地原始日线，不是实时刷新）\n\n"
    text += "若旧底稿称行情未提供，以本节本轮快照为准。仅价格，不推算股数或市值。\n\n"
    text += "| 公司代码 | 未复权收盘价 | 行情日期 | 币种 | 状态/来源 |\n|---|---:|---|---|---|\n"
    root = REPO / "data/fundamental_quant/public_history/ranges"
    today = datetime.now(timezone(timedelta(hours=8))).date()
    notes = []
    for code in dict.fromkeys(map(str, codes)):
        if not re.fullmatch(r"\d{6}", code):
            text += f"| {code} | 未取得 | — | 未定 | 本适配仅读取A股原始日线 |\n"
            continue
        candidates, unreadable = [], []
        for path in sorted(root.glob(f"a_share_{code}_*.json")):
            try:
                data = io.load(path)
                if (
                    data.get("source") != "tencent_raw_day"
                    or data.get("issuer_id") != f"a_share:{code}"
                ):
                    continue
                for row in data.get("rows", []):
                    day, value = date.fromisoformat(row["date"]), float(row["close"])
                    if day <= today and math.isfinite(value) and value > 0:
                        candidates.append(
                            (day, value, data.get("currency") or "未定", path)
                        )
            except (ValueError, KeyError, TypeError, AttributeError, OSError) as error:
                unreadable.append(f"{path.name}: {type(error).__name__}")
        if candidates:
            newest = max(row[0] for row in candidates)
            latest = sorted({row for row in candidates if row[0] == newest})
            conflict = len({(row[1], row[2]) for row in latest}) > 1
            for day, value, currency, path in latest:
                protected.append(path)
                text += f"| {code} | {value:g} | {day} | {currency} | {'同日冲突，分别保留；' if conflict else ''}{path.name} |\n"
        else:
            text += (
                f"| {code} | 未取得 | — | 未定 | 已有原始行情中未找到，不自动补请求 |\n"
            )
        if unreadable:
            notes.append(f"{code}本地文件读取注记：" + "；".join(unreadable))
    text += "\n\n" + "\n\n".join(notes)
    return text, list(dict.fromkeys(protected))


def material(path):
    if path.is_dir():
        names = ("最终报告.md", "完整分析.md", "模型输入.md", "完整摘要.md")
        path = next((path / n for n in names if (path / n).is_file()), None)
        if path is None:
            raise ValueError("directory_has_no_supported_document")
    files = [path.resolve()]
    sidecar = path.parent / "图表数据.yaml"
    if path.name == "最终报告.md" and sidecar.is_file():
        files.append(sidecar.resolve())
    return files


def prepare(
    sources,
    output,
    config,
    feedback=(),
    focus="",
    budget=None,
    *,
    companies=(),
    local_prices=True,
):
    sources, feedback = (
        list(map(Path.resolve, sources)),
        list(map(Path.resolve, feedback)),
    )
    output, config = output.resolve(), config.resolve()
    separate(output, sources + feedback + [config])
    settings = io.load(config)
    if settings["request"]["model"] != "deepseek-flash":
        raise ValueError("pricing_configuration_requires_deepseek_flash")
    stages = settings["rounds"]
    if [r["id"] for r in stages] != ["draft", "review", "write"]:
        raise ValueError("exactly_three_fixed_rounds_required")
    files = list(dict.fromkeys(p for s in sources for p in material(s)))
    if not files:
        raise ValueError("no_research_material")
    market, market_files = (
        latest_market(sources, companies)
        if local_prices and settings.get("local_latest_price", False)
        else ("", [])
    )
    common_files = [REPO / p for p in settings["prompts"]]
    stage_files = [REPO / r["prompt"] for r in stages]
    prompt_files = common_files + stage_files
    questions = REPO / settings["questions"]
    tokenizer = REPO / settings["tokenizer"]
    assets = list(
        dict.fromkeys(
            p for f in files + feedback for p in (f.parent / "assets").glob("*.png")
        )
    )
    dependencies = [
        Path(__file__),
        REPO / "scripts/research_report.py",
        REPO / "scripts/research_render.py",
        REPO / "scripts/research_rounds.py",
        Path(io.__file__),
        REPO / "src/fundamental_data/handoff.py",
        REPO / "src/__init__.py",
    ]
    protected = (
        files
        + feedback
        + prompt_files
        + [questions, tokenizer, config]
        + assets
        + dependencies
        + market_files
    )
    hashes = {str(p.resolve()): io.sha(p) for p in protected}
    body = ""
    for i, path in enumerate(files + feedback):
        label = "用户反馈材料" if i >= len(files) else "研究材料"
        text = path.read_bytes().decode("utf8")
        for asset in (path.parent / "assets").glob("*.png"):
            name = io.sha(asset) + ".png"
            text = text.replace("assets/" + asset.name, "assets/" + name)
        body += f"\n\n# {label} {i + 1}：{path.name}\n\n" + text
    body += market
    common = "\n\n".join(p.read_text(encoding="utf8") for p in common_files)
    common += "\n\n# 沿用的七问覆盖表\n\n" + "\n".join(io.load(questions)["questions"])
    system = common + "\n\n" + stage_files[0].read_text(encoding="utf8")
    request = dict(
        settings["request"],
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": "本轮研究重点：" + focus + body},
        ],
    )
    count = io.estimate(request, Tokenizer.from_file(str(tokenizer)), settings)
    if count > settings["input_waterline"]:
        raise ValueError("input_over_waterline_no_truncation")
    maximum = Decimal(str(budget)) if budget is not None else None
    rates = settings["peak_rates"]
    jobs = []
    for i, stage in enumerate(stages):
        reserve = (
            Decimal(settings["model_input_capacity_bound"]) * Decimal(rates["input"])
            + Decimal(stage["max_tokens"]) * Decimal(rates["output"])
        ) / 1000000
        jobs.append(
            dict(
                stage,
                dependencies=[stages[i - 1]["id"]] if i else [],
                reserved_usd_peak=str(reserve),
                input_tokens_planning=count
                + (
                    settings["handoff_token_reserve"]
                    + settings["planned_images_each_later_round"]
                    * settings["image_token_reserve_each"]
                    if i
                    else 0
                ),
            )
        )
    reserve = sum(Decimal(j["reserved_usd_peak"]) for j in jobs)
    projected = {}
    for label, price in (("off_peak", settings["execution_rates"]), ("peak", rates)):
        projected[label] = str(
            sum(
                (
                    Decimal(j["input_tokens_planning"]) * Decimal(str(price["input"]))
                    + Decimal(j["output_planning_tokens"])
                    * Decimal(str(price["output"]))
                )
                / 1000000
                for j in jobs
            )
        )
    if maximum is not None and (
        not maximum.is_finite() or maximum <= 0 or maximum < reserve
    ):
        raise ValueError("invalid_or_insufficient_budget")
    plan = {
        k: settings[k]
        for k in (
            "pricing_utc_dates",
            "execution_rates",
            "price_source",
            "pricing_checked_date",
            "pricing_note",
            "peak_rates",
        )
    }
    plan.update(
        authorized_usd=str(maximum) if maximum is not None else None,
        maximum_calls=3,
        automatic_retries=0,
        automatic_additional_requests=False,
        tokenizer=str(tokenizer),
        input_tokens_estimate=count,
        input_waterline=settings["input_waterline"],
        endpoint=io.ENDPOINT,
        reserved_usd_peak=str(reserve),
        source_hashes=dict(hashes),
        estimated_uncached_cost_usd=projected,
        config=settings,
        jobs=jobs,
    )
    io.verify(hashes)
    for i, path in enumerate(files + feedback):
        io.save(output / "inputs" / f"{i + 1}_{path.name}", path.read_bytes())
    if market:
        io.save(output / "行情快照.md", market)
        for path in market_files:
            io.save(
                output / "market_sources" / (io.sha(path) + ".json"), path.read_bytes()
            )
    for asset in assets:
        target = output / "assets" / (io.sha(asset) + ".png")
        if not target.exists():
            io.save(target, asset.read_bytes())
    io.save(output / "模型输入.md", request["messages"][1]["content"])
    io.save(output / "完整prompt.md", system)
    io.save(output / "共通约定.md", common)
    for stage, path in zip(stages, stage_files):
        io.save(output / "prompts" / f"{stage['id']}.md", path.read_bytes())
    io.save(output / "request.json", request)
    plan["request_sha256"] = io.sha(output / "request.json")
    io.save(output / "plan.json", plan)
    io.verify(hashes)
    hashes.update(
        {str(p.resolve()): io.sha(p) for p in output.rglob("*") if p.is_file()}
    )
    io.save(output / "freeze.json", {"files": hashes, "python": sys.version})
    io.save(output / "freeze.sha256", io.sha(output / "freeze.json"))
    return plan
