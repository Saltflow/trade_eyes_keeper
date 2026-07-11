"""
信号扫描器

读取策略搜索优化器的最新结果，对当日数据评估最优策略信号，产出：
  1. 共识报告 — Top-5 策略的构建器使用频率 + 标的入选频率
  2. 策略告警 — 标的在共识入选池内 AND 命中共识买入信号
  3. 指标快照 — 所有标的当前值（仅共识指标列）

共识机制:
  - 构建器: 在 ≥2/5 策略中被引用 → 纳入监控列
  - 标的:   在 ≥3/5 策略中入选    → 纳入报警池
  - 报警:   标的在报警池 AND 今日命中共识买入规则
"""

import logging
import re
from collections import Counter
from pathlib import Path
from typing import Optional

import pandas as pd
from pydantic import BaseModel, Field

import numpy as np
import yaml

from .indicator_library import compute_all
from .rule_engine import ExpressionEngine

logger = logging.getLogger(__name__)

# 策略规则中可能引用的指标（用于解析）
_KNOWN_INDICATORS = {
    "rsi", "vol_ratio", "boll_pct_b", "boll_upper", "boll_lower",
    "adx", "macd_hist", "macd", "macd_signal", "atr",
    "deviation", "ma60", "close",
}

# 指标中文标头
_INDICATOR_LABELS = {
    "rsi": "RSI",
    "vol_ratio": "量比",
    "boll_pct_b": "布林%B",
    "adx": "ADX",
    "macd_hist": "MACD柱",
    "atr": "ATR",
    "deviation": "偏差%",
}


class ConsensusReport(BaseModel):
    """Top-5 策略共识统计"""

    buy_signal_counts: dict[str, int] = Field(default_factory=dict)
    sell_signal_counts: dict[str, int] = Field(default_factory=dict)
    stock_inclusion_counts: dict[str, int] = Field(default_factory=dict)
    consensus_buy_signals: list[str] = Field(default_factory=list)  # ≥2/5
    consensus_stocks: list[str] = Field(default_factory=list)  # ≥3/5
    consensus_indicators: list[str] = Field(default_factory=list)


class StrategyAlert(BaseModel):
    """单条策略报警"""

    stock_code: str
    rule_id: str
    rule_label: str
    condition_str: str
    current_value: str  # 当前指标值, e.g. "RSI=22.3"
    strategy_rank: int  # 用了哪个 rank 的策略
    type: str = "strategy_buy"


class ScanResult(BaseModel):
    """单次扫描结果"""

    group: str
    consensus: ConsensusReport
    alerts: list[StrategyAlert] = Field(default_factory=list)
    indicator_snapshot: dict[str, dict[str, float]] = Field(default_factory=dict)
    divergence_warnings: list[str] = Field(default_factory=list)


