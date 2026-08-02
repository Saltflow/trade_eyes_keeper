"""参考持仓管理模块。

持久化参考持仓（Ref Portfolio），用于日报/简报展示系统持续运行的仓位状态。
- 只在简报时间（09:50 / 14:30）按量化信号调仓
- 禁止盘后交易（美股 24h 除外，但周末休市不交易）
- 仅手动 reset 时重置，否则永远接上一期仓位

数据持久化到 data/ref_portfolio.yaml。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import numpy as np
import yaml

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────────
DEFAULT_INITIAL_CAPITAL = 100000.0
DEFAULT_COMMISSION_RATE = 0.005
DEFAULT_BUY_AMOUNT = 5000.0  # 每次买入最大金额（已废弃）
BUY_CASH_FRACTION = 0.20  # 每次买入占现金的比例
MAX_BUY_AMOUNT = 50000.0  # 单次买入金额上限
REF_MONTHLY_LIMIT = 50000.0  # 参考持仓月度买入上限（比搜参宽松）
DEFAULT_SELL_FRACTION = 0.25  # 每次卖出最大比例
BRIEF_WINDOWS = ("09:50", "14:30")  # 允许调仓的时间窗口
DATA_DIR = Path("data")
PORTFOLIO_FILE = DATA_DIR / "ref_portfolio.yaml"


def reference_execution_contract(
    strategy_execution: dict,
    execution_config,
    market_group: str,
) -> dict:
    """Return the complete execution contract pinned by a manual reset."""
    return {
        "strategy_execution": dict(strategy_execution),
        "initial_capital": float(execution_config.initial_capital),
        "commission_rate": float(execution_config.commission_rate),
        "min_holding_days": int(execution_config.min_holding_days),
        "lot_size": int(execution_config.lot_sizes.get(market_group, 100)),
        "fx_rate": float(execution_config.fx_rates.get(market_group, 1.0)),
    }


# ── 数据模型 ──────────────────────────────────────────────────


@dataclass
class Holding:
    """单笔持仓"""

    code: str
    shares: int
    avg_cost: float  # 每股平均成交价；手续费单独计入现金和 Trade
    last_buy_date: str = ""


@dataclass
class Trade:
    """单笔交易记录"""

    date: str  # YYYY-MM-DD
    code: str
    action: str  # "buy" / "sell"
    shares: int
    price: float  # 成交单价
    cost: float  # 总金额（买入为正，卖出为负）
    reason: str  # 触发信号 rule_id
    commission: float = 0.0
    event_id: str = ""
    run_id: str = ""
    strategy_id: str = ""


@dataclass
class RefPortfolio:
    """参考持仓完整状态"""

    inception_date: str = ""  # 期初日期 YYYY-MM-DD
    cash: float = DEFAULT_INITIAL_CAPITAL
    initial_capital: float = DEFAULT_INITIAL_CAPITAL
    trading_days: int = 0  # 有交易的交易日数
    last_rebalance_date: str = ""  # 最近一次调仓日期
    last_reset_date: str = ""  # 最近一次手动重置日期
    holdings: dict[str, Holding] = field(default_factory=dict)
    trade_log: list[Trade] = field(default_factory=list)
    processed_events: list[str] = field(default_factory=list)
    market_group: str = ""
    strategy_run_id: str = ""
    strategy_id: str = ""
    strategy_timestamp: str = ""
    params_hash: str = ""
    execution_hash: str = ""

    @property
    def is_bound(self) -> bool:
        return bool(
            self.market_group
            and self.strategy_run_id
            and self.strategy_run_id != "legacy"
            and self.strategy_id
        )

    def total_market_value(self, prices: dict[str, float]) -> float:
        """当前持仓市值。"""
        total = 0.0
        for code, h in self.holdings.items():
            if code in prices and prices[code] > 0:
                total += h.shares * prices[code]
        return total

    def nav(self, prices: dict[str, float]) -> float:
        """当前净值 = 现金 + 持仓市值。"""
        return self.cash + self.total_market_value(prices)

    def nav_return_pct(self, prices: dict[str, float]) -> float | None:
        """净值回报率（相对 initial_capital）。"""
        if self.initial_capital <= 0:
            return None
        return (self.nav(prices) / self.initial_capital - 1.0) * 100.0

    def to_dict(self) -> dict:
        """序列化为纯 Python dict（供 YAML 持久化）。"""
        return {
            "inception_date": self.inception_date,
            "cash": round(self.cash, 2),
            "initial_capital": self.initial_capital,
            "trading_days": self.trading_days,
            "last_rebalance_date": self.last_rebalance_date,
            "last_reset_date": self.last_reset_date,
            "market_group": self.market_group,
            "strategy_run_id": self.strategy_run_id,
            "strategy_id": self.strategy_id,
            "strategy_timestamp": self.strategy_timestamp,
            "params_hash": self.params_hash,
            "execution_hash": self.execution_hash,
            "processed_events": list(dict.fromkeys(self.processed_events)),
            "holdings": {
                code: {
                    "shares": h.shares,
                    "avg_cost": round(h.avg_cost, 4),
                    "last_buy_date": h.last_buy_date,
                }
                for code, h in self.holdings.items()
            },
            "trade_log": [
                {
                    "date": t.date,
                    "code": t.code,
                    "action": t.action,
                    "shares": t.shares,
                    "price": round(t.price, 4),
                    "cost": round(t.cost, 2),
                    "reason": t.reason,
                    "commission": round(t.commission, 4),
                    "event_id": t.event_id,
                    "run_id": t.run_id,
                    "strategy_id": t.strategy_id,
                }
                for t in self.trade_log
            ],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RefPortfolio":
        """从 dict 反序列化。"""
        pf = cls(
            inception_date=d.get("inception_date", ""),
            cash=d.get("cash", DEFAULT_INITIAL_CAPITAL),
            initial_capital=d.get("initial_capital", DEFAULT_INITIAL_CAPITAL),
            trading_days=d.get("trading_days", 0),
            last_rebalance_date=d.get("last_rebalance_date", ""),
            last_reset_date=d.get("last_reset_date", ""),
            processed_events=list(d.get("processed_events") or []),
            market_group=str(d.get("market_group", "")),
            strategy_run_id=str(d.get("strategy_run_id", "")),
            strategy_id=str(d.get("strategy_id", "")),
            strategy_timestamp=str(d.get("strategy_timestamp", "")),
            params_hash=str(d.get("params_hash", "")),
            execution_hash=str(d.get("execution_hash", "")),
        )
        for code, hd in (d.get("holdings") or {}).items():
            pf.holdings[code] = Holding(
                code=code,
                shares=hd["shares"],
                avg_cost=hd["avg_cost"],
                last_buy_date=str(hd.get("last_buy_date", "")),
            )
        for td in d.get("trade_log") or []:
            pf.trade_log.append(
                Trade(
                    date=td["date"],
                    code=td["code"],
                    action=td["action"],
                    shares=td["shares"],
                    price=td["price"],
                    cost=td["cost"],
                    reason=td.get("reason", ""),
                    commission=td.get("commission", 0.0),
                    event_id=td.get("event_id", ""),
                    run_id=td.get("run_id", ""),
                    strategy_id=td.get("strategy_id", ""),
                )
            )
        return pf


# ── 管理器 ────────────────────────────────────────────────────


class RefPortfolioManager:
    """参考持仓管理器：加载/保存/重置/调仓/Nav 计算。"""

    def __init__(self, file_path: Path | str | None = None):
        self._file = Path(file_path) if file_path else PORTFOLIO_FILE

    # ── 持久化 ──

    def load(self) -> RefPortfolio:
        """加载参考持仓。文件不存在时返回空持仓（未初始化状态）。"""
        if not self._file.exists():
            logger.info("参考持仓文件不存在，返回空持仓")
            return RefPortfolio()
        try:
            raw = self._file.read_text(encoding="utf-8")
            if not raw.strip():
                return RefPortfolio()
            data = yaml.safe_load(raw) or {}
            pf_data = data.get("ref_portfolio", {}) or {}
            return RefPortfolio.from_dict(pf_data)
        except Exception as e:
            logger.warning(f"加载参考持仓失败: {e}，返回空持仓")
            return RefPortfolio()

    def save(self, pf: RefPortfolio):
        """保存参考持仓到 YAML。"""
        self._file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._file.with_suffix(".yaml.tmp")
        try:
            data = {"ref_portfolio": pf.to_dict()}
            tmp.write_text(
                yaml.dump(
                    data, allow_unicode=True, default_flow_style=False, sort_keys=False
                ),
                encoding="utf-8",
            )
            tmp.replace(self._file)
            logger.debug(f"参考持仓已保存: {self._file}")
        except Exception as e:
            logger.error(f"保存参考持仓失败: {e}")
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError as cleanup_error:
                    logger.warning(
                        "Failed to remove temporary reference portfolio %s: %s",
                        tmp,
                        cleanup_error,
                    )

    # ── 重置 ──

    def reset(
        self,
        initial_capital: float = DEFAULT_INITIAL_CAPITAL,
        inception_date: str | None = None,
        *,
        market_group: str = "",
        strategy_run_id: str = "",
        strategy_id: str = "",
        strategy_timestamp: str = "",
        params_hash: str = "",
        execution_hash: str = "",
    ) -> RefPortfolio:
        """重置参考持仓：清空标的、恢复初始现金、设置期初日期。

        Args:
            initial_capital: 初始资金
            inception_date: 期初日期 YYYY-MM-DD，None 则用今天

        Returns:
            重置后的空 RefPortfolio（已自动保存）
        """
        if inception_date is None:
            inception_date = date.today().strftime("%Y-%m-%d")
        now_str = date.today().strftime("%Y-%m-%d")
        pf = RefPortfolio(
            inception_date=inception_date,
            cash=initial_capital,
            initial_capital=initial_capital,
            trading_days=0,
            last_reset_date=now_str,
            market_group=market_group,
            strategy_run_id=strategy_run_id,
            strategy_id=strategy_id,
            strategy_timestamp=strategy_timestamp,
            params_hash=params_hash,
            execution_hash=execution_hash,
        )
        self.save(pf)
        logger.info(
            f"参考持仓已重置: 初始资金={initial_capital}, 期初={inception_date}, "
            f"市场={market_group}, 运行={strategy_run_id}"
        )
        return pf

    # ── 调仓 ──

    @staticmethod
    def _alert_value(alert, key: str, default=""):
        if isinstance(alert, dict):
            return alert.get(key, default)
        return getattr(alert, key, default)

    def rebalance(
        self,
        pf: RefPortfolio,
        alerts: list,
        prices: dict[str, float],
        trade_date: str,
        monthly_buy_limit: float = REF_MONTHLY_LIMIT,
        commission_rate: float = DEFAULT_COMMISSION_RATE,
        lot_size: int = 100,
        fx_rate: float = 1.0,
        label: str = "",
        force: bool = False,
    ) -> tuple[RefPortfolio, list[Trade]]:
        """根据策略信号和当前价格调仓。

        Args:
            pf: 当前参考持仓
            alerts: StrategyAlert 列表（来自 SignalScanner）
            prices: {stock_code: current_price} 现价表（原始货币）
            trade_date: 调仓日期 YYYY-MM-DD
            lot_size: 每手股数（A股=100, 美股=1）
            commission_rate: 手续费率
            monthly_buy_limit: 当日买入上限（CNY）
            fx_rate: 汇率乘数（A股=1.0, 港股=0.9, 美股=7.0）
            label: 分组标识（用于日志）

        Returns:
            (更新后的 RefPortfolio, 本次产生的 Trade 列表)
        """
        tag = f"[{label}]" if label else ""
        logger.info(
            f"参考持仓{tag} 调仓开始: {len(alerts)} 条信号, "
            f"cash={pf.cash:,.0f}, 持仓={len(pf.holdings)}只, "
            f"lot={lot_size}, fx={fx_rate}"
        )

        # ── 前置校验：周末不交易（force 模式跳过）──
        try:
            dt = datetime.strptime(trade_date, "%Y-%m-%d")
            if not force and dt.weekday() >= 5:
                logger.info(f"参考持仓{tag} 跳过调仓: {trade_date} 是周末")
                return pf, []
        except ValueError:
            logger.warning(f"参考持仓{tag} 无法解析日期 {trade_date}")
            return pf, []

        # ── 分类信号 ──
        buy_signals = []  # (stock_code, rule_id)
        sell_signals = []  # (stock_code, rule_id)
        skipped_alerts = 0

        for alert in alerts:
            code = str(self._alert_value(alert, "stock_code", ""))
            rid = str(self._alert_value(alert, "rule_id", ""))
            side = str(self._alert_value(alert, "side", "")).lower()
            rtype = str(self._alert_value(alert, "type", ""))
            rlabel = str(
                self._alert_value(
                    alert,
                    "rule_label",
                    self._alert_value(alert, "label", ""),
                )
            )
            if not code:
                skipped_alerts += 1
                continue

            is_buy = (
                side == "buy"
                or rtype == "strategy_buy"
                or "buy" in rid.lower()
            )
            is_sell = (
                side == "sell"
                or rtype == "strategy_sell"
                or "sell" in rid.lower()
            )

            if is_buy and not is_sell:
                buy_signals.append((code, rid))
                logger.debug(
                    f"参考持仓{tag} 买入信号: {code} | {rlabel[:30]} | "
                    f"type={rtype} rid={rid}"
                )
            elif is_sell and not is_buy:
                sell_signals.append((code, rid))
                logger.debug(
                    f"参考持仓{tag} 卖出信号: {code} | {rlabel[:30]} | "
                    f"type={rtype} rid={rid}"
                )
            else:
                skipped_alerts += 1
                logger.debug(
                    f"参考持仓{tag} 跳过模糊信号: {code} "
                    f"is_buy={is_buy} is_sell={is_sell} type={rtype} rid={rid}"
                )

        logger.info(
            f"参考持仓{tag} 信号分类: 买{buy_signals} 卖{sell_signals} "
            f"跳过{skipped_alerts}"
        )

        # ── 同日互斥 ──
        buy_codes = {c for c, _ in buy_signals}
        sell_codes = {c for c, _ in sell_signals}
        conflict_codes = buy_codes & sell_codes
        if conflict_codes:
            logger.info(f"参考持仓{tag} 同日互斥取消: {conflict_codes}")
            buy_signals = [(c, r) for c, r in buy_signals if c not in conflict_codes]
            sell_signals = [(c, r) for c, r in sell_signals if c not in conflict_codes]

        trades: list[Trade] = []
        new_pf = RefPortfolio(
            inception_date=pf.inception_date,
            cash=pf.cash,
            initial_capital=pf.initial_capital,
            trading_days=pf.trading_days,
            last_rebalance_date=pf.last_rebalance_date,
            last_reset_date=pf.last_reset_date,
            processed_events=list(pf.processed_events),
            market_group=pf.market_group,
            strategy_run_id=pf.strategy_run_id,
            strategy_id=pf.strategy_id,
            strategy_timestamp=pf.strategy_timestamp,
            params_hash=pf.params_hash,
            execution_hash=pf.execution_hash,
            holdings={
                k: Holding(v.code, v.shares, v.avg_cost, v.last_buy_date)
                for k, v in pf.holdings.items()
            },
            trade_log=list(pf.trade_log),
        )
        day_buy_total = 0.0

        # ── 1. 先执行卖出（释放现金）──
        for code, rid in sell_signals:
            h = new_pf.holdings.get(code)
            if not h or h.shares <= 0:
                logger.debug(f"参考持仓{tag} 卖出跳过 {code}: 无持仓")
                continue
            raw_price = prices.get(code, 0)
            if raw_price <= 0:
                logger.debug(f"参考持仓{tag} 卖出跳过 {code}: 无价格")
                continue
            price = raw_price * fx_rate  # → CNY

            raw_shares = int(h.shares * DEFAULT_SELL_FRACTION)
            sell_shares = (raw_shares // lot_size) * lot_size
            if sell_shares <= 0:
                logger.debug(
                    f"参考持仓{tag} 卖出跳过 {code}: "
                    f"不足一手 (持仓{h.shares}, 25%={raw_shares}, lot={lot_size})"
                )
                continue

            gross = sell_shares * price
            commission = gross * commission_rate
            net = gross - commission

            h.shares -= sell_shares
            new_pf.cash += net

            if h.shares <= 0:
                del new_pf.holdings[code]

            trade = Trade(
                date=trade_date,
                code=code,
                action="sell",
                shares=sell_shares,
                price=round(price, 4),
                cost=-gross,
                reason=rid,
                commission=commission,
            )
            trades.append(trade)
            new_pf.trade_log.append(trade)
            logger.info(
                f"参考持仓{tag} 卖出: {code} {sell_shares}股 "
                f"@CNY{price:.2f} 净收入 {net:.2f}"
            )

        # ── 2. 再执行买入 ──
        for code, rid in buy_signals:
            raw_price = prices.get(code, 0)
            if raw_price <= 0:
                logger.debug(f"参考持仓{tag} 买入跳过 {code}: 无价格")
                continue
            price = raw_price * fx_rate  # → CNY
            price_cny = price

            max_amount = min(
                new_pf.cash * BUY_CASH_FRACTION, MAX_BUY_AMOUNT, new_pf.cash
            )
            if max_amount <= 0:
                logger.debug(f"参考持仓{tag} 买入跳过 {code}: 现金不足")
                continue

            raw_shares = int(max_amount / price_cny)
            buy_shares = (raw_shares // lot_size) * lot_size
            if buy_shares <= 0:
                logger.debug(
                    f"参考持仓{tag} 买入跳过 {code}: "
                    f"不足一手 (金额{max_amount:.0f}, 价CNY{price_cny:.2f}, "
                    f"算得{raw_shares}股, lot={lot_size})"
                )
                continue

            gross = buy_shares * price_cny
            commission = gross * commission_rate
            total_cost = gross + commission

            if total_cost > new_pf.cash:
                buy_shares = max(0, buy_shares - lot_size)
                if buy_shares <= 0:
                    logger.debug(
                        f"参考持仓{tag} 买入跳过 {code}: "
                        f"减一手后仍不足 (cost={total_cost:.0f} > cash={new_pf.cash:.0f})"
                    )
                    continue
                gross = buy_shares * price_cny
                commission = gross * commission_rate
                total_cost = gross + commission

            if total_cost > new_pf.cash:
                logger.debug(
                    f"参考持仓{tag} 买入跳过 {code}: "
                    f"资金不足 (cost={total_cost:.0f} > cash={new_pf.cash:.0f})"
                )
                continue

            if day_buy_total + gross > monthly_buy_limit:
                logger.info(
                    f"参考持仓{tag} 买入跳过 {code}: 当日买入超限 "
                    f"({day_buy_total:.0f}+{gross:.0f}>{monthly_buy_limit})"
                )
                continue

            new_pf.cash -= total_cost
            day_buy_total += gross

            if code in new_pf.holdings:
                h = new_pf.holdings[code]
                total_cost_basis = h.shares * h.avg_cost + total_cost
                h.shares += buy_shares
                h.avg_cost = total_cost_basis / h.shares if h.shares > 0 else price_cny
                h.last_buy_date = trade_date
            else:
                new_pf.holdings[code] = Holding(
                    code=code,
                    shares=buy_shares,
                    avg_cost=price_cny,
                    last_buy_date=trade_date,
                )

            trade = Trade(
                date=trade_date,
                code=code,
                action="buy",
                shares=buy_shares,
                price=round(price_cny, 4),
                cost=gross,
                reason=rid,
                commission=commission,
            )
            trades.append(trade)
            new_pf.trade_log.append(trade)
            logger.info(
                f"参考持仓{tag} 买入: {code} {buy_shares}股 "
                f"@CNY{price_cny:.2f} 成本 {total_cost:.2f}"
            )

        # ── 更新统计 ──
        if trades:
            new_pf.last_rebalance_date = trade_date
            new_pf.trading_days = pf.trading_days + (
                1 if (pf.last_rebalance_date or "")[:10] != trade_date[:10] else 0
            )

        logger.info(
            f"参考持仓{tag} 调仓结束: {len(trades)}笔交易, "
            f"持仓{len(new_pf.holdings)}只, cash={new_pf.cash:,.0f}"
        )
        return new_pf, trades

    @staticmethod
    def _copy_portfolio(pf: RefPortfolio) -> RefPortfolio:
        return RefPortfolio(
            inception_date=pf.inception_date,
            cash=pf.cash,
            initial_capital=pf.initial_capital,
            trading_days=pf.trading_days,
            last_rebalance_date=pf.last_rebalance_date,
            last_reset_date=pf.last_reset_date,
            holdings={
                code: Holding(
                    holding.code,
                    holding.shares,
                    holding.avg_cost,
                    holding.last_buy_date,
                )
                for code, holding in pf.holdings.items()
            },
            trade_log=list(pf.trade_log),
            processed_events=list(pf.processed_events),
            market_group=pf.market_group,
            strategy_run_id=pf.strategy_run_id,
            strategy_id=pf.strategy_id,
            strategy_timestamp=pf.strategy_timestamp,
            params_hash=pf.params_hash,
            execution_hash=pf.execution_hash,
        )

    @staticmethod
    def _rebalance_target_plan(
        new_pf,
        trade_plan,
        row,
        plan_date,
        symbols,
        prices,
        highs,
        lows,
        tradable,
        processed,
        event_id,
        sell,
        buy,
        lot,
        fee_rate,
        fx_rate,
        min_holding_days,
    ) -> None:
        """Mirror the Backtester target-weight transition for one live row."""
        entries = np.asarray(
            trade_plan.entry_events
            if trade_plan.entry_events is not None
            else trade_plan.buy_signals,
            dtype=bool,
        )
        exits = np.asarray(
            trade_plan.exit_events
            if trade_plan.exit_events is not None
            else trade_plan.sell_signals,
            dtype=bool,
        )
        force_exits = np.asarray(
            trade_plan.force_exit_signals
            if trade_plan.force_exit_signals is not None
            else np.zeros_like(exits),
            dtype=bool,
        )
        conviction = np.asarray(
            trade_plan.conviction
            if trade_plan.conviction is not None
            else trade_plan.buy_priority,
            dtype=float,
        )
        columns = len(symbols)
        active = np.zeros(columns, dtype=bool)
        active_score = np.zeros(columns, dtype=float)

        # Reconstruct strategy state only from rows before today's event.  The
        # plan is sliced at the portfolio inception date by the caller, so a
        # manual reset never inherits a training-period position.
        for history_row in range(row):
            leave = exits[history_row] | force_exits[history_row]
            active[leave] = False
            active_score[leave] = 0.0
            enter = entries[history_row] & ~active
            active[enter] = True
            active_score[enter] = np.maximum(
                conviction[history_row, enter],
                0.000001,
            )

        min_calendar_days = int(
            trade_plan.execution.get(
                "min_holding_calendar_days",
                min_holding_days,
            )
        )
        state_changed = False

        # Exits precede entries, exactly as in the unified simulator.
        for column in range(columns):
            forced = bool(force_exits[row, column])
            ordinary = bool(exits[row, column])
            if not forced and not ordinary:
                continue
            eid = event_id(column, "force_sell" if forced else "sell")
            if eid in processed:
                continue
            processed.add(eid)
            holding = new_pf.holdings.get(symbols[column])
            held_days = 10**9
            if holding is not None and holding.last_buy_date:
                try:
                    held_days = (
                        datetime.strptime(plan_date, "%Y-%m-%d")
                        - datetime.strptime(holding.last_buy_date, "%Y-%m-%d")
                    ).days
                except ValueError:
                    held_days = 10**9
            if not forced and held_days < min_calendar_days:
                continue
            if active[column]:
                active[column] = False
                active_score[column] = 0.0
                state_changed = True
            if holding is not None and holding.shares > 0:
                quantity = int(holding.shares / lot) * lot
                if quantity <= 0:
                    quantity = holding.shares
                sell(
                    column,
                    quantity,
                    "trade_plan_force_exit" if forced else "trade_plan_exit",
                    eid,
                )

        for column in range(columns):
            if not entries[row, column] or active[column]:
                continue
            eid = event_id(column, "buy")
            if eid in processed:
                continue
            processed.add(eid)
            raw_price = float(highs[row, column])
            if (
                not tradable[row, column]
                or not np.isfinite(raw_price)
                or raw_price <= 0
            ):
                continue
            active[column] = True
            active_score[column] = max(
                float(conviction[row, column]),
                0.000001,
            )
            state_changed = True

        if not state_changed:
            return

        per_symbol_cap = max(
            0.0,
            float(trade_plan.execution.get("per_symbol_cap", 0.20)),
        )
        total_cap = min(
            1.0,
            max(
                0.0,
                float(trade_plan.execution.get("total_exposure_cap", 0.80)),
            ),
        )
        targets = np.zeros(columns, dtype=float)
        if trade_plan.target_weights is not None:
            declared = np.asarray(trade_plan.target_weights, dtype=float)[row]
            valid = active & np.isfinite(declared) & (declared > 0)
            targets[valid] = np.minimum(declared[valid], per_symbol_cap)
            total = float(targets.sum())
            if total > total_cap and total > 0:
                targets *= total_cap / total
        else:
            from ..strategy import allocate_target_weights

            targets = allocate_target_weights(
                np.maximum(active_score, 0.000001),
                active,
                per_symbol_cap,
                total_cap,
            )

        close_cny = np.asarray(prices[row], dtype=float) * fx_rate
        nav_before = new_pf.cash
        for column, code in enumerate(symbols):
            holding = new_pf.holdings.get(code)
            close = close_cny[column]
            if (
                holding is not None
                and np.isfinite(close)
                and close > 0
            ):
                nav_before += holding.shares * close

        # Do not reduce young ordinary holdings merely to finance a new event.
        for column, code in enumerate(symbols):
            holding = new_pf.holdings.get(code)
            close = close_cny[column]
            if (
                holding is None
                or holding.shares <= 0
                or not np.isfinite(close)
                or close <= 0
                or not tradable[row, column]
            ):
                continue
            held_days = 10**9
            if holding.last_buy_date:
                try:
                    held_days = (
                        datetime.strptime(plan_date, "%Y-%m-%d")
                        - datetime.strptime(holding.last_buy_date, "%Y-%m-%d")
                    ).days
                except ValueError:
                    held_days = 10**9
            current_value = holding.shares * close
            target_value = nav_before * targets[column]
            if current_value <= target_value or held_days < min_calendar_days:
                continue
            sell_price = float(lows[row, column]) * fx_rate
            if not np.isfinite(sell_price) or sell_price <= 0:
                continue
            quantity = int(
                (current_value - target_value) / sell_price / lot
            ) * lot
            quantity = min(quantity, int(holding.shares / lot) * lot)
            if quantity > 0:
                sell(
                    column,
                    quantity,
                    "trade_plan_target_reduce",
                    f"{new_pf.strategy_run_id}:{plan_date}:{code}:target_reduce",
                )

        nav_after_sells = new_pf.cash
        current_shares = np.zeros(columns, dtype=float)
        for column, code in enumerate(symbols):
            holding = new_pf.holdings.get(code)
            if holding is not None:
                current_shares[column] = holding.shares
            close = close_cny[column]
            if holding is not None and np.isfinite(close) and close > 0:
                nav_after_sells += holding.shares * close

        desired = np.zeros(columns, dtype=float)
        required_cash = 0.0
        buy_prices = np.asarray(highs[row], dtype=float) * fx_rate
        for column in range(columns):
            close = close_cny[column]
            price = buy_prices[column]
            if (
                not active[column]
                or not tradable[row, column]
                or not np.isfinite(close)
                or close <= 0
                or not np.isfinite(price)
                or price <= 0
            ):
                continue
            target_shares = nav_after_sells * targets[column] / close
            desired[column] = max(
                target_shares - current_shares[column],
                0.0,
            )
            required_cash += desired[column] * price * (1.0 + fee_rate)
        cash_scale = 1.0
        if required_cash > new_pf.cash and required_cash > 0:
            cash_scale = new_pf.cash / required_cash
        for column in range(columns):
            quantity = int(desired[column] * cash_scale / lot) * lot
            if quantity <= 0:
                continue
            buy(
                column,
                quantity,
                "trade_plan_target_increase",
                (
                    f"{new_pf.strategy_run_id}:{plan_date}:"
                    f"{symbols[column]}:target_increase"
                ),
            )

    def rebalance_plan(
        self,
        pf: RefPortfolio,
        trade_plan,
        market_data,
        trade_date: str,
        *,
        run_id: str,
        strategy_id: str,
        lot_size: int = 100,
        commission_rate: float = DEFAULT_COMMISSION_RATE,
        min_holding_days: int = 0,
        fx_rate: float = 1.0,
        label: str = "",
        force: bool = False,
    ) -> tuple[RefPortfolio, list[Trade]]:
        """Execute the final effective row of the canonical market TradePlan.

        The persistent account is pinned to one activated optimizer run.  It
        therefore rejects a plan from any other run until a manual reset binds
        the new run.  Live fills use the trigger-day high for buys, low for
        sells and close for valuation, matching the reference-account policy.
        """
        tag = f"[{label}]" if label else ""
        if (
            not pf.is_bound
            or pf.strategy_run_id != run_id
            or pf.strategy_id != strategy_id
        ):
            logger.warning(
                "参考持仓%s 未绑定或运行不匹配，跳过调仓: pinned=%s/%s plan=%s/%s",
                tag,
                pf.strategy_run_id,
                pf.strategy_id,
                run_id,
                strategy_id,
            )
            return pf, []
        try:
            parsed_trade_date = datetime.strptime(trade_date[:10], "%Y-%m-%d")
        except ValueError:
            logger.warning("参考持仓%s 无法解析日期 %s", tag, trade_date)
            return pf, []
        if not force and parsed_trade_date.weekday() >= 5:
            return pf, []

        dates = [str(value)[:10] for value in trade_plan.dates]
        rows = [index for index, value in enumerate(dates) if value <= trade_date[:10]]
        if not rows:
            return pf, []
        row = rows[-1]
        plan_date = dates[row]
        symbols = list(trade_plan.symbols or market_data.symbols)
        prices = np.asarray(market_data.prices, dtype=float)
        if prices.ndim != 2 or prices.shape != trade_plan.buy_signals.shape:
            raise ValueError("TradePlan and live market prices must have equal shapes")
        highs = (
            prices
            if market_data.highs is None
            else np.asarray(market_data.highs, dtype=float)
        )
        lows = (
            prices
            if market_data.lows is None
            else np.asarray(market_data.lows, dtype=float)
        )
        tradable = (
            np.isfinite(prices) & (prices > 0)
            if market_data.tradable is None
            else np.asarray(market_data.tradable, dtype=bool)
        )
        if any(value.shape != prices.shape for value in (highs, lows, tradable)):
            raise ValueError("Live close/high/low/tradable matrices must align")

        new_pf = self._copy_portfolio(pf)
        processed = set(new_pf.processed_events)
        trades: list[Trade] = []
        lot = max(1, int(lot_size))
        fee_rate = max(0.0, float(commission_rate))
        scale = float(fx_rate)

        def event_id(column: int, side: str) -> str:
            return f"{run_id}:{plan_date}:{symbols[column]}:{side}"

        def sell(column: int, quantity: int, reason: str, eid: str) -> bool:
            code = symbols[column]
            holding = new_pf.holdings.get(code)
            raw_price = float(lows[row, column])
            if (
                holding is None
                or holding.shares <= 0
                or quantity <= 0
                or not tradable[row, column]
                or not np.isfinite(raw_price)
                or raw_price <= 0
            ):
                return False
            quantity = min(int(quantity), holding.shares)
            price = raw_price * scale
            gross = quantity * price
            commission = gross * fee_rate
            holding.shares -= quantity
            new_pf.cash += gross - commission
            if holding.shares <= 0:
                del new_pf.holdings[code]
            trade = Trade(
                plan_date,
                code,
                "sell",
                quantity,
                round(price, 4),
                -gross,
                reason,
                commission,
                eid,
                run_id,
                strategy_id,
            )
            trades.append(trade)
            new_pf.trade_log.append(trade)
            return True

        def buy(column: int, quantity: int, reason: str, eid: str) -> bool:
            code = symbols[column]
            raw_price = float(highs[row, column])
            if (
                quantity <= 0
                or not tradable[row, column]
                or not np.isfinite(raw_price)
                or raw_price <= 0
            ):
                return False
            price = raw_price * scale
            gross = quantity * price
            commission = gross * fee_rate
            total_cost = gross + commission
            if total_cost > new_pf.cash + 0.000001:
                return False
            old = new_pf.holdings.get(code)
            if old is None:
                new_pf.holdings[code] = Holding(
                    code,
                    quantity,
                    price,
                    plan_date,
                )
            else:
                old_value = old.shares * old.avg_cost
                old.shares += quantity
                old.avg_cost = (old_value + quantity * price) / old.shares
                old.last_buy_date = plan_date
            new_pf.cash -= total_cost
            trade = Trade(
                plan_date,
                code,
                "buy",
                quantity,
                round(price, 4),
                gross,
                reason,
                commission,
                eid,
                run_id,
                strategy_id,
            )
            trades.append(trade)
            new_pf.trade_log.append(trade)
            return True

        model = str(trade_plan.execution.get("model", "cash_cap"))
        if model == "target_weight":
            self._rebalance_target_plan(
                new_pf,
                trade_plan,
                row,
                plan_date,
                symbols,
                prices,
                highs,
                lows,
                tradable,
                processed,
                event_id,
                sell,
                buy,
                lot,
                fee_rate,
                scale,
                min_holding_days,
            )
        elif model == "cash_cap":
            sell_signals = np.asarray(trade_plan.sell_signals, dtype=bool)
            buy_signals = np.asarray(trade_plan.buy_signals, dtype=bool)
            sell_priority = np.asarray(trade_plan.sell_priority, dtype=float)
            buy_priority = np.asarray(trade_plan.buy_priority, dtype=float)
            date_to_row = {value: index for index, value in enumerate(dates)}

            sell_columns = sorted(
                np.flatnonzero(sell_signals[row]),
                key=lambda column: (-sell_priority[row, column], symbols[column]),
            )
            for column in sell_columns:
                eid = event_id(int(column), "sell")
                if eid in processed:
                    continue
                processed.add(eid)
                holding = new_pf.holdings.get(symbols[column])
                if holding is None:
                    continue
                buy_row = date_to_row.get(holding.last_buy_date)
                if (
                    buy_row is not None
                    and row - buy_row < max(0, int(min_holding_days))
                ):
                    continue
                price = float(lows[row, column]) * scale
                if not np.isfinite(price) or price <= 0:
                    continue
                desired = min(
                    float(trade_plan.sell_cash_limit),
                    holding.shares * price,
                )
                quantity = int(desired / price / lot) * lot
                if quantity <= 0 and holding.shares < lot:
                    quantity = holding.shares
                sell(int(column), quantity, "trade_plan_sell", eid)

            buy_columns = sorted(
                np.flatnonzero(buy_signals[row]),
                key=lambda column: (-buy_priority[row, column], symbols[column]),
            )
            for column in buy_columns:
                eid = event_id(int(column), "buy")
                if eid in processed:
                    continue
                processed.add(eid)
                price = float(highs[row, column]) * scale
                if not np.isfinite(price) or price <= 0 or new_pf.cash <= 0:
                    continue
                budget = min(float(trade_plan.buy_cash_limit), new_pf.cash)
                quantity = int(
                    budget / (price * (1.0 + fee_rate)) / lot
                ) * lot
                buy(int(column), quantity, "trade_plan_buy", eid)
        else:
            raise ValueError(f"Unsupported TradePlan execution model: {model}")

        new_pf.processed_events = sorted(processed)
        if trades:
            previous = (pf.last_rebalance_date or "")[:10]
            new_pf.last_rebalance_date = plan_date
            new_pf.trading_days = pf.trading_days + int(previous != plan_date)
        return new_pf, trades

    # ── 查询 ──

    @staticmethod
    def calculate_nav(pf: RefPortfolio, prices: dict[str, float]) -> float:
        """计算当前净值。"""
        return pf.nav(prices)

    @staticmethod
    def get_status(pf: RefPortfolio, prices: dict[str, float] | None = None) -> dict:
        """获取可展示的状态摘要。

        Returns:
            {
                "inception_date": "2026-07-14",
                "cash": 95820.50,
                "initial_capital": 100000.0,
                "trading_days": 3,
                "nav": 102350.80,
                "nav_return_pct": 2.35,
                "holdings": [{"code": "601728", "shares": 500, "price": 12.50,
                              "market_value": 6250.0, "avg_cost": 12.34}],
                "last_rebalance_date": "2026-07-15",
            }
        """
        prices = prices or {}
        nav = pf.nav(prices)
        nav_ret = pf.nav_return_pct(prices)

        holdings_list = []
        for code, h in pf.holdings.items():
            p = prices.get(code, 0.0)
            holdings_list.append(
                {
                    "code": code,
                    "shares": h.shares,
                    "price": round(p, 2),
                    "market_value": round(h.shares * p, 2),
                    "avg_cost": round(h.avg_cost, 4),
                }
            )

        return {
            "inception_date": pf.inception_date,
            "cash": round(pf.cash, 2),
            "initial_capital": pf.initial_capital,
            "trading_days": pf.trading_days,
            "nav": round(nav, 2) if prices else round(pf.cash, 2),
            "nav_return_pct": round(nav_ret, 2) if nav_ret is not None else None,
            "holdings": holdings_list,
            "last_rebalance_date": pf.last_rebalance_date,
            "total_market_value": round(pf.total_market_value(prices), 2),
            "market_group": pf.market_group,
            "strategy_run_id": pf.strategy_run_id,
            "strategy_id": pf.strategy_id,
            "strategy_timestamp": pf.strategy_timestamp,
            "params_hash": pf.params_hash,
            "execution_hash": pf.execution_hash,
            "requires_manual_reset": bool(pf.inception_date and not pf.is_bound),
        }

    # ── 便捷方法 ──

    def is_initialized(self, pf: RefPortfolio) -> bool:
        """持仓是否已初始化（有期初日期）。"""
        return bool(pf.inception_date)
