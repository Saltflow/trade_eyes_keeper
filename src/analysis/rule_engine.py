"""
规则引擎 — 动态条件解析与评估

设计目标：
  - 用户通过 YAML 配置定义买卖规则，无需修改代码
  - 使用安全的 Python 表达式求值（eval + 受限命名空间）
  - 默认规则与当前 MA60 锚点择时策略行为完全一致
  - 支持"只买不卖"等任意规则组合

使用示例:
    rules = get_default_rules()
    engine = RuleEngine(rules)
    ctx = {"deviation": -0.06, "cash": 10000, ...}
    for rule, amount in engine.evaluate_day(ctx):
        if rule.type == "buy":
            execute_buy(amount)
        elif rule.type == "sell":
            execute_sell(rule.action_fraction, rule.action_min, rule.action_max)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════
# 表达式引擎
# ════════════════════════════════════════════════════════


class ExpressionEngine:
    """
    安全的表达式求值器。

    仅允许有限的内置函数，其余全部禁用。
    上下文变量由调用方注入（deviation, cash, shares 等）。
    """

    # 白名单：仅在表达式内可用的内置函数
    ALLOWED_BUILTINS: dict[str, object] = {
        "min": min,
        "max": max,
        "abs": abs,
        "int": int,
        "float": float,
        "round": round,
        "bool": bool,
        "len": len,
        "True": True,
        "False": False,
        "None": None,
    }

    @staticmethod
    def evaluate(expr: str, context: dict) -> object:
        """
        在给定上下文中求值表达式。

        Args:
            expr: Python 表达式字符串
            context: 变量/值字典

        Returns:
            表达式结果 (bool, float, int, etc.)

        Raises:
            NameError: 表达式引用了未定义的变量或禁用的内置函数
            SyntaxError: 表达式语法错误
        """
        if not expr or not expr.strip():
            raise ValueError("表达式不能为空")

        # 合并白名单函数 + 上下文变量作为求值命名空间
        namespace = {**ExpressionEngine.ALLOWED_BUILTINS, **context}

        # __builtins__ = {} 禁用所有内置函数，避免安全风险
        return eval(expr, {"__builtins__": {}}, namespace)


# ════════════════════════════════════════════════════════
# 规则定义
# ════════════════════════════════════════════════════════


@dataclass
class Rule:
    """
    单条交易规则

    Attributes:
        id: 唯一标识（如 "buy_minus5"）
        label: 人类可读名称
        type: "buy" | "sell"
        priority: 执行优先级（越小越优先）
        condition: Python 表达式 → bool
        action_amount: (仅 buy) 买入金额表达式
        action_fraction: (仅 sell) 卖出比例 (0-1)
        action_min: (仅 sell) 最低卖出金额
        action_max: (仅 sell) 最高卖出金额
        budget_pool: 月度预算池 "buy" | "sell"
        reset_when: 重置"已触发"标记的条件表达式
    """

    id: str
    label: str
    type: str  # "buy" | "sell"
    priority: int
    condition: str
    budget_pool: str  # "buy" | "sell"

    # Buy 专用
    action_amount: str | None = None

    # Sell 专用
    action_fraction: float | None = None
    action_min: float | None = None
    action_max: float | None = None

    # 重置条件（None = 不重置，只能触发一次）
    reset_when: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "Rule":
        """
        从字典构造 Rule（用于 YAML 反序列化）。

        必须字段: id, type, condition, budget_pool
        """
        required = ["id", "type", "condition", "budget_pool"]
        for key in required:
            if key not in d:
                raise ValueError(f"Rule 缺少必须字段: {key}")

        rule_type = d["type"]
        if rule_type not in ("buy", "sell"):
            raise ValueError(f"Rule type 必须是 'buy' 或 'sell': {rule_type}")

        rule = cls(
            id=d["id"],
            label=d.get("label", d["id"]),
            type=rule_type,
            priority=d.get("priority", 0),
            condition=d["condition"],
            budget_pool=d["budget_pool"],
            reset_when=d.get("reset_when"),
        )

        if rule_type == "buy":
            rule.action_amount = d.get("action_amount", "min(5000, cash)")
        elif rule_type == "sell":
            rule.action_fraction = d.get("action_fraction", 0.25)
            rule.action_min = d.get("action_min", 2500)
            rule.action_max = d.get("action_max", 10000)

        return rule


# ════════════════════════════════════════════════════════
# 规则引擎
# ════════════════════════════════════════════════════════


class RuleEngine:
    """
    每日规则评估器。

    对给定的上下文（价格、持仓、预算等）评估所有规则：
      1. 重置满足重置条件的规则的"已触发"标记
      2. 按优先级顺序评估未锁定的规则
      3. 触发规则后立即锁定（同周期不再触发）
      4. 返回所有命中的规则及对应金额
    """

    def __init__(self, rules: list[Rule]):
        if not rules:
            raise ValueError("规则列表不能为空")
        # 按优先级升序（数字越小越优先）
        self.rules = sorted(rules, key=lambda r: r.priority)
        # 每个规则一个锁标记
        self._triggered: dict[str, bool] = {r.id: False for r in self.rules}

    def evaluate_day(self, context: dict) -> list[tuple[Rule, float]]:
        """
        评估当日所有规则。

        Args:
            context: 包含所有仿真变量的字典

        Returns:
            list of (Rule, action_amount): 命中的规则及对应的
            买入金额或卖出金额（sell 的 amount 由 action_fraction
            计算，在此返回占位值；实际卖股逻辑由调用方根据
            action_fraction/min/max 执行）
        """
        # ── 步骤1: 重置已解锁的规则 ──
        self._reset_triggered(context)

        # ── 步骤2: 按优先级评估 ──
        results: list[tuple[Rule, float]] = []
        for rule in self.rules:
            if self._triggered[rule.id]:
                continue

            try:
                cond = ExpressionEngine.evaluate(rule.condition, context)
            except Exception as exc:
                logger.warning(f"规则 {rule.id} 条件求值失败: {exc}")
                continue

            if not cond:
                continue

            # 计算动作金额
            amount = self._compute_action_amount(rule, context)

            results.append((rule, amount))
            self._triggered[rule.id] = True

        return results

    def _reset_triggered(self, context: dict):
        """重置满足 reset_when 条件的规则的锁。"""
        for rule in self.rules:
            if not self._triggered[rule.id]:
                continue
            if rule.reset_when is None:
                continue
            try:
                should_reset = ExpressionEngine.evaluate(rule.reset_when, context)
                if should_reset:
                    self._triggered[rule.id] = False
            except Exception as exc:
                logger.debug(f"规则 {rule.id} 重置条件求值失败: {exc}")

    def _compute_action_amount(self, rule: Rule, context: dict) -> float:
        """根据规则类型计算动作金额。"""
        if rule.type == "buy":
            if rule.action_amount:
                try:
                    return float(ExpressionEngine.evaluate(rule.action_amount, context))
                except Exception as exc:
                    logger.error(f"规则 {rule.id} 动作金额求值失败: {exc}")
                    return 0.0
            return 0.0
        else:
            # Sell: 返回 0.0 占位，实际由调用方根据 fraction/min/max 处理
            return 0.0

    @property
    def triggered_status(self) -> dict[str, bool]:
        """返回所有规则的触发状态（调试用）。"""
        return dict(self._triggered)


# ════════════════════════════════════════════════════════
# 默认规则（与当前 MA60 锚点择时行为一致）
# ════════════════════════════════════════════════════════


def get_default_rules() -> list[Rule]:
    """
    返回默认规则集，与当前硬编码的 MA60 锚点择时策略完全一致。

    规则:
      1. buy_minus5:  跌破 -5% → 买入 ≤5000
      2. buy_minus10: 跌破 -10% → 买入 ≤5000
      3. sell_plus5:  突破 +5% → 卖出 1/4
      4. sell_plus10: 突破 +10% → 卖出 1/4
      5. sell_plus15: 突破 +15% → 卖出 1/4

    买入重置: deviation 从 ≤0 穿越到 >0
    卖出重置: deviation 从 <0 穿越到 >0 或从 ≥0 穿越到 <0
    """
    buy_reset = (
        "deviation > 0 "
        "and prev_deviation is not None "
        "and prev_deviation <= 0"
    )
    sell_reset = (
        "(deviation < 0 "
        " and prev_deviation is not None "
        " and prev_deviation >= 0) "
        "or (deviation > 0 "
        "    and prev_deviation is not None "
        "    and prev_deviation < 0)"
    )

    return [
        Rule(
            id="buy_minus5",
            label="跌破MA60 -5%",
            type="buy",
            priority=1,
            condition=(
                "deviation <= -0.05 "
                "and prev_deviation is not None "
                "and prev_deviation > -0.05"
            ),
            action_amount="min(5000, cash)",
            budget_pool="buy",
            reset_when=buy_reset,
        ),
        Rule(
            id="buy_minus10",
            label="跌破MA60 -10%",
            type="buy",
            priority=2,
            condition=(
                "deviation <= -0.10 "
                "and prev_deviation is not None "
                "and prev_deviation > -0.10"
            ),
            action_amount="min(5000, cash)",
            budget_pool="buy",
            reset_when=buy_reset,
        ),
        Rule(
            id="sell_plus5",
            label="突破MA60 +5%",
            type="sell",
            priority=3,
            condition=(
                "deviation >= 0.05 "
                "and prev_deviation is not None "
                "and prev_deviation < 0.05 "
                "and shares > 0"
            ),
            action_fraction=0.25,
            action_min=2500,
            action_max=10000,
            budget_pool="sell",
            reset_when=sell_reset,
        ),
        Rule(
            id="sell_plus10",
            label="突破MA60 +10%",
            type="sell",
            priority=4,
            condition=(
                "deviation >= 0.10 "
                "and prev_deviation is not None "
                "and prev_deviation < 0.10 "
                "and shares > 0"
            ),
            action_fraction=0.25,
            action_min=2500,
            action_max=10000,
            budget_pool="sell",
            reset_when=sell_reset,
        ),
        Rule(
            id="sell_plus15",
            label="突破MA60 +15%",
            type="sell",
            priority=5,
            condition=(
                "deviation >= 0.15 "
                "and prev_deviation is not None "
                "and prev_deviation < 0.15 "
                "and shares > 0"
            ),
            action_fraction=0.25,
            action_min=2500,
            action_max=10000,
            budget_pool="sell",
            reset_when=sell_reset,
        ),
    ]
