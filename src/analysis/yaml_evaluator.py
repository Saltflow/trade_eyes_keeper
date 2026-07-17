"""YAML 策略统一回测器。

所有策略评估（搜参后报告、日报、任何消费者）统一走此模块。
固定近9个月日历窗口，用 YAML 自带的 market_config 做基准对齐。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import yaml

logger = logging.getLogger(__name__)

DEFAULT_LOOKBACK = 273


@dataclass
class StrategyEvalReport:
    """统一策略评估报告。"""

    group: str = ""
    label: str = ""
    yaml_name: str = ""
    engine: str = ""
    total_return: float = 0.0
    excess_return: float = 0.0
    max_drawdown: float = 0.0
    sharpe: float = 0.0
    trade_count: int = 0
    position_pct: float = 0.0
    benchmark_returns: dict[str, float] = field(default_factory=dict)
    primary_benchmark: str = ""
    params: dict[str, float] = field(default_factory=dict)
    quarterly: list[dict] = field(default_factory=list)
    nav_series: list[float] = field(default_factory=list)
    nav_dates: list[str] = field(default_factory=list)
    sensitivity: dict | None = None
    volatility: dict | None = None
    candlestick_png: bytes | None = None
    weekly_ohlc: list[dict] | None = None

    def to_dict(self) -> dict:
        return {
            "group": self.group, "label": self.label,
            "yaml_name": self.yaml_name, "engine": self.engine,
            "total_return": self.total_return,
            "excess_return": self.excess_return,
            "dd": self.max_drawdown, "sharpe": self.sharpe,
            "trades": self.trade_count, "position": self.position_pct,
            "benchmark_returns": self.benchmark_returns,
            "primary_benchmark": self.primary_benchmark,
            "params": self.params, "quarterly": self.quarterly,
            "nav_series": self.nav_series, "nav_dates": self.nav_dates,
            "sensitivity": self.sensitivity, "volatility": self.volatility,
            "candlestick_png": self.candlestick_png,
            "weekly_ohlc": self.weekly_ohlc,
        }


def evaluate_yaml_strategy(
    yaml_path: str | Path,
    config: dict,
    stock_codes: list[str] | None = None,
    lookback_days: int = DEFAULT_LOOKBACK,
    with_sensitivity: bool = True,
    with_volatility: bool = True,
    with_candlestick: bool = False,
) -> StrategyEvalReport | None:
    """对 YAML 策略做固定窗口回测。"""
    import pandas as pd
    from src.analysis.percentile_engine import (
        PercentileSignalFn, _decode_tau, _decode_w, _decode_pos_frac,
    )
    from src.analysis.signal_scanner import _params_from_yaml
    from src.analysis.signal_functions import simulate_portfolio, Params as _Params
    from src.analysis.execution_config import get_execution_config
    from src.data.data_source import DataSource
    from src.analysis.portfolio_strategy import _detect_fine_group

    yaml_path = Path(yaml_path)
    if not yaml_path.exists():
        logger.error("YAML 文件不存在: %s", yaml_path)
        return None

    with open(yaml_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    top = (data.get("strategies") or [{}])[0]
    params_dict = top.get("params", {})
    engine_name = params_dict.get("_engine", "global")
    if engine_name not in ("percentile", "pct", "new"):
        return None

    exec_cfg = get_execution_config()
    group = data.get("group", "")
    mc = data.get("market_config", {}) or {}
    fx_rate = mc.get("fx_rate") or exec_cfg.fx_rates.get(group, 1.0)
    lot_size = mc.get("lot_size") or exec_cfg.lot_sizes.get(group, 100)
    commission = mc.get("commission_rate", exec_cfg.commission_rate)
    initial_capital = mc.get("initial_capital", exec_cfg.initial_capital)
    monthly_limit = mc.get("monthly_buy_limit", exec_cfg.monthly_buy_limit)
    benchmark_codes = list(mc.get("benchmark_codes") or [])
    risk_free_rate = mc.get("risk_free_rate", 0.02)

    if not benchmark_codes:
        try:
            with open("config/optimizer_constraints.yaml", "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            bm = raw.get("benchmarks", {}) or {}
            benchmark_codes = list(bm.get(group, bm.get("non_a_share", [])) or [])
        except Exception:
            pass

    if stock_codes is None:
        sc_str = params_dict.get("_stocks", "")
        stock_codes = [s.strip() for s in sc_str.split(",") if s.strip()]
    if not stock_codes:
        return None

    ds = DataSource(config)
    stocks_data: dict[str, pd.DataFrame] = {}
    for code in stock_codes:
        df = ds.fetch_stock_data(str(code), days=lookback_days)
        if df is not None and not df.empty:
            stocks_data[str(code)] = df
    if not stocks_data:
        return None

    sfn = PercentileSignalFn()
    ep = _params_from_yaml(params_dict)
    from src.analysis.indicator_library import compute_all
    computed = compute_all(stocks_data)

    per_code_bs, per_code_ss, per_code_pr = {}, {}, {}
    all_dates: set[str] = set()
    for c in stocks_data:
        df = computed.get(c, stocks_data[c]).copy()
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        df = df.sort_values("date").reset_index(drop=True)
        nb, ns = sfn.score_timeseries(ep, df)
        per_code_bs[c] = nb
        per_code_ss[c] = ns
        per_code_pr[c] = df["close"].astype(float).values
        all_dates.update(df["date"])

    dates_sorted = sorted(all_dates)
    didx = {d: i for i, d in enumerate(dates_sorted)}
    T = len(dates_sorted)
    N = len(stocks_data)
    buy_scores = np.zeros((T, N), dtype=np.float64)
    sell_scores = np.zeros((T, N), dtype=np.float64)
    price = np.full((T, N), np.nan)
    code_list = list(stocks_data.keys())
    for j, c in enumerate(code_list):
        for k in range(min(T, len(per_code_bs.get(c, [])))):
            buy_scores[k, j] = per_code_bs[c][k]
            sell_scores[k, j] = per_code_ss[c][k]
            price[k, j] = per_code_pr[c][k]
    for j in range(N):
        last = np.nan
        for ti in range(T):
            if np.isnan(price[ti, j]):
                price[ti, j] = last
            else:
                last = price[ti, j]
    price = np.nan_to_num(price, nan=0.0)

    main_fg = max(set(_detect_fine_group(str(c)) for c in code_list),
                  key=lambda g: sum(1 for c in code_list if _detect_fine_group(str(c)) == g))
    price_cny = price * fx_rate

    ex = sfn.execution_params(ep)
    trace = simulate_portfolio(
        buy_scores, sell_scores, price_cny,
        initial_capital, ex["buy_threshold"], ex["sell_threshold"],
        ex["position_frac"], lot_size, monthly_limit, commission,
        dates_sorted, code_list,
    )

    # 基准收益
    benchmark_returns: dict[str, float] = {}
    primary_bench = ""
    if benchmark_codes and dates_sorted:
        start_date = dates_sorted[0]
        end_date = dates_sorted[-1]
        for bcode in benchmark_codes:
            if bcode == "risk_free":
                n_days = T
                rf_ret = (1 + risk_free_rate) ** (n_days / 252.0) - 1
                benchmark_returns[bcode] = round(rf_ret * 100, 2)
                continue
            bdf = ds.fetch_stock_data(bcode, days=lookback_days + 30)
            if bdf is None or bdf.empty:
                continue
            bdf["date_str"] = pd.to_datetime(bdf["date"]).dt.strftime("%Y-%m-%d")
            bdf = bdf.set_index("date_str")
            all_bd = sorted(bdf.index)
            sc = [d for d in all_bd if d <= start_date]
            ec = [d for d in all_bd if d >= end_date]
            if not sc or not ec:
                continue
            px_s = float(bdf.loc[sc[-1], "close"])
            px_e = float(bdf.loc[ec[0], "close"])
            if px_s <= 0:
                continue
            br = (px_e - px_s) / px_s * 100
            benchmark_returns[bcode] = round(br, 2)
            if not primary_bench:
                primary_bench = bcode

    excess = trace.total_return_pct
    if benchmark_returns and primary_bench:
        excess = trace.total_return_pct - benchmark_returns[primary_bench]

    vals = getattr(ep, "values", ep)
    decoded: dict[str, float] = {}
    for lbl in ("adx_pct", "rsi_pct", "deviation_pct", "vol_ratio_pct", "ma200_dev_pct"):
        decoded[f"{lbl}_tau"] = round(_decode_tau(vals.get(f"{lbl}_tau", 5)), 2)
        decoded[f"{lbl}_w"] = round(_decode_w(vals.get(f"{lbl}_w", 2)), 2)
    decoded["tau_buy"] = round(_decode_tau(vals.get("buy_score_thresh", 5)), 2)
    decoded["tau_sell"] = round(_decode_tau(vals.get("sell_score_thresh", 5)), 2)
    decoded["pos_frac"] = round(_decode_pos_frac(vals.get("position_frac", 2)), 2)

    gl = {"a_share": "A股", "hk": "港股", "us": "美股"}
    report = StrategyEvalReport(
        group=group, label=gl.get(group, group),
        yaml_name=yaml_path.name, engine=engine_name,
        total_return=round(trace.total_return_pct, 2),
        excess_return=round(excess, 2),
        max_drawdown=round(trace.max_drawdown_pct, 2),
        sharpe=round(trace.sharpe_ratio, 2),
        trade_count=trace.total_trades,
        position_pct=round(trace.avg_position_pct, 0),
        benchmark_returns=benchmark_returns,
        primary_benchmark=primary_bench,
        params=decoded,
        quarterly=getattr(trace, "quarterly_holdings", []) or [],
        nav_series=[round(float(v), 2) for v in trace.daily_values],
        nav_dates=dates_sorted,
    )

    if with_sensitivity:
        copies = sfn.random_perturbations(ep, n=10)
        pert_rets = []
        for cp in copies:
            cp_ex = sfn.execution_params(_Params(values=cp, _engine="percentile"))
            tr = simulate_portfolio(
                buy_scores, sell_scores, price_cny,
                initial_capital, cp_ex["buy_threshold"], cp_ex["sell_threshold"],
                cp_ex["position_frac"], lot_size, monthly_limit, commission,
                dates_sorted, code_list,
            )
            pert_rets.append(round(tr.total_return_pct, 2))
        if pert_rets:
            br = round(trace.total_return_pct, 2)
            wr = min(pert_rets)
            report.sensitivity = {
                "worst_ret": wr, "drop_pct": round(br - wr, 2),
                "base_ret": br,
                "ret_range": [round(min(pert_rets), 2), round(max(pert_rets), 2)],
            }

    if with_volatility:
        report.volatility = sfn.cross_day_volatility(
            ep, buy_scores, sell_scores, price_cny,
            lookback_days=5, initial_cash=initial_capital, monthly_limit=monthly_limit,
        )

    if with_candlestick:
        from src.notification.chart_generator import (
            _build_weekly_ohlc, generate_candlestick_chart,
        )
        ohlc = _build_weekly_ohlc(report.nav_series, report.nav_dates)
        if ohlc:
            result = generate_candlestick_chart(ohlc)
            if result:
                _, png = result
                report.candlestick_png = png
                report.weekly_ohlc = ohlc

    return report
