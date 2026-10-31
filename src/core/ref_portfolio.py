"""参考持仓管理模块。

持久化参考持仓（Ref Portfolio），用于日报/简报展示系统持续运行的仓位状态。
- 由简报入口按绑定策略的统一 TradePlan 调仓
- 周末默认不交易，显式 force 调用可覆盖
- 仅手动 reset 时重置，否则永远接上一期仓位

数据持久化到 data/ref_portfolio.yaml。
"""

from __future__ import annotations

import copy
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
DATA_DIR = Path("data")
PORTFOLIO_FILE = DATA_DIR / "ref_portfolio.yaml"


def _parse_date(value: object) -> date | None:
    if not value:
        return None
    try:
        # Calendar-only input: retain naive dates for validation/day differences.
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()  # noqa: DTZ007
    except ValueError:
        return None


def reference_valuation_price(value: object) -> float | None:
    """Accept only finite, positive prices; missing quotes are never zero."""
    if isinstance(value, (bool, np.bool_)):
        return None
    try:
        price = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return price if np.isfinite(price) and price > 0 else None


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
        "withholding_rate": float(
            execution_config.withholding_rates.get(market_group, 0.0)
        ),
    }


def reference_execution_binding_status(stored_hash: str, contract: dict) -> str:
    """Diagnose an execution binding without relaxing the trading guard."""
    from ..search.contracts import stable_hash

    if stored_hash == stable_hash(contract):
        return "current"
    if "withholding_rate" in contract:
        legacy_contract = {
            key: value for key, value in contract.items() if key != "withholding_rate"
        }
        if stored_hash == stable_hash(legacy_contract):
            return "legacy_missing_withholding"
    return "mismatch"


# ── 数据模型 ──────────────────────────────────────────────────


@dataclass
class Holding:
    """单笔持仓"""

    code: str
    shares: float
    avg_cost: float  # 每股平均成交价；手续费单独计入现金和 Trade
    last_buy_date: str = ""


