"""Fixed-rule local Ke replay; never searches, promotes, or sends reports.

Inputs are copied to a fresh run directory. Missing historical valuations stay
unavailable. Such a replay is diagnostic and cannot qualify for promotion.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from zoneinfo import ZoneInfo

from src.backtest.engine import Backtester, build_trade_plan
from src.data.backtest_data import prepare_backtest_data
from src.data.ke_history_store import KeHistoryStore
from src.data.market_history import PointInTimeMarketStore
from src.instruments.calculations import derive_company_fundamentals
from src.instruments.classifier import detect_market
from src.instruments.models import MetricStatus, MetricValue
from src.instruments.point_in_time import (
    PointInTimeFundamentalStore,
    adjust_statement_shares,
)
from src.markets import get_skip_signals
from src.search.config import get_market_optimizer_configs
from src.strategy.api import Params
from src.strategy.fundamental_context import FundamentalStrategyMarketData

CONTRACT = "daily-implied-ke-history/1"


class MemoryStatementStore(PointInTimeFundamentalStore):
    def __init__(self, statements):
        self.statements = statements

    def read_all(self, code):
        return self.statements


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def symbols_from(config: dict) -> list[str]:
    return list(
        dict.fromkeys(
            str(item.get("stock_code", item.get("code", "")))
            if isinstance(item, dict)
            else str(item)
            for item in config["stocks"]
        )
    )


def render_report(output: Path, summary: dict) -> None:
    """The presentation contains return curves; diagnostics stay in JSON/CSV."""
    from src.notification.report_policy import (
        generate_return_curve_png,
        return_curves_html,
    )

    payload = summary["return_curves"]
    png = generate_return_curve_png(payload)
    image_source = None
    if png:
        (output / "return_curves.png").write_bytes(png)
        image_source = "return_curves.png"
    document = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>Ke策略收益曲线</title><style>body{font-family:system-ui,sans-serif;max-width:1100px;margin:28px auto;padding:0 18px;line-height:1.6}pre{white-space:pre-wrap;font-family:inherit}img{max-width:100%}</style>"""
    document += return_curves_html(payload, image_source=image_source)
    document += "</html>"
    (output / "report.html").write_text(document, encoding="utf-8")


def make_return_curves(summary: dict, output: Path) -> dict:
    from src.strategy.return_curves import CURVE_CONTRACT

    payload = {
        "contract": CURVE_CONTRACT,
        "strategy_id": "ke_threshold_allocation",
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "universe": list(summary["universe"]),
        "markets": {},
        "source_sha256": summary["source_sha256"],
        "request": summary["request"],
    }
    for group, item in summary["markets"].items():
        active = [a for a in item["ke_readiness"].values() if not a["excluded"]]
        usable = sum(a["available_days"] > 0 for a in active)
        status = "unavailable" if not usable else "partial"
        required = [a for a in active if a.get("valuation_status") != "not_applicable"]
        if required and all(
            a.get("required_days", 0) > 0
            and a["available_days"] + a.get("known_nonpositive_days", 0)
            >= a["required_days"]
            for a in required
        ):
            status = "complete"
        nav = pd.read_csv(output / f"{group}_nav.csv")
        message = f"历史Ke可计算 {usable}/{len(required)} 只适用PE的交易标的"
        gaps = {
            code: max(
                a.get("required_days", 0)
                - a["available_days"]
                - a.get("known_nonpositive_days", 0),
                0,
            )
            for code, a in item["ke_readiness"].items()
            if not a["excluded"] and a.get("valuation_status") != "not_applicable"
        }
        gaps = {code: count for code, count in gaps.items() if count}
        if gaps:
            message += "；" + "、".join(
                f"{code}缺{count}个交易日" for code, count in gaps.items()
            )
        not_applicable = len(active) - len(required)
        if not_applicable:
            message += f"；{not_applicable} 只REIT不适用普通股票PE"
        reference = [
            code
            for code, a in item["ke_readiness"].items()
            if a.get("valuation_status") != "not_applicable"
            and a.get("valuation_basis")
            in {
                "official_index_reference",
                "third_party_index_reference",
                "lagged_reference",
            }
        ]
        if reference:
            message += "；基金采用指数估值参考：" + ", ".join(reference)
            coverage_label = (
                "含指数PE参考" if status == "complete" else "部分历史估值缺失"
            )
            status = "partial"
        else:
            coverage_label = "完整" if status == "complete" else "历史Ke覆盖不完整"
        payload["markets"][group] = {
            "dates": list(nav["date"]),
            "values": list(nav["nav"]),
            "initial_capital": item["initial_capital"],
            "data_as_of": item["dates"][-1],
            "coverage": {
                "status": status,
                "message": message,
                "label": coverage_label,
                "missing_trading_days": gaps,
            },
        }
    return payload