class SignalScanner:
    """信号扫描器"""

    def __init__(self, results_dir: str | Path = "data/optimizer"):
        self.results_dir = Path(results_dir)
        self._expr_engine = ExpressionEngine()

    # ── 文件加载 ──

    def _find_latest(self, group: str) -> Path | None:
        """找到最新的策略结果 YAML（精确匹配 group，排除 non_ 前缀混淆）"""
        import re
        candidates = sorted(
            self.results_dir.glob("*.yaml"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        pattern = re.compile(rf"(?<!non_){re.escape(group)}_strategies\.yaml$")
        for p in candidates:
            if pattern.search(p.name):
                return p
        return None

    def _load_strategies(self, group: str, top_n: int = 5) -> list[dict]:
        """加载 Top-N 策略（处理 numpy 标量）"""
        path = self._find_latest(group)
        if not path:
            return []

        with open(path, encoding="utf-8") as f:
            raw = f.read()
        # 清洗 numpy 标签（老文件可能有残留）
        raw = re.sub(r"!!python/object/apply:numpy\.core\.multiarray\.scalar\s*\n\s*- &id\d+.*?(?=\n\w)", "", raw, flags=re.DOTALL)
        raw = re.sub(r"\s*- !!binary \|[^\n]*\n\s+[A-Za-z0-9+/=\n]+", " 0.0", raw)

        try:
            data = yaml.safe_load(raw)
        except Exception as e:
            logger.warning(f"YAML 解析失败: {e}")
            return []

        strategies = data.get("strategies", [])[:top_n]
        return strategies

    # ── 共识计算 ──

    def _extract_indicators_from_condition(self, condition: str) -> set[str]:
        """从条件字符串提取引用的指标名"""
        found = set()
        for ind in _KNOWN_INDICATORS:
            if ind in condition:
                found.add(ind)
        return found

    def compute_consensus(self, strategies: list[dict]) -> ConsensusReport:
        """
        从 Top-N 策略计算共识。

        Args:
            strategies: 每个元素含 params (信号/仓位) 和 rules (条件)
        """
        buy_counts: Counter[str] = Counter()
        sell_counts: Counter[str] = Counter()
        stock_counts: Counter[str] = Counter()

        for s in strategies:
            params = s.get("params", {})
            # 买入信号
            for key in ("buy_1_signal", "buy_2_signal"):
                sig = params.get(key, "?")
                if sig and sig != "none":
                    buy_counts[sig] += 1
            # 卖出信号
            for key in ("sell_1_signal", "sell_2_signal", "sell_3_signal"):
                sig = params.get(key, "?")
                if sig and sig != "none":
                    sell_counts[sig] += 1
            # 入选标的
            stocks_str = params.get("_stocks", "")
            for part in stocks_str.replace("+", ",").split(","):
                code = part.strip()
                # 排除纯数字短码（"+N" 后缀），保留正常标的
                if code and not (code.isdigit() and len(code) <= 3):
                    stock_counts[code] += 1

        # 共识买入信号 (≥2/5)
        n = len(strategies)
        buy_min = max(2, n // 3 + 1)
        sell_min = max(2, n // 3 + 1)
        stock_min = max(3, n // 2 + 1)

        consensus_buy = [s for s, c in buy_counts.most_common() if c >= buy_min]
        consensus_sell = [s for s, c in sell_counts.most_common() if c >= sell_min]
        consensus_stocks = [s for s, c in stock_counts.most_common() if c >= stock_min]

        # 共识指标 = 买入信号引用的指标
        all_indicators: set[str] = set()
        for s in strategies:
            rules = s.get("rules", [])
            for r in rules:
                if r.get("type") == "buy":
                    all_indicators |= self._extract_indicators_from_condition(
                        r.get("condition", "")
                    )
        # 过滤：只保留 ≥2 个策略中出现的共识信号关联指标
        # 简化：直接用所有引用过的指标
        consensus_indicators = sorted(all_indicators & {
            "rsi", "vol_ratio", "boll_pct_b", "adx", "macd_hist", "deviation",
        })

        return ConsensusReport(
            buy_signal_counts=dict(buy_counts),
            sell_signal_counts=dict(sell_counts),
            stock_inclusion_counts=dict(stock_counts),
            consensus_buy_signals=consensus_buy,
            consensus_stocks=consensus_stocks,
            consensus_indicators=consensus_indicators,
        )

    # ── 扫描主逻辑 ──

    def scan(
        self,
        session,
        group: str,
        top_n: int = 5,
    ) -> ScanResult:
        """主入口：对当日数据进行策略信号扫描"""
        strategies = self._load_strategies(group, top_n)
        if not strategies:
            return ScanResult(group=group, consensus=ConsensusReport())

        consensus = self.compute_consensus(strategies)
        result = ScanResult(group=group, consensus=consensus)

        # 背离检测
        result.divergence_warnings = self._detect_divergence(strategies)

        # 获取历史数据和今日指标
        historical: dict[str, pd.DataFrame] = getattr(session, "_historical", {}) or {}
        stocks_data = session.stocks_data or []
        all_codes = self._get_stock_codes(stocks_data)
        # 按 group 过滤标的：a_share 用二分，hk/us 用细分组
        from .portfolio_strategy import (
            _detect_stock_group, _detect_fine_group, get_skip_signals,
        )
        # 跳过 skip_signals 标的（仅盯盘，不显示策略信号）
        skip_sig = get_skip_signals(getattr(session, "config", {}) or {})
        if group in ("hk", "us"):
            stock_codes = [c for c in all_codes
                           if _detect_fine_group(c) == group and c not in skip_sig]
        else:
            stock_codes = [c for c in all_codes
                           if _detect_stock_group(c) == group and c not in skip_sig]

        if not historical or not stock_codes:
            return result

        # 计算今日指标
        today_indicators = self._compute_today_indicators(historical, stock_codes)
        result.indicator_snapshot = today_indicators

        # 评估告警: 对当前会话所有标的（来自 config，非 YAML _stocks 快照）
        # 评估策略买入信号 — 删/加标的立刻生效
        for code in stock_codes:
            if code not in today_indicators:
                continue
            today = today_indicators[code]
            # 去重按 (标的, 条件表达式)：相同条件只报一次，
            # 不管来自哪个策略/rule_id（Top5 常有多策略同条件）
            seen_conds: set[tuple[str, str]] = set()
            for s_idx, strat in enumerate(strategies):
                rules = strat.get("rules", [])
                for r in rules:
                    if r.get("type") != "buy" or r.get("condition", "False") == "False":
                        continue
                    cond = r.get("condition", "False")
                    dedup_key = (code, cond)
                    if dedup_key in seen_conds:
                        continue
                    seen_conds.add(dedup_key)
                    # 构建上下文
                    ctx = self._build_context(today, historical.get(code), code)
                    try:
                        if self._expr_engine.evaluate(cond, ctx):
                            cur_val = self._describe_current(today, cond)
                            result.alerts.append(
                                StrategyAlert(
                                    stock_code=code,
                                    rule_id=r.get("id", "?"),
                                    rule_label=r.get("label", r.get("id", "?")),
                                    condition_str=cond,
                                    current_value=cur_val,
                                    strategy_rank=s_idx + 1,
                                )
                            )
                    except Exception as e:
                        logger.debug(f"{code} 规则 {r.get('id','?')} 评估失败 (非致命): {e}")

        return result

    # ── 辅助方法 ──

    def _get_stock_codes(self, stocks_data) -> list[str]:
        """从 session.stocks_data 提取股票代码列表"""
        codes = []
        if isinstance(stocks_data, pd.DataFrame):
            col = stocks_data.get("stock_code")
            if col is not None:
                codes = [str(c) for c in col.tolist() if pd.notna(c)]
        elif isinstance(stocks_data, (list, dict)):
            items = stocks_data.values() if isinstance(stocks_data, dict) else stocks_data
            for item in items:
                # StockPriceData (Pydantic) → .stock_code
                code = getattr(item, "stock_code", None)
                if code is None and isinstance(item, dict):
                    code = item.get("stock_code")
                if code:
                    codes.append(str(code))
        return [c for c in codes if c]

    def _compute_today_indicators(
        self,
        historical: dict[str, pd.DataFrame],
        stock_codes: list[str],
    ) -> dict[str, dict[str, float]]:
        """用历史数据计算指标，返回每只股票今日值"""
        result: dict[str, dict[str, float]] = {}
        subset = {c: df for c, df in historical.items() if c in stock_codes}
        if not subset:
            return result

        try:
            computed = compute_all(subset)
        except Exception as e:
            logger.warning(f"指标计算失败: {e}")
            return result

        for code, df in computed.items():
            if df is None or df.empty:
                continue
            # 确保有 deviation 列
            if "deviation" not in df.columns and "close" in df.columns:
                df["ma60"] = df["close"].rolling(60, min_periods=1).mean()
                df["deviation"] = (df["close"] - df["ma60"]) / df["ma60"]

            row = df.iloc[-1]
            vals: dict[str, float] = {}
            for col in df.columns:
                if col in ("date", "open", "high", "low", "volume", "amount",
                           "stock_code", "stock_name", "amplitude", "change_pct",
                           "change", "turnover"):
                    continue
                v = row.get(col)
                if v is not None and not (isinstance(v, float) and pd.isna(v)):
                    try:
                        vals[col] = float(v)
                    except (ValueError, TypeError):
                        pass  # skip non-numeric columns
            # RSI 默认 50（如果缺失）
            if "rsi" not in vals:
                vals["rsi"] = 50.0
            result[code] = vals

        return result

    def _build_context(
        self,
        today: dict[str, float],
        hist_df: pd.DataFrame | None,
        stock_code: str,
    ) -> dict:
        """构建 ExpressionEngine 评估所需的上下文"""
        ctx = {
            "close": today.get("close", 0),
            "ma60": today.get("ma60", 0),
            "deviation": today.get("deviation", 0),
            "rsi": today.get("rsi", 50),
            "vol_ratio": today.get("vol_ratio", 1.0),
            "boll_pct_b": today.get("boll_pct_b", 0.5),
            "adx": today.get("adx", 20),
            "macd_hist": today.get("macd_hist", 0),
            "atr": today.get("atr", 0),
            "shares": 0,
            "cash": 0,
            "position_value": 0,
        }
        # prev_deviation: 取倒数第二天的 deviation
        if hist_df is not None and len(hist_df) >= 2:
            if "deviation" not in hist_df.columns and "close" in hist_df.columns:
                hist_df = hist_df.copy()
                hist_df["ma60"] = hist_df["close"].rolling(60, min_periods=1).mean()
                hist_df["deviation"] = (
                    hist_df["close"] - hist_df["ma60"]
                ) / hist_df["ma60"]
            prev = hist_df.iloc[-2].get("deviation")
            if prev is not None and not (isinstance(prev, float) and pd.isna(prev)):
                ctx["prev_deviation"] = float(prev)
            else:
                ctx["prev_deviation"] = None
        else:
            ctx["prev_deviation"] = None

        return ctx

    def _describe_current(self, today: dict[str, float], condition: str) -> str:
        """描述当前指标值"""
        parts = []
        for ind, label in _INDICATOR_LABELS.items():
            if ind in condition and ind in today:
                val = today[ind]
                if ind == "deviation":
                    parts.append(f"{label}={val * 100:.1f}%")
                else:
                    parts.append(f"{label}={val:.1f}")
        return ", ".join(parts) if parts else "—"

    def _detect_divergence(self, strategies: list[dict]) -> list[str]:
        """检测训练-测试背离"""
        warnings = []
        for s in strategies:
            train = s.get("train_return", 0) or 0
            test = s.get("test_return", 0) or 0
            divergence = abs(train - test)
            if divergence > 15:
                rank = s.get("rank", "?")
                warnings.append(
                    f"Rank {rank}: 背离 {divergence:.0f}% (训练 {train:+.1f}% vs 测试 {test:+.1f}%)"
                )
        return warnings

    # ── 回测 ──

    def run_backtest(self, session, group: str) -> dict | None:
        """
        用最新优化策略跑完整历史回测。

        Returns:
            {
                "strategy_rank": int,
                "report_id": str,
                "total_return": float, "max_drawdown": float, "sharpe": float,
                "trade_count": int, "stocks": list[str],
                "phase_metrics": dict,  # {observe/deploy/test: SubPeriodMetrics}
                "rules": list[dict],
                "benchmarks": dict,  # {name: test_excess}
            }
            如果没有任何优化结果或数据，返回 None。
        """
        from .portfolio_strategy import PortfolioEvaluator, PortfolioResult
        from .backtest_config import (
            make_default_optimizer_config, BacktestConfig,
        )
        from .rule_engine import Rule
        from .indicator_library import compute_all

        # 加载 Top-1 策略
        strategies = self._load_strategies(group, top_n=1)
        if not strategies:
            return None
        top = strategies[0]

        # 获取历史数据
        historical: dict = getattr(session, "_historical", {}) or {}
        stocks = list(historical.keys())
        if not stocks:
            return None

        # 选取入选标的
        stocks_str = top.get("params", {}).get("_stocks", "")
        selected = []
        for part in stocks_str.replace("+", ",").split(","):
            code = part.strip()
            if code and not (code.isdigit() and len(code) <= 3):
                selected.append(code)
        if not selected:
            # fallback: 取同组前 10 只，不分组的 session 取全部前 10
            if group == "a_share":
                group_stocks = [s for s in stocks
                                if s.isdigit() or s.replace(".", "").isdigit()]
            else:
                group_stocks = [s for s in stocks
                                if not (s.isdigit() or s.replace(".", "").isdigit())]
            selected = group_stocks[:10] if group_stocks else stocks[:10]

        # 构建 Rule 对象
        rules = []
        for r_dict in top.get("rules", []):
            try:
                rule = Rule(
                    id=r_dict.get("id", "?"),
                    label=r_dict.get("label", r_dict.get("id", "?")),
                    type=r_dict.get("type", "buy"),
                    priority=r_dict.get("priority", 1),
                    condition=r_dict.get("condition", "False"),
                    budget_pool=r_dict.get("budget_pool", r_dict.get("type", "buy")),
                    action_amount=r_dict.get("action_amount"),
                    action_fraction=r_dict.get("action_fraction"),
                    action_min=r_dict.get("action_min"),
                    action_max=r_dict.get("action_max"),
                    reset_when=r_dict.get("reset_when"),
                )
                rules.append(rule)
            except Exception as e:
                logger.warning(f"规则 {r_dict.get('id','?')} 解析失败: {e}")

        if not rules:
            return None

        # 准备数据
        stocks_data = {c: df for c, df in historical.items() if c in selected}
        if not stocks_data:
            return None

        # 计算指标
        try:
            indicators = compute_all(stocks_data)
        except Exception as e:
            logger.warning(f"回测指标计算失败: {e}")
            indicators = None

        # 运行回测
        evaluator = PortfolioEvaluator(stocks_data, group)
        evaluator.rules = rules
        config = make_default_optimizer_config()

        result = evaluator.evaluate(
            list(stocks_data.keys()),
            backtest_config=config,
            indicators_data=indicators,
        )

        # 基准计算
        benchmarks = self._compute_benchmarks(selected, config)

        return {
            "strategy_rank": top.get("rank", 1),
            "report_id": top.get("report_id", ""),
            "total_return": result.total_return,
            "max_drawdown": result.max_drawdown,
            "sharpe": result.sharpe_ratio,
            "trade_count": result.trade_count,
            "stocks": list(stocks_data.keys()),
            "phase_metrics": result.sub_periods,
            "rules": top.get("rules", []),
            "benchmarks": benchmarks,
        }

    def _compute_benchmarks(
        self, stock_codes: list[str], config
    ) -> dict[str, float]:
        """计算基准 ETF 买持超额收益: (末价 - 初价)/初价 - 无风险收益。

        不做时间线约束 — 买持就是从第一天拿到最后一天。
        """
        results: dict[str, float] = {}
        bench_data = getattr(self, "benchmark_data", {}) or {}
        if not bench_data:
            return results

        from .backtest_config import BacktestConfig
        bcfg = config if isinstance(config, BacktestConfig) else BacktestConfig()
        rf_rate = bcfg.rf_rate

        for name, df in bench_data.items():
            if df is None or df.empty or len(df) < 2:
                results[name] = 0.0
                continue
            try:
                close = df["close"] if "close" in df.columns else df.iloc[:, 0]
                start_p = float(close.iloc[0])
                end_p = float(close.iloc[-1])
                if start_p <= 0:
                    results[name] = 0.0
                    continue
                total_ret = (end_p - start_p) / start_p * 100.0
                # 扣除无风险收益: rf_rate * years
                days = len(df)
                rf_cost = rf_rate * days / 365.0
                excess = round(total_ret - rf_cost, 2)
                results[name] = excess
            except Exception as e:
                logger.warning(f"基准 {name} 计算失败: {e}")
                results[name] = 0.0
        return results