@dataclass
class Trade:
    """单笔交易记录"""

    date: str  # YYYY-MM-DD
    code: str
    action: str  # "buy" / "sell"
    shares: float
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
    gross_dividend_cash: float = 0.0
    dividend_tax_cost: float = 0.0
    net_dividend_cash: float = 0.0
    in_kind_positions: dict[str, dict] = field(default_factory=dict)

    @property
    def is_bound(self) -> bool:
        return bool(
            self.market_group
            and self.strategy_run_id
            and self.strategy_run_id != "legacy"
            and self.strategy_id
        )

    def missing_price_codes(self, prices: dict[str, float]) -> list[str]:
        """List holdings whose current valuation cannot be determined."""
        missing = {
            code
            for code in self.holdings
            if reference_valuation_price(prices.get(code)) is None
        }
        missing.update(
            item["asset_code"]
            for item in self.in_kind_positions.values()
            if (item["quantity"] > 0 or (
                item["fraction"] > 0 and not item.get("fractional_cash_known", False)
            ))
            and reference_valuation_price(prices.get(item["asset_code"])) is None
        )
        return sorted(missing)

    def total_market_value(self, prices: dict[str, float]) -> float | None:
        """当前持仓市值；任一持仓缺少有效行情时返回 None。"""
        total = 0.0
        for code, h in self.holdings.items():
            price = reference_valuation_price(prices.get(code))
            if price is None:
                return None
            total += h.shares * price
        for item in self.in_kind_positions.values():
            fixed_fraction = item.get("fractional_cash_known", False)
            units = item["quantity"] + (0.0 if fixed_fraction else item["fraction"])
            if fixed_fraction:
                total += float(item["fractional_cash_value"])
            total += float(item.get("tiered_cash_value", 0.0))
            if units <= 0:
                continue
            price = reference_valuation_price(prices.get(item["asset_code"]))
            if price is None:
                return None
            total += units * price
        return total

    def nav(self, prices: dict[str, float]) -> float | None:
        """当前净值 = 现金 + 完整持仓市值；不把缺价持仓当作零。"""
        market_value = self.total_market_value(prices)
        return None if market_value is None else self.cash + market_value

    def nav_return_pct(self, prices: dict[str, float]) -> float | None:
        """净值回报率（相对 initial_capital）。"""
        nav = self.nav(prices)
        if self.initial_capital <= 0 or nav is None:
            return None
        return (nav / self.initial_capital - 1.0) * 100.0

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
            "gross_dividend_cash": round(self.gross_dividend_cash, 2),
            "dividend_tax_cost": round(self.dividend_tax_cost, 2),
            "net_dividend_cash": round(self.net_dividend_cash, 2),
            "processed_events": list(dict.fromkeys(self.processed_events)),
            "in_kind_positions": copy.deepcopy(self.in_kind_positions),
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
    def from_dict(cls, d: dict) -> RefPortfolio:
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
            gross_dividend_cash=float(d.get("gross_dividend_cash", 0.0) or 0.0),
            dividend_tax_cost=float(d.get("dividend_tax_cost", 0.0) or 0.0),
            net_dividend_cash=float(d.get("net_dividend_cash", 0.0) or 0.0),
            in_kind_positions=copy.deepcopy(d.get("in_kind_positions") or {}),
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
        # Keep the documented empty-portfolio fallback for corrupt storage.
        except Exception as e:  # noqa: BLE001
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
        # Preserve atomic-save cleanup and re-raise every storage failure.
        except Exception as e:  # noqa: BLE001
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
            # Portfolio inception/reset uses the existing local calendar date.
            inception_date = date.today().strftime("%Y-%m-%d")  # noqa: DTZ011
        # Portfolio inception/reset uses the existing local calendar date.
        now_str = date.today().strftime("%Y-%m-%d")  # noqa: DTZ011
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
            gross_dividend_cash=pf.gross_dividend_cash,
            dividend_tax_cost=pf.dividend_tax_cost,
            net_dividend_cash=pf.net_dividend_cash,
            in_kind_positions=copy.deepcopy(pf.in_kind_positions),
        )

    def apply_corporate_actions(
        self,
        pf: RefPortfolio,
        actions: list[object],
        as_of_date: str | date,
        *,
        withholding_rate: float,
        fx_rate: float = 1.0,
        external_prices: dict | None = None,
    ) -> tuple[RefPortfolio, list[Trade]]:
        """Accrue causally-known dividends and share actions before trading.

        Reference portfolios use the same raw-price, explicit-cash contract as
        the backtester.  A cash dividend that was not public by its ex-date is
        rejected rather than credited with hindsight.
        """
        try:
            cutoff = (
                as_of_date
                if isinstance(as_of_date, date)
                # Calendar-only input: retain naive dates for validation/day differences.
                else datetime.strptime(str(as_of_date)[:10], "%Y-%m-%d").date()  # noqa: DTZ007
            )
        except ValueError as exc:
            raise ValueError(f"invalid corporate-action cutoff: {as_of_date}") from exc
        rate = float(withholding_rate)
        if not 0.0 <= rate < 1.0:
            raise ValueError("withholding rate must be in [0, 1)")
        scale = float(fx_rate)
        if not np.isfinite(scale) or scale <= 0.0:
            raise ValueError("corporate-action FX rate must be positive")
        inception = _parse_date(pf.inception_date)
        new_pf = self._copy_portfolio(pf)
        processed = set(new_pf.processed_events)
        trades: list[Trade] = []
        self._apply_in_kind_actions(
            new_pf, actions, cutoff, scale, external_prices, trades
        )
        ordered = sorted(
            actions,
            key=lambda item: (
                getattr(item, "ex_date", date.max),
                str(getattr(item, "code", "")),
                str(getattr(item, "action_type", "")),
            ),
        )
        for action in ordered:
            if getattr(action, "action_type", "") == "in_kind_distribution":
                continue
            code = str(getattr(action, "code", ""))
            ex_date = getattr(action, "ex_date", None)
            if not isinstance(ex_date, date) or ex_date > cutoff:
                continue
            holding = new_pf.holdings.get(code)
            if holding is None or holding.shares <= 0.0:
                continue
            published_at = getattr(action, "published_at", None)
            cash_per_share = getattr(action, "cash_per_share", None)
            multiplier = getattr(action, "share_multiplier", None)
            rights_price = getattr(action, "rights_price", None)
            if rights_price is not None:
                raise ValueError(
                    f"unsupported reference rights issue: {code} {ex_date}"
                )
            if cash_per_share is None and multiplier is None:
                raise ValueError(
                    f"unresolved reference corporate action: {code} {ex_date}"
                )
            event_id = (
                f"corp:{code}:{ex_date.isoformat()}:"
                f"{getattr(action, 'action_type', '')}:{cash_per_share}:"
                f"{multiplier}:{published_at}"
            )
            if event_id in processed:
                continue
            if inception is not None and ex_date < inception:
                processed.add(event_id)
                continue
            if cash_per_share is not None:
                if not isinstance(published_at, date) or published_at > ex_date:
                    raise ValueError(
                        f"cash dividend lacks causal publication date: {code} {ex_date}"
                    )
                per_share = float(cash_per_share) * scale
                if not np.isfinite(per_share) or per_share < 0.0:
                    raise ValueError(
                        f"invalid reference cash dividend: {code} {ex_date}"
                    )
                gross = holding.shares * per_share
                tax = gross * rate
                net = gross - tax
                new_pf.cash += net
                new_pf.gross_dividend_cash += gross
                new_pf.dividend_tax_cost += tax
                new_pf.net_dividend_cash += net
                trade = Trade(
                    date=ex_date.isoformat(),
                    code=code,
                    action="dividend",
                    shares=holding.shares,
                    price=round(per_share, 6),
                    cost=-gross,
                    reason="corporate_action_dividend",
                    commission=tax,
                    event_id=event_id,
                    run_id=new_pf.strategy_run_id,
                    strategy_id=new_pf.strategy_id,
                )
                trades.append(trade)
                new_pf.trade_log.append(trade)
            if multiplier is not None:
                share_multiplier = float(multiplier)
                if not np.isfinite(share_multiplier) or share_multiplier <= 0.0:
                    raise ValueError(
                        f"invalid reference share multiplier: {code} {ex_date}"
                    )
                previous_shares = holding.shares
                holding.shares *= share_multiplier
                holding.avg_cost /= share_multiplier
                if not np.isclose(share_multiplier, 1.0):
                    trade = Trade(
                        date=ex_date.isoformat(),
                        code=code,
                        action="share_action",
                        shares=holding.shares - previous_shares,
                        price=0.0,
                        cost=0.0,
                        reason="corporate_action_share_multiplier",
                        event_id=event_id,
                        run_id=new_pf.strategy_run_id,
                        strategy_id=new_pf.strategy_id,
                    )
                    trades.append(trade)
                    new_pf.trade_log.append(trade)
            processed.add(event_id)
        new_pf.processed_events = sorted(processed)
        return new_pf, trades

    @staticmethod
    def _apply_in_kind_actions(
        pf: RefPortfolio,
        actions: list[object],
        cutoff: date,
        fx_rate: float,
        external_prices: dict | None,
        trades: list[Trade],
    ) -> None:
        """Persist ex-date entitlements separately from tradeable holdings.

        Missed ex-date capture is an explicit reconciliation error: today's
        holdings cannot prove historical qualification.  Existing captured
        rights continue to settle even after the parent position is sold.
        """
        import pandas as pd

        from ..backtest.in_kind import advance_in_kind, build_in_kind_schedule, execution_term
        from ..search.contracts import stable_hash

        inception = _parse_date(pf.inception_date)
        for action in actions:
            if getattr(action, "action_type", "") != "in_kind_distribution":
                continue
            ex_date = getattr(action, "ex_date", None)
            if ex_date is None or ex_date > cutoff:
                continue
            if inception is not None and ex_date < inception:
                continue
            code = str(getattr(action, "code", ""))
            terms = getattr(action, "in_kind_terms", None)
            asset = str(getattr(terms, "asset_code", "") or "")
            identity = f"in_kind:{code}:{ex_date}:{asset}"
            state = pf.in_kind_positions.get(identity)
            terms_data = (
                terms.model_dump() if hasattr(terms, "model_dump")
                else vars(terms) if terms is not None else None
            )
            terms_hash = stable_hash(terms_data)
            conflicts = [
                key for key in pf.in_kind_positions
                if key.startswith(f"in_kind:{code}:{ex_date}:") and key != identity
            ]
            if conflicts or (state is not None and state.get("terms_hash") != terms_hash):
                raise ValueError(f"in-kind terms changed; reconciliation required: {identity}")
            if state is None and ex_date < cutoff:
                raise ValueError(
                    f"missed in-kind ex-date requires holdings reconciliation: {identity}"
                )
            if state is not None and state["last_processed_date"] >= cutoff.isoformat():
                continue
            dates = pd.date_range(ex_date, cutoff, freq="D")
            schedule = build_in_kind_schedule(
                [action], dates, [code], external_prices
            ).scaled(fx_rate)
            if len(schedule.events) != 1:
                raise ValueError(f"in-kind reference schedule is incomplete: {identity}")
            quantities = np.array([float(state["quantity"]) if state else 0.0])
            fractions = np.array([float(state["fraction"]) if state else 0.0])
            audit = (
                np.asarray(state["audit"], dtype=np.float64).reshape(1, 14)
                if state else np.zeros((1, 14))
            )
            holding = pf.holdings.get(code)
            parent_shares = np.array([holding.shares if holding else 0.0])
            start = (
                int(dates.searchsorted(pd.Timestamp(state["last_processed_date"]))) + 1
                if state else 0
            )
            before_cash = pf.cash
            for row in range(start, len(dates)):
                cash, _value = advance_in_kind(
                    row, parent_shares, *schedule.kernel_args(), quantities, fractions,
                    audit,
                )
                pf.cash += cash
            cash_known = len(dates) - 1 >= int(schedule.events[0, 12])
            fractional_value = fractions[0] * schedule.events[0, 6]
            if fractions[0] * schedule.events[0, 28] < schedule.events[0, 9]:
                fractional_value = 0.0
            fractional_value *= schedule.fx_rates[-1, 0]
            described = schedule.describe(quantities, fractions, audit)
            position_value = sum(
                item["value"] for item in described["in_kind_positions"]
            )
            pf.in_kind_positions[identity] = {
                "asset_code": asset,
                "quantity": float(quantities[0]),
                "fraction": float(fractions[0]),
                "last_processed_date": cutoff.isoformat(),
                "delivered": cutoff >= execution_term(terms, "actual_delivery_date") if execution_term(terms, "actual_delivery_date") is not None else bool(audit[0, 7]),
                "audit": audit[0].tolist(),
                "fractional_cash_known": cash_known,
                "fractional_cash_value": float(fractional_value),
                "cashflows": described["in_kind_cashflows"],
                "value": float(position_value),
                "tiered_cash_value": float(sum(item.get("cash_receivable", 0.0) for item in described["in_kind_positions"])),
                "settlement_basis": getattr(terms, "settlement_basis", "confirmed"),
                "assumption_basis": getattr(terms, "assumption_basis", None) or getattr(terms, "estimation_assumptions", []),
                "policy_hash": getattr(terms, "policy_hash", None) or getattr(terms, "estimation_policy_id", None),
                "terms_hash": terms_hash,
                "source_url": getattr(terms, "source_url", None),
                "ex_date": ex_date.isoformat(),
                "delivery_date": described["in_kind_positions"][0]["delivery_date"] if described["in_kind_positions"] else None,
                "parent_code": code,
            }
            trade = Trade(
                date=cutoff.isoformat(), code=code, action="in_kind",
                shares=float(quantities[0] + fractions[0]), price=0.0,
                cost=before_cash - pf.cash, reason="corporate_action_in_kind",
                event_id=f"{identity}:{cutoff}", run_id=pf.strategy_run_id,
                strategy_id=pf.strategy_id,
            )
            trades.append(trade)
            pf.trade_log.append(trade)

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
                        # Calendar-only input: retain naive dates for validation/day differences.
                        datetime.strptime(plan_date, "%Y-%m-%d")  # noqa: DTZ007
                        # Calendar-only input: retain naive dates for validation/day differences.
                        - datetime.strptime(holding.last_buy_date, "%Y-%m-%d")  # noqa: DTZ007
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
        locked_value = 0.0
        for item in new_pf.in_kind_positions.values():
            if float(item.get("value", 0.0)) <= 0:
                continue
            if item["last_processed_date"] != plan_date:
                raise ValueError("in-kind reference valuation is stale before allocation")
            locked_value += float(item["value"])
        close_cny = np.asarray(prices[row], dtype=float) * fx_rate
        capital = new_pf.cash + locked_value
        for column, code in enumerate(symbols):
            holding = new_pf.holdings.get(code)
            close = close_cny[column]
            if holding is not None and np.isfinite(close) and close > 0:
                capital += holding.shares * close
        if capital > 0:
            total_cap = max(total_cap - locked_value / capital, 0.0)
        symbol_caps = np.full(columns, per_symbol_cap, dtype=float)
        if capital > 0:
            for item in new_pf.in_kind_positions.values():
                if item["asset_code"] not in symbols:
                    continue
                column = symbols.index(item["asset_code"])
                equity_value = float(item["value"]) - float(item.get("tiered_cash_value", 0.0))
                if item.get("fractional_cash_known", False):
                    equity_value -= float(item.get("fractional_cash_value", 0.0))
                symbol_caps[column] = max(
                    symbol_caps[column] - max(equity_value, 0.0) / capital, 0.0
                )
        targets = np.zeros(columns, dtype=float)
        if trade_plan.target_weights is not None:
            declared = np.asarray(trade_plan.target_weights, dtype=float)[row]
            valid = active & np.isfinite(declared) & (declared > 0)
            targets[valid] = np.minimum(declared[valid], symbol_caps[valid])
            total = float(targets.sum())
            if total > total_cap and total > 0:
                targets *= total_cap / total
        else:
            remaining = total_cap
            while remaining > 1e-12:
                available = active & (active_score > 0) & (targets < symbol_caps - 1e-12)
                score_sum = float(active_score[available].sum())
                if not available.any() or score_sum <= 0:
                    break
                addition = np.zeros(columns, dtype=float)
                addition[available] = np.minimum(
                    remaining * active_score[available] / score_sum,
                    symbol_caps[available] - targets[available],
                )
                used = float(addition.sum())
                if used <= 1e-12:
                    break
                targets += addition
                remaining -= used

        close_cny = np.asarray(prices[row], dtype=float) * fx_rate
        nav_before = new_pf.cash + locked_value
        for column, code in enumerate(symbols):
            holding = new_pf.holdings.get(code)
            close = close_cny[column]
            if holding is not None and np.isfinite(close) and close > 0:
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
                        # Calendar-only input: retain naive dates for validation/day differences.
                        datetime.strptime(plan_date, "%Y-%m-%d")  # noqa: DTZ007
                        # Calendar-only input: retain naive dates for validation/day differences.
                        - datetime.strptime(holding.last_buy_date, "%Y-%m-%d")  # noqa: DTZ007
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
            quantity = int((current_value - target_value) / sell_price / lot) * lot
            quantity = min(quantity, int(holding.shares / lot) * lot)
            if quantity > 0:
                sell(
                    column,
                    quantity,
                    "trade_plan_target_reduce",
                    f"{new_pf.strategy_run_id}:{plan_date}:{code}:target_reduce",
                )

        nav_after_sells = new_pf.cash + locked_value
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
            # Calendar-only input: retain naive dates for validation/day differences.
            parsed_trade_date = datetime.strptime(trade_date[:10], "%Y-%m-%d")  # noqa: DTZ007
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
                if buy_row is not None and row - buy_row < max(
                    0, int(min_holding_days)
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
                quantity = int(budget / (price * (1.0 + fee_rate)) / lot) * lot
                buy(int(column), quantity, "trade_plan_buy", eid)
        else:
            raise ValueError(f"Unsupported TradePlan execution model: {model}")

        new_pf.processed_events = sorted(processed)
        if trades:
            previous = (pf.last_rebalance_date or "")[:10]
            new_pf.last_rebalance_date = plan_date
            new_pf.trading_days = pf.trading_days + int(previous != plan_date)
        return new_pf, trades

    def adjust_position(
        self,
        pf: RefPortfolio,
        code: str,
        action: str,
        shares: int,
        price: float,
        trade_date: str,
        *,
        commission_rate: float = DEFAULT_COMMISSION_RATE,
        lot_size: int = 100,
        fx_rate: float = 1.0,
        event_id: str = "",
        reason: str = "manual_bot",
    ) -> tuple[RefPortfolio, list[Trade]]:
        """Apply one explicit bot position change and return an auditable copy.

        ``price`` is entered in the instrument's native currency; the
        portfolio stores cash, trade costs and average cost in CNY. ``set``
        means target shares, while ``buy``/``sell`` mean a quantity delta.
        This method deliberately bypasses signal timing and holding-period
        rules because the operator supplied an explicit execution price.
        """
        action = str(action).strip().lower()
        code = str(code).strip().upper()
        if action not in {"set", "buy", "sell"}:
            raise ValueError("action must be set, buy or sell")
        if not self.is_initialized(pf) or not pf.is_bound:
            raise ValueError("reference portfolio must be initialized and bound")
        if not code:
            raise ValueError("code is required")
        if int(shares) != shares or shares < 0:
            raise ValueError("shares must be a non-negative integer")
        shares = int(shares)
        if action != "set" and shares <= 0:
            raise ValueError("buy/sell shares must be positive")
        if lot_size <= 0:
            raise ValueError("lot_size must be positive")
        if shares % lot_size:
            raise ValueError(f"shares must be a multiple of lot size {lot_size}")
        try:
            # Calendar-only input: retain naive dates for validation/day differences.
            datetime.strptime(trade_date, "%Y-%m-%d")  # noqa: DTZ007
        except ValueError as exc:
            raise ValueError("trade_date must use YYYY-MM-DD") from exc
        price = float(price)
        fx_rate = float(fx_rate)
        commission_rate = float(commission_rate)
        if not np.isfinite(price) or price <= 0:
            raise ValueError("price must be positive")
        if not np.isfinite(fx_rate) or fx_rate <= 0:
            raise ValueError("fx_rate must be positive")
        if not np.isfinite(commission_rate) or commission_rate < 0:
            raise ValueError("commission_rate must be non-negative")

        event_id = event_id or (
            f"manual:{trade_date}:{action}:{code}:{shares}:{price:.8f}"
        )
        current = pf.holdings.get(code)
        current_shares = current.shares if current is not None else 0
        if event_id in pf.processed_events and (
            action != "set" or current_shares == shares
        ):
            return self._copy_portfolio(pf), []

        if action == "set":
            target_shares = shares
        elif action == "buy":
            target_shares = current_shares + shares
        else:
            target_shares = current_shares - shares
        if target_shares < 0:
            raise ValueError(f"sell shares exceed {code} holding ({current_shares})")

        delta = target_shares - current_shares
        new_pf = self._copy_portfolio(pf)
        new_pf.processed_events.append(event_id)
        new_pf.processed_events = list(dict.fromkeys(new_pf.processed_events))
        if delta == 0:
            return new_pf, []

        price_cny = price * fx_rate
        quantity = abs(delta)
        gross = quantity * price_cny
        commission = gross * commission_rate
        if delta > 0:
            total_cost = gross + commission
            if total_cost > new_pf.cash + 1e-9:
                raise ValueError(
                    f"cash insufficient: need {total_cost:.2f}, "
                    f"available {new_pf.cash:.2f}"
                )
            new_pf.cash -= total_cost
            holding = new_pf.holdings.get(code)
            if holding is None:
                new_pf.holdings[code] = Holding(
                    code=code,
                    shares=quantity,
                    avg_cost=price_cny,
                    last_buy_date=trade_date,
                )
            else:
                basis = holding.shares * holding.avg_cost + total_cost
                holding.shares += quantity
                holding.avg_cost = basis / holding.shares
                holding.last_buy_date = trade_date
            trade = Trade(
                date=trade_date,
                code=code,
                action="buy",
                shares=quantity,
                price=round(price_cny, 4),
                cost=gross,
                reason=reason,
                commission=commission,
                event_id=event_id,
                run_id=pf.strategy_run_id,
                strategy_id=pf.strategy_id,
            )
        else:
            holding = new_pf.holdings.get(code)
            if holding is None or holding.shares < quantity:
                raise ValueError(f"sell shares exceed {code} holding")
            holding.shares -= quantity
            new_pf.cash += gross - commission
            if holding.shares == 0:
                del new_pf.holdings[code]
            trade = Trade(
                date=trade_date,
                code=code,
                action="sell",
                shares=quantity,
                price=round(price_cny, 4),
                cost=-gross,
                reason=reason,
                commission=commission,
                event_id=event_id,
                run_id=pf.strategy_run_id,
                strategy_id=pf.strategy_id,
            )
        new_pf.trade_log.append(trade)
        new_pf.last_rebalance_date = trade_date
        new_pf.trading_days = pf.trading_days + int(
            (pf.last_rebalance_date or "")[:10] != trade_date
        )
        return new_pf, [trade]

    # ── 查询 ──

    @staticmethod
    def calculate_nav(pf: RefPortfolio, prices: dict[str, float]) -> float | None:
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
        market_value = pf.total_market_value(prices)
        missing_price_codes = pf.missing_price_codes(prices)

        holdings_list = []
        for code, h in pf.holdings.items():
            p = reference_valuation_price(prices.get(code))
            holdings_list.append(
                {
                    "code": code,
                    "shares": h.shares,
                    "price": round(p, 2) if p is not None else None,
                    "market_value": round(h.shares * p, 2) if p is not None else None,
                    "price_available": p is not None,
                    "avg_cost": round(h.avg_cost, 4),
                }
            )

        in_kind_positions, in_kind_cashflows = [], []
        for identity, item in pf.in_kind_positions.items():
            quantity, fraction = float(item["quantity"]), float(item["fraction"])
            asset = item["asset_code"]
            price = reference_valuation_price(prices.get(asset))
            fixed = item.get("fractional_cash_known", False)
            quoted_units = quantity + (0.0 if fixed else fraction)
            value = (
                quoted_units * price if price is not None
                else 0.0 if quoted_units == 0 else None
            )
            if fixed and value is not None:
                value += float(item["fractional_cash_value"])
            if value is not None:
                value += float(item.get("tiered_cash_value", 0.0))
            in_kind_positions.append({
                "event_id": identity, "asset_code": asset,
                "parent_code": item.get("parent_code", identity.split(":")[1]),
                "source_url": item.get("source_url"),
                "ex_date": item.get("ex_date", identity.split(":")[2]),
                "delivery_date": item.get("delivery_date"),
                "quantity": quantity, "fractional_quantity": fraction,
                "delivered": bool(item["delivered"]),
                "pending_quantity": 0.0 if item["delivered"] else quantity,
                "value": round(value, 2) if value is not None else None,
                "cash_receivable": float(item.get("tiered_cash_value", 0.0)),
                "settlement_basis": item.get("settlement_basis", "confirmed"),
                "assumption_basis": item.get("assumption_basis"),
                "policy_hash": item.get("policy_hash"),
            })
            in_kind_cashflows.extend(copy.deepcopy(item.get("cashflows", [])))

        return {
            "inception_date": pf.inception_date,
            "cash": round(pf.cash, 2),
            "initial_capital": pf.initial_capital,
            "trading_days": pf.trading_days,
            "nav": round(nav, 2) if nav is not None else None,
            "nav_return_pct": round(nav_ret, 2) if nav_ret is not None else None,
            "holdings": holdings_list,
            "in_kind_positions": in_kind_positions,
            "in_kind_cashflows": in_kind_cashflows,
            "last_rebalance_date": pf.last_rebalance_date,
            "total_market_value": (
                round(market_value, 2) if market_value is not None else None
            ),
            "valuation_complete": not missing_price_codes,
            "missing_price_codes": missing_price_codes,
            "valuation_reason": (
                "估值不完整：缺少 " + ", ".join(missing_price_codes) + " 行情"
                if missing_price_codes
                else ""
            ),
            "market_group": pf.market_group,
            "strategy_run_id": pf.strategy_run_id,
            "strategy_id": pf.strategy_id,
            "strategy_timestamp": pf.strategy_timestamp,
            "params_hash": pf.params_hash,
            "execution_hash": pf.execution_hash,
            "gross_dividend_cash": round(pf.gross_dividend_cash, 2),
            "dividend_tax_cost": round(pf.dividend_tax_cost, 2),
            "net_dividend_cash": round(pf.net_dividend_cash, 2),
            "requires_manual_reset": bool(pf.inception_date and not pf.is_bound),
        }

    # ── 便捷方法 ──

    def is_initialized(self, pf: RefPortfolio) -> bool:
        """持仓是否已初始化（有期初日期）。"""
        return bool(pf.inception_date)
