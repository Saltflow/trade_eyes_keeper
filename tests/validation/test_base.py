"""
抗硬编码测试基类
所有抗硬编码测试应继承此类以确保一致性
"""
import random
import pytest
from datetime import datetime
from .test_utils import RandomTestParameterGenerator


class BaseAntiHardcodeTest:
    """抗硬编码测试基类"""
    
    def __init__(self, seed=None):
        """
        初始化测试基类
        
        Args:
            seed: 随机种子，确保测试可重复
        """
        self.random_generator = RandomTestParameterGenerator(seed)
        self.test_seed = seed or random.randint(1, 10000)
        random.seed(self.test_seed)
        
    def setup_method(self, method):
        """每个测试方法前执行"""
        # 重置随机种子，确保测试可重复
        if self.test_seed:
            random.seed(self.test_seed)
            
    def assert_price_relationships(self, price_data, allow_equality=True):
        """
        断言价格关系恒等式：low ≤ close ≤ high
        
        Args:
            price_data: 包含open, close, high, low的字典
            allow_equality: 是否允许相等（涨停/跌停情况）
        """
        low = price_data['low']
        close = price_data['close']
        high = price_data['high']
        
        if allow_equality:
            assert low <= close <= high, f"价格关系不满足: low={low} ≤ close={close} ≤ high={high}"
        else:
            assert low < close < high, f"价格关系不满足: low={low} < close={close} < high={high}"
    
    def assert_dividend_yield_formula(self, dividend_per_share, close_price, dividend_yield, tolerance=0.01):
        """
        断言股息率公式：dividend_yield = (dividend_per_share / close_price) * 100
        
        Args:
            dividend_per_share: 每股股息
            close_price: 收盘价
            dividend_yield: 报告的股息率
            tolerance: 计算容差
        """
        if close_price == 0:
            pytest.skip("收盘价为0，无法计算股息率")
            
        calculated_yield = (dividend_per_share / close_price) * 100
        difference = abs(calculated_yield - dividend_yield)
        
        assert difference <= tolerance, (
            f"股息率公式不成立: "
            f"计算值={calculated_yield:.2f}% vs 报告值={dividend_yield:.2f}% "
            f"(差异={difference:.2f}% > {tolerance}%)"
        )
    
    def assert_roe_consistency(self, pe_ratio, pb_ratio, roe, tolerance=5.0):
        """
        断言ROE一致性：|roe - (pb/pe)*100| ≤ tolerance
        
        Args:
            pe_ratio: 市盈率
            pb_ratio: 市净率
            roe: 净资产收益率
            tolerance: 容差百分比
        """
        if pe_ratio == 0:
            pytest.skip("PE比率为0，无法计算ROE")
            
        roe_calculated = (pb_ratio / pe_ratio) * 100
        difference = abs(roe - roe_calculated)
        
        assert difference <= tolerance, (
            f"ROE不一致: "
            f"计算值={roe_calculated:.2f}% vs 报告值={roe:.2f}% "
            f"(差异={difference:.2f}% > {tolerance}%)"
        )
    
    def assert_ma60_calculation(self, close_prices, ma60_values, tolerance=0.01):
        """
        断言MA60计算正确
        
        Args:
            close_prices: 收盘价列表（至少60个）
            ma60_values: MA60值列表
            tolerance: 计算容差
        """
        if len(close_prices) < 60:
            pytest.skip("数据不足60个，无法计算MA60")
            
        for i in range(59, len(close_prices)):
            window = close_prices[i-59:i+1]
            calculated_ma60 = sum(window) / 60
            reported_ma60 = ma60_values[i]
            
            difference = abs(calculated_ma60 - reported_ma60)
            assert difference <= tolerance, (
                f"MA60计算错误 at index {i}: "
                f"计算值={calculated_ma60:.2f} vs 报告值={reported_ma60:.2f} "
                f"(差异={difference:.2f} > {tolerance})"
            )
    
    def assert_amplitude_formula(self, open_price, high_price, low_price, amplitude, tolerance=0.01):
        """
        断言振幅公式：amplitude = (high - low) / open * 100
        
        Args:
            open_price: 开盘价
            high_price: 最高价
            low_price: 最低价
            amplitude: 报告的振幅
            tolerance: 计算容差
        """
        if open_price == 0:
            pytest.skip("开盘价为0，无法计算振幅")
            
        calculated_amplitude = (high_price - low_price) / open_price * 100
        difference = abs(calculated_amplitude - amplitude)
        
        assert difference <= tolerance, (
            f"振幅公式不成立: "
            f"计算值={calculated_amplitude:.2f}% vs 报告值={amplitude:.2f}% "
            f"(差异={difference:.2f}% > {tolerance}%)"
        )
    
    def assert_daily_change_formula(self, open_price, close_price, change, tolerance=0.01):
        """
        断言日变化公式：change = close - open
        
        Args:
            open_price: 开盘价
            close_price: 收盘价
            change: 报告的变化值
            tolerance: 计算容差
        """
        calculated_change = close_price - open_price
        difference = abs(calculated_change - change)
        
        assert difference <= tolerance, (
            f"日变化公式不成立: "
            f"计算值={calculated_change:.2f} vs 报告值={change:.2f} "
            f"(差异={difference:.2f} > {tolerance})"
        )
    
    def assert_change_pct_formula(self, prev_close, close_price, change_pct, tolerance=0.01):
        """
        断言涨跌幅公式：change_pct = (close - prev_close) / prev_close * 100
        
        Args:
            prev_close: 前收盘价
            close_price: 收盘价
            change_pct: 报告的涨跌幅
            tolerance: 计算容差
        """
        if prev_close == 0:
            pytest.skip("前收盘价为0，无法计算涨跌幅")
            
        calculated_change_pct = (close_price - prev_close) / prev_close * 100
        difference = abs(calculated_change_pct - change_pct)
        
        assert difference <= tolerance, (
            f"涨跌幅公式不成立: "
            f"计算值={calculated_change_pct:.2f}% vs 报告值={change_pct:.2f}% "
            f"(差异={difference:.2f}% > {tolerance}%)"
        )
    
    def run_randomized_test(self, test_func, iterations=10, description=""):
        """
        运行随机化测试
        
        Args:
            test_func: 测试函数，接受随机生成器作为参数
            iterations: 迭代次数
            description: 测试描述
            
        Returns:
            bool: 是否所有迭代都通过
        """
        successes = 0
        failures = []
        
        for i in range(iterations):
            try:
                # 每次迭代使用不同的随机种子
                iteration_seed = self.test_seed + i if self.test_seed else None
                gen = RandomTestParameterGenerator(iteration_seed)
                test_func(gen)
                successes += 1
            except AssertionError as e:
                failures.append(f"迭代 {i+1}: {str(e)}")
            except Exception as e:
                failures.append(f"迭代 {i+1} 异常: {str(e)}")
        
        if failures:
            pytest.fail(f"{description} 随机化测试失败 ({successes}/{iterations} 通过):\n" + "\n".join(failures))
        
        return True