def prepare(config_path: Path, source_root: Path, output: Path, end: str) -> None:
    if output.exists():
        raise FileExistsError(f"run directory already exists: {output}")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    # This explicit allowlist excludes account, bot, email and LLM credentials.
    config = {
        key: raw[key]
        for key in (
            "stocks",
            "optimizer",
            "skip_search",
            "skip_signals",
            "point_in_time_data",
        )
        if key in raw
    }
    output.mkdir(parents=True)
    root = output / "inputs" / "point_in_time"
    for name in ("market", "fundamentals", "listing_dates", "action_evidence"):
        if (source_root / name).is_dir():
            shutil.copytree(source_root / name, root / name)
    config.setdefault("point_in_time_data", {})["output_dir"] = str(root.resolve())
    # A cache copy is the only location providers may update.
    start = (pd.Timestamp(end) + pd.Timedelta(days=1) - pd.DateOffset(months=24)).date()
    write_json(output / "config.json", config)
    write_json(
        output / "request.json",
        {
            "start": start.isoformat(),
            "end": end,
            "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
            "source_store": str(source_root.resolve()),
            "search": False,
            "promotion": False,
        },
    )
    result = prepare_backtest_data(
        config,
        [c for c in symbols_from(config) if c not in get_skip_signals(config)],
        start,
        end,
        purpose="backtest",
        require_current=True,
        readiness_path=output / "market_readiness.json",
    )
    print(
        json.dumps(
            {
                "ready": result.ready,
                "ready_codes": result.ready_codes,
                "issues": [item.as_dict() for item in result.issues],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


def make_ke_context(
    store,
    bundles,
    execution,
    excluded,
    diagnostics,
    *,
    start_date=None,
    supplements=None,
):
    """Daily raw-price earnings yield from only previously published filings.

    Date-only filings become visible the next session. Currency conversion uses
    the existing execution profile's fixed CNY-per-currency contract. Annual
    fallback is retained from derive_company_fundamentals and counted below.
    """

    def enrich(market_data):
        shape = (len(market_data.dates), len(market_data.symbols), 1)
        values = np.full(shape, np.nan, dtype=np.float64)
        available = np.zeros(shape, dtype=bool)
        sources = np.full(shape[:2], None, dtype=object)
        tradable = (
            np.ones(shape[:2], dtype=bool)
            if market_data.tradable is None
            else np.asarray(market_data.tradable, dtype=bool).copy()
        )
        currency_rates = {
            "CNY": 1.0,
            "RMB": 1.0,
            "HKD": float(execution.fx_rates["hk"]),
            "USD": float(execution.fx_rates["us"]),
        }
        for col, code in enumerate(market_data.symbols):
            bundle = bundles[code]
            price_map = dict(
                zip(
                    pd.to_datetime(bundle.prices["date"]).dt.date,
                    bundle.prices["raw_close"],
                )
            )
            if "tradable" in bundle.prices:
                flags = dict(
                    zip(
                        pd.to_datetime(bundle.prices["date"]).dt.date,
                        bundle.prices["tradable"],
                    )
                )
                for row, stamp in enumerate(market_data.dates):
                    tradable[row, col] &= bool(
                        flags.get(pd.Timestamp(stamp).date(), False)
                    )
            statements = store.read_all(code)
            currencies = {str(s.currency).upper() for s in statements if s.currency}
            item = {
                "excluded": code in excluded,
                "statement_count": len(statements),
                "currencies": sorted(currencies),
                "available_days": 0,
                "annual_fallback_days": 0,
                "first_ke_date": None,
                "last_ke_date": None,
                "missing_reason": None,
                "oldest_latest_report_age_days": 0,
                "split_safe_income_per_share_days": 0,
                "cumulative_eps_guard_days": 0,
                "nonpositive_reported_ttm_days": 0,
                "required_days": 0,
                "known_nonpositive_days": 0,
                "source_documents": {},
            }
            diagnostics[code] = item
            if code in excluded:
                item["missing_reason"] = "configured_skip_signals"
                continue
            quote_currency = (
                bundle.currency
                or {"a_share": "CNY", "hk": "HKD", "us": "USD"}[detect_market(code)]
            )
            if quote_currency not in currency_rates:
                item["missing_reason"] = "unknown_quote_currency"
                continue
            # Reuse the store's exact revision preference without disk rereads.
            memory = MemoryStatementStore(statements)
            for row, stamp in enumerate(market_data.dates):
                day = pd.Timestamp(stamp).date()
                if start_date is not None and day < date.fromisoformat(start_date):
                    continue
                price = price_map.get(day)
                if price is None or not np.isfinite(price) or price <= 0:
                    continue
                item["required_days"] += int(tradable[row, col])
                supplement = supplements.fund(code, day) if supplements else None
                if supplement is not None:
                    item["valuation_status"] = supplement["status"]
                    item["valuation_basis"] = supplement["valuation_basis"]
                    item["missing_reason"] = supplement.get("reason")
                    if supplement["status"] == "available":
                        values[row, col, 0] = supplement["ke"]
                        available[row, col, 0] = True
                        sources[row, col] = date.fromisoformat(
                            supplement["available_from"]
                        )
                        item["available_days"] += 1
                        item["first_ke_date"] = item["first_ke_date"] or str(day)
                        item["last_ke_date"] = str(day)
                        item["source_documents"][supplement["source_url"]] = supplement[
                            "source_sha256"
                        ]
                    continue
                supplement = supplements.earnings(code, day) if supplements else None
                if supplement is not None:
                    item["valuation_basis"] = "disclosed_listed_share_ttm_eps"
                    item["missing_reason"] = supplement.get("reason")
                    eps = supplement["ttm_eps"]
                    currency = supplement["currency"]
                    if eps is None or currency not in currency_rates:
                        continue
                    if eps <= 0:
                        item["known_nonpositive_days"] += 1
                        continue
                    ratio = currency_rates[currency] / currency_rates[quote_currency]
                    values[row, col, 0] = 0.02 + eps * ratio / float(price)
                    available[row, col, 0] = True
                    sources[row, col] = date.fromisoformat(supplement["available_from"])
                    item["available_days"] += 1
                    item["first_ke_date"] = item["first_ke_date"] or str(day)
                    item["last_ke_date"] = str(day)
                    item["latest_eps_period_end"] = supplement["period_end"]
                    for component in supplement["components"]:
                        item["source_documents"][component["source_url"]] = component[
                            "document_sha256"
                        ]
                    continue
                known = memory.as_of(code, day - pd.Timedelta(days=1))
                if not known:
                    continue
                known_currencies = {
                    str(s.currency).upper() for s in known if s.currency
                }
                if len(known_currencies) != 1 or not known_currencies.issubset(
                    currency_rates
                ):
                    item["missing_reason"] = "missing_or_ambiguous_statement_currency"
                    continue
                currency = next(iter(known_currencies))
                ratio = currency_rates[currency] / currency_rates[quote_currency]
                item["earnings_to_quote_fx"] = ratio
                known = adjust_statement_shares(known, bundle.actions, day)
                reported_ttm = [
                    s
                    for s in known
                    if "diluted_eps_contains_baostock_ttm_eps" in s.diagnostics
                ]
                latest_ttm = (
                    max(reported_ttm, key=lambda s: (s.period_end, s.published_at))
                    if reported_ttm
                    else None
                )
                if latest_ttm is not None and (
                    latest_ttm.diluted_eps is None
                    or not np.isfinite(latest_ttm.diluted_eps)
                    or latest_ttm.diluted_eps <= 0
                ):
                    item["nonpositive_reported_ttm_days"] += 1
                    item["known_nonpositive_days"] += 1
                    continue
                company = derive_company_fundamentals(
                    known,
                    current_price=MetricValue(
                        value=1.0,
                        status=MetricStatus.OBSERVED,
                        as_of=day,
                        source="unit_price_in_statement_currency",
                        currency=currency,
                    ),
                    evaluation_date=day,
                )
                pe = company.pe_ttm.value
                eps = (
                    1.0 / pe if pe is not None and np.isfinite(pe) and pe > 0 else None
                )
                if latest_ttm is not None:
                    eps = latest_ttm.diluted_eps
                eps_period_end = (
                    latest_ttm.period_end
                    if latest_ttm is not None
                    else company.ttm_net_income_parent.as_of
                )
                split_crossed = any(
                    a.share_multiplier is not None
                    and a.share_multiplier != 1
                    and a.ex_date <= day
                    and (a.published_at is None or a.published_at < day)
                    and (eps_period_end is None or eps_period_end < a.ex_date)
                    for a in bundle.actions
                )
                cumulative_eps_unsafe = latest_ttm is None and any(
                    s.diluted_eps is not None
                    and s.is_cumulative
                    and s.period_type in {"quarter", "3M"}
                    and "diluted_eps_contains_baostock_ttm_eps" not in s.diagnostics
                    for s in known
                )
                if split_crossed or cumulative_eps_unsafe:
                    # Reported historical EPS may already be restated for a
                    # split. Do not guess its basis or divide it a second time.
                    # Parent earnings / action-adjusted valuation shares has a
                    # clear numerator and denominator in the current share unit.
                    income = company.ttm_net_income_parent.value
                    shares = company.total_shares.value
                    eps = (
                        income / shares
                        if income is not None
                        and shares is not None
                        and np.isfinite(income)
                        and np.isfinite(shares)
                        and income > 0
                        and shares > 0
                        else None
                    )
                    if eps is not None:
                        item["split_safe_income_per_share_days"] += int(split_crossed)
                        item["cumulative_eps_guard_days"] += int(cumulative_eps_unsafe)
                if eps is None:
                    continue
                ke = 0.02 + eps * ratio / float(price)
                if not np.isfinite(ke):
                    continue
                values[row, col, 0] = ke
                available[row, col, 0] = True
                sources[row, col] = max(s.published_at for s in known if s.published_at)
                item["oldest_latest_report_age_days"] = max(
                    item["oldest_latest_report_age_days"],
                    (day - max(s.period_end for s in known)).days,
                )
                item["available_days"] += 1
                if "年报" in (company.ttm_net_income_parent.note or ""):
                    item["annual_fallback_days"] += 1
                item["first_ke_date"] = item["first_ke_date"] or str(day)
                item["last_ke_date"] = str(day)
            if (
                not item["available_days"]
                and item.get("valuation_status") != "not_applicable"
            ):
                item["missing_reason"] = "no_positive_causal_historical_pe"
        return FundamentalStrategyMarketData(
            **{
                name: getattr(market_data, name)
                for name in (
                    "indicator_matrix",
                    "dates",
                    "symbols",
                    "prices",
                    "highs",
                    "lows",
                    "date_ordinals",
                    "market",
                    "observation_counts",
                )
            },
            tradable=tradable,
            fundamental_features=values,
            fundamental_availability_mask=available,
            fundamental_feature_names=("implied_ke",),
            fundamental_feature_contract=CONTRACT,
            fundamental_as_of_dates=sources,
            fundamental_historical_walk_forward_eligible=True,
        ).validate_fundamental_panel()

    return enrich


def replay(output: Path) -> None:
    from src.strategy.plugins.ke_threshold_allocation import (
        KeThresholdAllocationStrategy,
    )

    config = json.loads((output / "config.json").read_text(encoding="utf-8"))
    request = json.loads((output / "request.json").read_text(encoding="utf-8"))
    readiness = json.loads(
        (output / "market_readiness.json").read_text(encoding="utf-8")
    )
    if readiness["issues"]:
        raise ValueError("market data incomplete; see market_readiness.json")
    root = Path(config["point_in_time_data"]["output_dir"])
    supplements = KeHistoryStore(root)
    store = PointInTimeMarketStore(root)
    codes = symbols_from(config)
    bundles = {c: store.read(c, config=config) for c in codes}
    if any(b is None for b in bundles.values()):
        raise ValueError("a configured instrument is missing its market bundle")
    frames = {
        code: pd.DataFrame(
            {
                "date": pd.to_datetime(b.prices["date"]),
                **{
                    key: b.prices[f"qfq_{key}"]
                    for key in ("open", "high", "low", "close")
                },
                "volume": b.prices["volume"],
                "tradable": b.prices["tradable"].astype(bool),
            }
        )
        for code, b in bundles.items()
    }
    summary = {
        "request": request,
        "universe": codes,
        "markets": {},
        "qualified_for_promotion": False,
        "valuation": "daily Ke=2%+1/positive PE; date-only filing visible next day",
        "fx": "existing configured fixed FX, not historical floating FX",
        "overrides": "no historical manual price overrides supplied",
        "supplemental_source_contracts": supplements.source_contracts,
        "source_sha256": {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (
                Path(__file__),
                Path("src/backtest/engine.py"),
                Path("src/strategy/plugins/ke_threshold_allocation.py"),
                Path("src/instruments/calculations.py"),
                Path("src/instruments/point_in_time.py"),
                Path("src/data/historical_ke_filings.py"),
                Path("src/data/historical_fund_ke.py"),
                Path("src/data/ke_history_store.py"),
            )
        },
        "valuation_limits": [
            "Existing annual fallback retained where complete quarterly earnings are unavailable",
            "Split/cumulative-EPS guard uses parent TTM earnings / adjusted shares",
            "Cache availability and report publication dates control when newer statements become usable",
            "No replay against frozen incumbent strategy was performed in this diagnostic run",
        ],
    }
    for group, resolved in get_market_optimizer_configs(config).items():
        selected = [c for c in codes if detect_market(c) == group]
        diagnostics = {}
        execution = resolved.constraints.execution
        context = make_ke_context(
            PointInTimeFundamentalStore(root),
            bundles,
            execution,
            get_skip_signals(config),
            diagnostics,
            start_date=request["start"],
            supplements=supplements,
        )
        strategy = KeThresholdAllocationStrategy()
        plan, data, fills, actual = build_trade_plan(
            frames,
            selected,
            strategy,
            Params({}),
            start_date=request["start"],
            end_date=request["end"],
            context_enricher=context,
            market_bundles=bundles,
        )
        if plan is None or set(actual) != set(selected):
            raise ValueError(f"incomplete market plan: {group}")
        trace = Backtester(execution, group).simulate(plan, data, fills)
        nav = np.asarray(trace.daily_values, dtype=float)
        pd.DataFrame({"date": trace.daily_dates, "nav": nav}).to_csv(
            output / f"{group}_nav.csv", index=False
        )
        # Zero conviction is the executor's sentinel, not a measured zero Ke.
        # Positive-PE daily Ke is strictly above 2%; preserve unavailable as NaN.
        ke_values = np.asarray(plan.conviction, dtype=float)
        pd.DataFrame(
            np.where(ke_values > 0, ke_values, np.nan),
            index=plan.dates,
            columns=plan.symbols,
        ).to_csv(output / f"{group}_ke.csv")
        item = {
            "symbols": selected,
            "dates": [trace.daily_dates[0], trace.daily_dates[-1]],
            "execution": asdict(execution),
            "initial_capital": execution.initial_capital,
            "final_nav": float(nav[-1]),
            "net_return_pct": (float(nav[-1]) / execution.initial_capital - 1) * 100,
            "max_drawdown_pct": trace.max_drawdown_pct,
            "trades": trace.total_trades,
            "gross_dividend_cash": trace.gross_dividend_cash,
            "dividend_tax_cost": trace.dividend_tax_cost,
            "net_dividend_cash": trace.net_dividend_cash,
            "pending_order_count": trace.pending_order_count,
            "final_cash": float(trace.final_cash),
            "positions": dict(zip(actual, np.asarray(trace.final_shares).tolist())),
            "ke_readiness": diagnostics,
        }
        summary["markets"][group] = item
        write_json(output / "summary.json", summary)
        print(
            json.dumps(
                {
                    "market": group,
                    "return_pct": item["net_return_pct"],
                    "trades": trace.total_trades,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    files = [p for p in root.rglob("*") if p.is_file()]
    write_json(
        output / "input_manifest.json",
        {
            str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in files
        },
    )
    summary["return_curves"] = make_return_curves(summary, output)
    write_json(output / "strategy_return_curves.json", summary["return_curves"])
    write_json(output / "summary.json", summary)
    render_report(output, summary)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "replay"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--source-store", type=Path)
    parser.add_argument(
        "--end", default=datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    )
    args = parser.parse_args()
    if args.mode == "prepare":
        if args.config is None or args.source_store is None:
            parser.error("prepare requires --config and --source-store")
        prepare(args.config, args.source_store, args.output, args.end)
    else:
        replay(args.output)


if __name__ == "__main__":
    main()
