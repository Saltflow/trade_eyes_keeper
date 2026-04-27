"""
规则引擎测试

表达式引擎 + 规则定义 + 规则引擎评估
"""

import pytest
from src.analysis.rule_engine import ExpressionEngine, Rule, RuleEngine


# ════════════════════════════════════════════════════════
# ExpressionEngine 测试
# ════════════════════════════════════════════════════════

class TestExpressionEngine:
    """表达式求值器测试"""

    def test_simple_arithmetic(self):
        ctx = {"a": 3, "b": 4}
        assert ExpressionEngine.evaluate("a + b", ctx) == 7
        assert ExpressionEngine.evaluate("a * b", ctx) == 12
        assert ExpressionEngine.evaluate("b / a", ctx) == pytest.approx(1.333, 0.01)
        assert ExpressionEngine.evaluate("a - b", ctx) == -1

    def test_comparisons(self):
        ctx = {"x": 5, "y": 10}
        assert ExpressionEngine.evaluate("x < y", ctx) is True
        assert ExpressionEngine.evaluate("x > y", ctx) is False
        assert ExpressionEngine.evaluate("x <= 5", ctx) is True
        assert ExpressionEngine.evaluate("x >= 6", ctx) is False
        assert ExpressionEngine.evaluate("x == 5", ctx) is True
        assert ExpressionEngine.evaluate("x != 5", ctx) is False

    def test_builtin_functions(self):
        ctx = {"cash": 3000, "shares": 100}
        assert ExpressionEngine.evaluate("min(5000, cash)", ctx) == 3000
        assert ExpressionEngine.evaluate("min(5000, cash)", {"cash": 8000}) == 5000
        assert ExpressionEngine.evaluate("max(0, -5)", ctx) == 0
        assert ExpressionEngine.evaluate("abs(-3.5)", ctx) == 3.5
        assert ExpressionEngine.evaluate("round(3.14159, 2)", ctx) == 3.14

    def test_boolean_logic(self):
        ctx = {"a": True, "b": False}
        assert ExpressionEngine.evaluate("a and b", ctx) is False
        assert ExpressionEngine.evaluate("a or b", ctx) is True
        assert ExpressionEngine.evaluate("not a", ctx) is False
        assert ExpressionEngine.evaluate("a and not b", ctx) is True

    def test_none_handling(self):
        assert ExpressionEngine.evaluate("x is None", {"x": None}) is True
        assert ExpressionEngine.evaluate("x is not None", {"x": 5}) is True
        assert ExpressionEngine.evaluate("x is not None", {"x": None}) is False

    def test_float_comparisons(self):
        ctx = {"deviation": -0.052, "threshold": -0.05}
        assert ExpressionEngine.evaluate("deviation <= threshold", ctx) is True

    def test_unknown_variable_raises(self):
        with pytest.raises((NameError, Exception)):
            ExpressionEngine.evaluate("undefined_var > 0", {})

    def test_syntax_error_raises(self):
        with pytest.raises((SyntaxError, Exception)):
            ExpressionEngine.evaluate("x > > 5", {"x": 3})

    def test_no_builtins_leak(self):
        """不能调用危险的内置函数"""
        with pytest.raises((NameError, Exception)):
            ExpressionEngine.evaluate("__import__('os').system('ls')", {})


# ════════════════════════════════════════════════════════
# Rule 测试
# ════════════════════════════════════════════════════════

class TestRule:
    """规则数据类测试"""

    def test_rule_from_dict(self):
        rule = Rule.from_dict(
            {
                "id": "test_buy",
                "label": "测试买入",
                "type": "buy",
                "priority": 1,
                "condition": "deviation <= -0.05",
                "action_amount": "min(5000, cash)",
                "budget_pool": "buy",
                "reset_when": "deviation > 0",
            }
        )
        assert rule.id == "test_buy"
        assert rule.type == "buy"
        assert rule.priority == 1
        assert rule.budget_pool == "buy"

    def test_rule_defaults(self):
        rule = Rule.from_dict(
            {
                "id": "minimal",
                "type": "buy",
                "condition": "x > 0",
                "action_amount": "1000",
                "budget_pool": "buy",
            }
        )
        assert rule.label == "minimal"
        assert rule.priority == 0
        assert rule.reset_when is None

    def test_rule_sell_type(self):
        rule = Rule.from_dict(
            {
                "id": "test_sell",
                "type": "sell",
                "condition": "deviation >= 0.05",
                "action_fraction": 0.25,
                "action_min": 2500,
                "action_max": 10000,
                "budget_pool": "sell",
            }
        )
        assert rule.type == "sell"
        assert rule.action_fraction == 0.25
        assert rule.action_min == 2500
        assert rule.action_max == 10000

    def test_rule_from_dict_missing_required(self):
        with pytest.raises(ValueError):
            Rule.from_dict({"id": "bad"})  # 缺少 condition