@pytest.fixture
def anti_hardcode_base():
    """提供抗硬编码测试基类实例的fixture"""
    return BaseAntiHardcodeTest(seed=random.randint(1, 10000))


@pytest.fixture
def seeded_anti_hardcode_base():
    """提供固定种子的抗硬编码测试基类实例（用于可重复测试）"""
    return BaseAntiHardcodeTest(seed=42)


class TestBaseAntiHardcodeTest:
    """抗硬编码测试基类的单元测试"""
    
    def test_price_relationships(self, anti_hardcode_base):
        """测试价格关系断言"""
        # 有效数据
        valid_data = {'low': 10.0, 'close': 10.5, 'high': 11.0}
        anti_hardcode_base.assert_price_relationships(valid_data)
        
        # 相等情况（涨停/跌停）
        equal_data = {'low': 10.0, 'close': 10.0, 'high': 10.0}
        anti_hardcode_base.assert_price_relationships(equal_data, allow_equality=True)
        
        # 无效数据应失败
        invalid_data = {'low': 11.0, 'close': 10.5, 'high': 10.0}
        try:
            anti_hardcode_base.assert_price_relationships(invalid_data)
            assert False, "应抛出断言错误"
        except AssertionError:
            pass  # 预期行为
    
    def test_dividend_yield_formula(self, anti_hardcode_base):
        """测试股息率公式断言"""
        anti_hardcode_base.assert_dividend_yield_formula(
            dividend_per_share=1.0,
            close_price=20.0,
            dividend_yield=5.0  # 1.0 / 20.0 * 100 = 5.0
        )
        
        # 微小差异应在容差内
        anti_hardcode_base.assert_dividend_yield_formula(
            dividend_per_share=1.0,
            close_price=20.0,
            dividend_yield=5.01,
            tolerance=0.1
        )
    
    def test_roe_consistency(self, anti_hardcode_base):
        """测试ROE一致性断言"""
        # PE=10, PB=2, ROE应为20%
        anti_hardcode_base.assert_roe_consistency(
            pe_ratio=10.0,
            pb_ratio=2.0,
            roe=20.0,
            tolerance=0.1
        )
        
        # 微小差异应在容差内
        anti_hardcode_base.assert_roe_consistency(
            pe_ratio=10.0,
            pb_ratio=2.0,
            roe=20.5,
            tolerance=5.0
        )