"""
数学恒等式验证工具
提供股票数据相关的数学验证功能
"""
import math


class MathIdentityValidator:
    """数学恒等式验证器"""
    
    @staticmethod
    def validate_price_relationships(price_data, allow_equality=True):
        """
        验证价格关系恒等式：low ≤ close ≤ high
        
        Args:
            price_data: 包含low, close, high价格的字典
            allow_equality: 是否允许相等（涨停/跌停情况）
            
        Returns:
            tuple: (是否通过, 错误信息)
        """
        try:
            low = float(price_data['low'])
            close = float(price_data['close'])
            high = float(price_data['high'])
            
            if allow_equality:
                if not (low <= close <= high):
                    return False, f"价格关系不满足: low={low} ≤ close={close} ≤ high={high}"
            else:
                if not (low < close < high):
                    return False, f"价格关系不满足: low={low} < close={close} < high={high}"
                    
            return True, "价格关系验证通过"
        except KeyError as e:
            return False, f"缺少必要价格字段: {e}"
        except (ValueError, TypeError) as e:
            return False, f"价格数据格式错误: {e}"
    
    @staticmethod
    def validate_dividend_yield(dividend_per_share, close_price, reported_yield, tolerance=0.01):
        """
        验证股息率公式：dividend_yield = (dividend_per_share / close_price) * 100
        
        Args:
            dividend_per_share: 每股股息
            close_price: 收盘价
            reported_yield: 报告的股息率百分比
            tolerance: 容差百分比
            
        Returns:
            tuple: (是否通过, 错误信息, 计算值)
        """
        try:
            dividend = float(dividend_per_share)
            close = float(close_price)
            reported = float(reported_yield)
            
            if close == 0:
                return False, "收盘价为0，无法计算股息率", None
                
            calculated_yield = (dividend / close) * 100
            difference = abs(calculated_yield - reported)
            
            if difference <= tolerance:
                return True, f"股息率公式验证通过: 差异{difference:.4f}% ≤ {tolerance}%", calculated_yield
            else:
                return False, f"股息率公式不成立: 计算值={calculated_yield:.4f}% vs 报告值={reported:.4f}% (差异={difference:.4f}% > {tolerance}%)", calculated_yield
        except (ValueError, TypeError) as e:
            return False, f"股息率数据格式错误: {e}", None
    
    @staticmethod
    def validate_roe_consistency(pe_ratio, pb_ratio, reported_roe, tolerance=5.0):
        """
        验证ROE一致性：|roe - (pb/pe)*100| ≤ tolerance
        
        Args:
            pe_ratio: 市盈率
            pb_ratio: 市净率
            reported_roe: 报告的净资产收益率百分比
            tolerance: 容差百分比
            
        Returns:
            tuple: (是否通过, 错误信息, 计算值)
        """
        try:
            pe = float(pe_ratio)
            pb = float(pb_ratio)
            roe = float(reported_roe)
            
            if pe == 0:
                return False, "PE比率为0，无法计算ROE", None
                
            roe_calculated = (pb / pe) * 100
            difference = abs(roe - roe_calculated)
            
            if difference <= tolerance:
                return True, f"ROE一致性验证通过: 差异{difference:.4f}% ≤ {tolerance}%", roe_calculated
            else:
                return False, f"ROE不一致: 计算值={roe_calculated:.4f}% vs 报告值={roe:.4f}% (差异={difference:.4f}% > {tolerance}%)", roe_calculated
        except (ValueError, TypeError) as e:
            return False, f"财务指标数据格式错误: {e}", None
    
    @staticmethod
    def validate_ma60_calculation(close_prices, ma60_values, tolerance=0.01):
        """
        验证MA60计算：ma60 = 前60日收盘价的平均值
        
        Args:
            close_prices: 收盘价列表（至少60个）
            ma60_values: MA60值列表
            tolerance: 计算容差
            
        Returns:
            tuple: (是否通过, 错误信息列表)
        """
        try:
            closes = [float(p) for p in close_prices]
            ma60s = [float(m) for m in ma60_values]
            
            if len(closes) < 60:
                return False, ["数据不足60个，无法计算MA60"], []
                
            if len(closes) != len(ma60s):
                return False, [f"数据长度不匹配: 收盘价{len(closes)}个, MA60值{len(ma60s)}个"], []
            
            errors = []
            for i in range(59, len(closes)):
                window = closes[i-59:i+1]
                calculated_ma60 = sum(window) / 60
                reported_ma60 = ma60s[i]
                
                difference = abs(calculated_ma60 - reported_ma60)
                if difference > tolerance:
                    errors.append(
                        f"索引{i}: 计算值={calculated_ma60:.4f} vs 报告值={reported_ma60:.4f} "
                        f"(差异={difference:.4f} > {tolerance})"
                    )
            
            if errors:
                return False, errors, []
            else:
                return True, ["MA60计算验证通过"], []
        except (ValueError, TypeError) as e:
            return False, [f"MA60数据格式错误: {e}"], []
    
    @staticmethod
    def validate_amplitude(open_price, high_price, low_price, reported_amplitude, tolerance=0.01):
        """
        验证振幅公式：amplitude = (high - low) / open * 100
        
        Args:
            open_price: 开盘价
            high_price: 最高价
            low_price: 最低价
            reported_amplitude: 报告的振幅百分比
            tolerance: 容差百分比
            
        Returns:
            tuple: (是否通过, 错误信息, 计算值)
        """
        try:
            open_p = float(open_price)
            high = float(high_price)
            low = float(low_price)
            amplitude = float(reported_amplitude)
            
            if open_p == 0:
                return False, "开盘价为0，无法计算振幅", None
                
            calculated_amplitude = (high - low) / open_p * 100
            difference = abs(calculated_amplitude - amplitude)
            
            if difference <= tolerance:
                return True, f"振幅公式验证通过: 差异{difference:.4f}% ≤ {tolerance}%", calculated_amplitude
            else:
                return False, f"振幅公式不成立: 计算值={calculated_amplitude:.4f}% vs 报告值={amplitude:.4f}% (差异={difference:.4f}% > {tolerance}%)", calculated_amplitude
        except (ValueError, TypeError) as e:
            return False, f"振幅数据格式错误: {e}", None
    
    @staticmethod
    def validate_daily_change(open_price, close_price, reported_change, tolerance=0.01):
        """
        验证日变化公式：change = close - open
        
        Args:
            open_price: 开盘价
            close_price: 收盘价
            reported_change: 报告的变化值
            tolerance: 计算容差
            
        Returns:
            tuple: (是否通过, 错误信息, 计算值)
        """
        try:
            open_p = float(open_price)
            close = float(close_price)
            change = float(reported_change)
            
            calculated_change = close - open_p
            difference = abs(calculated_change - change)
            
            if difference <= tolerance:
                return True, f"日变化公式验证通过: 差异{difference:.4f} ≤ {tolerance}", calculated_change
            else:
                return False, f"日变化公式不成立: 计算值={calculated_change:.4f} vs 报告值={change:.4f} (差异={difference:.4f} > {tolerance})", calculated_change
        except (ValueError, TypeError) as e:
            return False, f"日变化数据格式错误: {e}", None
    
    @staticmethod
    def validate_change_pct(prev_close, close_price, reported_change_pct, tolerance=0.01):
        """
        验证涨跌幅公式：change_pct = (close - prev_close) / prev_close * 100
        
        Args:
            prev_close: 前收盘价
            close_price: 收盘价
            reported_change_pct: 报告的涨跌幅百分比
            tolerance: 计算容差百分比
            
        Returns:
            tuple: (是否通过, 错误信息, 计算值)
        """
        try:
            prev = float(prev_close)
            close = float(close_price)
            change_pct = float(reported_change_pct)
            
            if prev == 0:
                return False, "前收盘价为0，无法计算涨跌幅", None
                
            calculated_change_pct = (close - prev) / prev * 100
            difference = abs(calculated_change_pct - change_pct)
            
            if difference <= tolerance:
                return True, f"涨跌幅公式验证通过: 差异{difference:.4f}% ≤ {tolerance}%", calculated_change_pct
            else:
                return False, f"涨跌幅公式不成立: 计算值={calculated_change_pct:.4f}% vs 报告值={change_pct:.4f}% (差异={difference:.4f}% > {tolerance}%)", calculated_change_pct
        except (ValueError, TypeError) as e:
            return False, f"涨跌幅数据格式错误: {e}", None
    
    @staticmethod
    def generate_test_cases(num_cases=10, seed=None):
        """
        生成随机测试用例
        
        Args:
            num_cases: 测试用例数量
            seed: 随机种子
            
        Returns:
            list: 测试用例字典列表
        """
        import random
        
        if seed is not None:
            random.seed(seed)
        
        test_cases = []
        for _ in range(num_cases):
            # 生成随机但合理的数据
            base_price = random.uniform(5.0, 50.0)
            
            # 价格数据
            open_price = base_price
            close_price = open_price * random.uniform(0.95, 1.05)
            high_price = max(open_price, close_price) * random.uniform(1.0, 1.05)
            low_price = min(open_price, close_price) * random.uniform(0.95, 1.0)
            
            # 确保价格关系
            low_price = min(low_price, open_price, close_price, high_price)
            high_price = max(high_price, open_price, close_price, low_price)
            
            # 财务指标
            pe_ratio = random.uniform(5.0, 100.0)
            pb_ratio = random.uniform(0.5, 10.0)
            roe = (pb_ratio / pe_ratio) * 100 if pe_ratio != 0 else random.uniform(-20.0, 50.0)
            
            # 股息数据
            dividend_yield = random.uniform(0.5, 20.0)
            dividend_per_share = close_price * dividend_yield / 100
            
            # 变化数据
            prev_close = open_price * random.uniform(0.95, 1.05)
            change = close_price - open_price
            change_pct = (close_price - prev_close) / prev_close * 100 if prev_close != 0 else 0
            
            # 振幅
            amplitude = (high_price - low_price) / open_price * 100 if open_price != 0 else 0
            
            test_case = {
                'price_data': {
                    'open': round(open_price, 2),
                    'close': round(close_price, 2),
                    'high': round(high_price, 2),
                    'low': round(low_price, 2)
                },
                'financial_data': {
                    'pe_ratio': round(pe_ratio, 2),
                    'pb_ratio': round(pb_ratio, 2),
                    'roe': round(roe, 2)
                },
                'dividend_data': {
                    'dividend_per_share': round(dividend_per_share, 3),
                    'dividend_yield': round(dividend_yield, 2)
                },
                'change_data': {
                    'prev_close': round(prev_close, 2),
                    'change': round(change, 2),
                    'change_pct': round(change_pct, 2),
                    'amplitude': round(amplitude, 2)
                },
                'expected_results': {
                    'price_relationships': True,
                    'dividend_yield': True,
                    'roe_consistency': True,
                    'amplitude': True,
                    'daily_change': True,
                    'change_pct': True
                }
            }
            
            test_cases.append(test_case)
        
        return test_cases


if __name__ == "__main__":
    # 模块自测
    validator = MathIdentityValidator()
    
    print("=== 数学恒等式验证工具自测 ===")
    
    # 测试价格关系验证
    price_data = {'low': 10.0, 'close': 10.5, 'high': 11.0}
    result, msg = validator.validate_price_relationships(price_data)
    print(f"价格关系验证: {result} - {msg}")
    
    # 测试股息率验证
    result, msg, calc = validator.validate_dividend_yield(1.0, 20.0, 5.0)
    print(f"股息率验证: {result} - {msg}")
    
    # 测试ROE一致性验证
    result, msg, calc = validator.validate_roe_consistency(10.0, 2.0, 20.0)
    print(f"ROE一致性验证: {result} - {msg}")
    
    # 测试振幅验证
    result, msg, calc = validator.validate_amplitude(10.0, 11.0, 9.5, 15.0)
    print(f"振幅验证: {result} - {msg}")
    
    # 生成测试用例
    test_cases = validator.generate_test_cases(num_cases=2, seed=42)
    print(f"生成{len(test_cases)}个测试用例")
    
    print("✅ 数学恒等式验证工具自测完成")