# ════════════════════════════════════════════════════════
# RuleEngine 测试
# ════════════════════════════════════════════════════════

class TestRuleEngine:
    """规则引擎核心测试"""

    def _make_ctx(self, **overrides):
        """构建典型上下文"""
        base = {
            "close": 10.0,
            "ma60": 10.0,
            "deviation": 0.0,
            "prev_deviation": None,
            "cash": 10000.0,
            "shares": 0,
            "position_value": 0.0,
            "monthly_buy_used": 0.0,
            "monthly_sell_used": 0.0,
            "monthly_buy_limit": 5000.0,
            "monthly_sell_limit": 5000.0,
            "lot_size": 100,
            "commission_rate": 0.002,
        }
        base.update(overrides)
        return base

    # ── 单规则 ──

    def test_single_rule_condition_met(self):
        """条件满足时触发"""
        rules = [
            Rule("buy_1", "buy跌破-5%", "buy", 1,
                 "deviation <= -0.05 and prev_deviation is not None "
                 "and prev_deviation > -0.05",
                 action_amount="min(5000, cash)",
                 budget_pool="buy")
        ]
        engine = RuleEngine(rules)
        ctx = self._make_ctx(deviation=-0.06, prev_deviation=-0.02)
        results = engine.evaluate_day(ctx)
        assert len(results) == 1
        assert results[0][0].id == "buy_1"

    def test_single_rule_condition_not_met(self):
        """条件不满足时不触发"""
        rules = [
            Rule("buy_1", "", "buy", 1,
                 "deviation <= -0.05",
                 action_amount="min(5000, cash)",
                 budget_pool="buy")
        ]
        engine = RuleEngine(rules)
        ctx = self._make_ctx(deviation=-0.02)  # 未跌破-5%
        results = engine.evaluate_day(ctx)
        assert len(results) == 0

    # ── 触发后锁定 ──

    def test_rule_locks_after_trigger(self):
        """规则触发后，后续同条件不再触发"""
        rules = [
            Rule("buy_1", "", "buy", 1,
                 "deviation <= -0.05",
                 action_amount="min(5000, cash)",
                 budget_pool="buy")
        ]
        engine = RuleEngine(rules)
        ctx = self._make_ctx(deviation=-0.06, prev_deviation=-0.02)

        # 第一次触发
        r1 = engine.evaluate_day(ctx)
        assert len(r1) == 1

        # 第二次（同条件）不再触发
        r2 = engine.evaluate_day(ctx)
        assert len(r2) == 0

    # ── 重置条件 ──

    def test_rule_reset_re_enables(self):
        """满足重置条件后，规则重新可用"""
        rules = [
            Rule("buy_1", "", "buy", 1,
                 "deviation <= -0.05",
                 action_amount="min(5000, cash)",
                 budget_pool="buy",
                 reset_when="deviation > 0")
        ]
        engine = RuleEngine(rules)

        # 触发
        ctx = self._make_ctx(deviation=-0.06, prev_deviation=-0.02)
        assert len(engine.evaluate_day(ctx)) == 1

        # 同一条件不触发
        assert len(engine.evaluate_day(ctx)) == 0

        # 满足重置条件
        ctx_reset = self._make_ctx(deviation=0.02, prev_deviation=-0.06)
        assert len(engine.evaluate_day(ctx_reset)) == 0  # 只重置，不触发

        # 再次满足触发条件
        ctx_again = self._make_ctx(deviation=-0.06, prev_deviation=0.02)
        assert len(engine.evaluate_day(ctx_again)) == 1  # 重新触发！

    # ── 优先级 ──

    def test_priority_ordering(self):
        """高优先级规则先执行"""
        rules = [
            Rule("low", "", "buy", 10,
                 "deviation <= -0.05",
                 action_amount="100",
                 budget_pool="buy"),
            Rule("high", "", "buy", 1,
                 "deviation <= -0.05",
                 action_amount="500",
                 budget_pool="buy"),
        ]
        engine = RuleEngine(rules)
        ctx = self._make_ctx(deviation=-0.06, prev_deviation=-0.02)
        results = engine.evaluate_day(ctx)
        # 两个都触发（独立锁），但 high 先
        assert len(results) >= 1
        assert results[0][0].id == "high"

    # ── buy-only (无卖出规则) ──

    def test_buy_only_no_sell(self):
        """只有买入规则时绝不卖出"""
        rules = [
            Rule("buy_1", "", "buy", 1,
                 "deviation <= -0.05 and prev_deviation is not None "
                 "and prev_deviation > -0.05",
                 action_amount="min(5000, cash)",
                 budget_pool="buy")
        ]
        engine = RuleEngine(rules)

        # 买入条件满足
        ctx_buy = self._make_ctx(deviation=-0.06, prev_deviation=-0.02)
        results = engine.evaluate_day(ctx_buy)
        assert len(results) == 1
        assert results[0][0].type == "buy"

        # 卖出条件满足但没有卖出规则 → 不触发
        ctx_sell = self._make_ctx(deviation=0.06, prev_deviation=0.02,
                                  shares=100, position_value=1000)
        results2 = engine.evaluate_day(ctx_sell)
        assert len(results2) == 0

    # ── 多规则同时触发 ──

    def test_multiple_rules_same_day(self):
        """同一天多条规则命中，全部返回"""
        rules = [
            Rule("buy_m5", "", "buy", 1,
                 "deviation <= -0.05",
                 action_amount="min(5000, cash)",
                 budget_pool="buy"),
            Rule("buy_m10", "", "buy", 2,
                 "deviation <= -0.10",
                 action_amount="min(5000, cash)",
                 budget_pool="buy"),
        ]
        engine = RuleEngine(rules)
        # 跌到-12%，两条都触发
        ctx = self._make_ctx(deviation=-0.12, prev_deviation=-0.02)
        results = engine.evaluate_day(ctx)
        assert len(results) == 2
        ids = {r[0].id for r in results}
        assert ids == {"buy_m5", "buy_m10"}


