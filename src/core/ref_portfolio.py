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
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────────
DEFAULT_INITIAL_CAPITAL = 100000.0
DEFAULT_COMMISSION_RATE = 0.005
DEFAULT_BUY_AMOUNT = 5000.0          # 每次买入最大金额（已废弃）
BUY_CASH_FRACTION = 0.20             # 每次买入占现金的比例
MAX_BUY_AMOUNT = 50000.0             # 单次买入金额上限
REF_MONTHLY_LIMIT = 50000.0          # 参考持仓月度买入上限（比搜参宽松）
DEFAULT_SELL_FRACTION = 0.25         # 每次卖出最大比例
BRIEF_WINDOWS = ("09:50", "14:30")   # 允许调仓的时间窗口
DATA_DIR = Path("data")
PORTFOLIO_FILE = DATA_DIR / "ref_portfolio.yaml"


# ── 数据模型 ──────────────────────────────────────────────────

@dataclass
class Holding:
    """单笔持仓"""
    code: str
    shares: int
    avg_cost: float           # 每股平均成本（含手续费摊销）


@dataclass
class Trade:
    """单笔交易记录"""
    date: str                 # YYYY-MM-DD
    code: str
    action: str               # "buy" / "sell"
    shares: int
    price: float              # 成交单价
    cost: float               # 总金额（买入为正，卖出为负）
    reason: str               # 触发信号 rule_id
    commission: float = 0.0


@dataclass
class RefPortfolio:
    """参考持仓完整状态"""
    inception_date: str = ""           # 期初日期 YYYY-MM-DD
    cash: float = DEFAULT_INITIAL_CAPITAL
    initial_capital: float = DEFAULT_INITIAL_CAPITAL
    trading_days: int = 0              # 有交易的交易日数
    last_rebalance_date: str = ""      # 最近一次调仓日期
    last_reset_date: str = ""          # 最近一次手动重置日期
    holdings: dict[str, Holding] = field(default_factory=dict)
    trade_log: list[Trade] = field(default_factory=list)

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
            "holdings": {
                code: {"shares": h.shares, "avg_cost": round(h.avg_cost, 4)}
                for code, h in self.holdings.items()
            },
            "trade_log": [
                {
                    "date": t.date, "code": t.code, "action": t.action,
                    "shares": t.shares, "price": round(t.price, 4),
                    "cost": round(t.cost, 2), "reason": t.reason,
                    "commission": round(t.commission, 4),
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
        )
        for code, hd in (d.get("holdings") or {}).items():
            pf.holdings[code] = Holding(
                code=code, shares=hd["shares"], avg_cost=hd["avg_cost"],
            )
        for td in d.get("trade_log") or []:
            pf.trade_log.append(Trade(
                date=td["date"], code=td["code"], action=td["action"],
                shares=td["shares"], price=td["price"], cost=td["cost"],
                reason=td.get("reason", ""),
                commission=td.get("commission", 0.0),
            ))
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
                yaml.dump(data, allow_unicode=True, default_flow_style=False,
                          sort_keys=False),
                encoding="utf-8",
            )
            tmp.replace(self._file)
            logger.debug(f"参考持仓已保存: {self._file}")
        except Exception as e:
            logger.error(f"保存参考持仓失败: {e}")
            if tmp.exists():
                try:
                    tmp.unlink()
                except Exception:
                    pass

    # ── 重置 ──

    def reset(
        self,
        initial_capital: float = DEFAULT_INITIAL_CAPITAL,
        inception_date: str | None = None,
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
        )
        self.save(pf)
        logger.info(
            f"参考持仓已重置: 初始资金={initial_capital}, 期初={inception_date}"
        )
        return pf

    # ── 调仓 ──

    def rebalance(
        self,
        pf: RefPortfolio,
        alerts: list,
        prices: dict[str, float],
        trade_date: str,
        lot_size: int = 100,
        commission_rate: float = DEFAULT_COMMISSION_RATE,
        monthly_buy_limit: float = 15000.0,
        fx_rate: float = 1.0,
        label: str = "",
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

        # ── 前置校验：周末不交易 ──
        try:
            dt = datetime.strptime(trade_date, "%Y-%m-%d")
            if dt.weekday() >= 5:
                logger.info(f"参考持仓{tag} 跳过调仓: {trade_date} 是周末")
                return pf, []
        except ValueError:
            logger.warning(f"参考持仓{tag} 无法解析日期 {trade_date}")
            return pf, []

        # ── 分类信号 ──
        buy_signals = []   # (stock_code, rule_id)
        sell_signals = []  # (stock_code, rule_id)
        skipped_alerts = 0

        for alert in alerts:
            code = str(getattr(alert, "stock_code", ""))
            rid = str(getattr(alert, "rule_id", ""))
            rtype = str(getattr(alert, "type", ""))
            rlabel = str(getattr(alert, "rule_label", ""))
            if not code:
                skipped_alerts += 1
                continue

            is_buy = rtype == "strategy_buy" or "buy" in rid.lower()
            is_sell = rtype == "strategy_sell" or "sell" in rid.lower()

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
            holdings={k: Holding(v.code, v.shares, v.avg_cost)
                      for k, v in pf.holdings.items()},
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
                date=trade_date, code=code, action="sell",
                shares=sell_shares, price=round(price, 4), cost=-gross,
                reason=rid, commission=commission,
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

            max_amount = min(new_pf.cash * BUY_CASH_FRACTION, MAX_BUY_AMOUNT, new_pf.cash)
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
            else:
                new_pf.holdings[code] = Holding(
                    code=code, shares=buy_shares, avg_cost=price_cny,
                )

            trade = Trade(
                date=trade_date, code=code, action="buy",
                shares=buy_shares, price=round(price_cny, 4), cost=gross,
                reason=rid, commission=commission,
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
                1 if (pf.last_rebalance_date or "")[:10] != trade_date[:10]
                else 0
            )

        logger.info(
            f"参考持仓{tag} 调仓结束: {len(trades)}笔交易, "
            f"持仓{len(new_pf.holdings)}只, cash={new_pf.cash:,.0f}"
        )
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
            holdings_list.append({
                "code": code,
                "shares": h.shares,
                "price": round(p, 2),
                "market_value": round(h.shares * p, 2),
                "avg_cost": round(h.avg_cost, 4),
            })

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
        }

    # ── 便捷方法 ──

    def is_initialized(self, pf: RefPortfolio) -> bool:
        """持仓是否已初始化（有期初日期）。"""
        return bool(pf.inception_date)