# ════════════════════════════════════════════════════════
# 默认规则集测试
# ════════════════════════════════════════════════════════

class TestDefaultRules:
    """默认规则（与当前硬编码行为一致）"""

    def test_default_rules_exist(self):
        from src.analysis.rule_engine import get_default_rules
        rules = get_default_rules()
        assert len(rules) == 5  # 2 buy + 3 sell
        types = {r.type for r in rules}
        assert "buy" in types
        assert "sell" in types

    def test_default_buy_minus5_trigger(self):
        """跌破-5%时触发默认买入"""
        from src.analysis.rule_engine import get_default_rules
        rules = get_default_rules()
        engine = RuleEngine(rules)

        ctx = {
            "close": 9.5, "ma60": 10.0, "deviation": -0.05,
            "prev_deviation": -0.02, "cash": 10000, "shares": 0,
            "position_value": 0, "monthly_buy_used": 0,
            "monthly_sell_used": 0, "monthly_buy_limit": 15000,
            "monthly_sell_limit": 15000, "lot_size": 100,
            "commission_rate": 0.002,
        }
        results = engine.evaluate_day(ctx)
        buy_results = [r for r in results if r[0].type == "buy"]
        assert len(buy_results) >= 1

    def test_default_sell_plus5_trigger(self):
        """突破+5%时触发默认卖出（有持仓）"""
        from src.analysis.rule_engine import get_default_rules
        rules = get_default_rules()
        engine = RuleEngine(rules)

        ctx = {
            "close": 10.5, "ma60": 10.0, "deviation": 0.05,
            "prev_deviation": 0.02, "cash": 5000, "shares": 200,
            "position_value": 2100, "monthly_buy_used": 0,
            "monthly_sell_used": 0, "monthly_buy_limit": 15000,
            "monthly_sell_limit": 15000, "lot_size": 100,
            "commission_rate": 0.002,
        }
        results = engine.evaluate_day(ctx)
        sell_results = [r for r in results if r[0].type == "sell"]
        assert len(sell_results) >= 1
