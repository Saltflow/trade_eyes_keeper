"""
抗硬编码测试工具模块
提供随机化参数生成和验证工具
"""
import random
from datetime import datetime, time, timedelta
from unittest.mock import patch
import pytz


class RandomTestParameterGenerator:
    """随机化测试参数生成器"""
    
    def __init__(self, seed=None):
        """
        初始化随机生成器
        
        Args:
            seed: 随机种子，用于可重复测试
        """
        if seed is not None:
            random.seed(seed)
    
    def random_stock_code(self, exclude_etfs=False):
        """
        随机股票代码生成
        
        Args:
            exclude_etfs: 是否排除ETF代码（510880, 512810）
            
        Returns:
            str: 随机股票代码
        """
        # 从当前配置加载股票列表（这里简化处理）
        stocks = ['601728', '600938', '601985', '601919', '600795',
                  '601398', '601088', '601818', '601390']
        
        if not exclude_etfs:
            stocks.extend(['510880', '512810'])
            
        return random.choice(stocks)
    
    def random_time_point(self, start_hour=9, end_hour=16):
        """
        随机交易时间点生成
        
        Args:
            start_hour: 开始小时（默认9点）
            end_hour: 结束小时（默认16点）
            
        Returns:
            datetime.time: 随机时间点
        """
        hour = random.randint(start_hour, end_hour)
        minute = random.randint(0, 59)
        return time(hour, minute)
    
    def random_datetime(self, date=None, hour_range=(9, 16)):
        """
        随机日期时间生成
        
        Args:
            date: 指定日期（默认为今天）
            hour_range: 小时范围元组
            
        Returns:
            datetime: 随机日期时间（带时区）
        """
        if date is None:
            date = datetime.now().date()
            
        hour = random.randint(*hour_range)
        minute = random.randint(0, 59)
        second = random.randint(0, 59)
        
        dt = datetime(date.year, date.month, date.day, hour, minute, second)
        return pytz.timezone('Asia/Shanghai').localize(dt)
    
    def random_price_data(self, base_price=None):
        """
        生成随机但合理的股价数据
        
        Args:
            base_price: 基础价格（默认随机5-50元）
            
        Returns:
            dict: 包含open, close, high, low, volume, amount
        """
        if base_price is None:
            base_price = random.uniform(5.0, 50.0)
            
        open_price = base_price
        # 日内波动：收盘价在开盘价±5%范围内
        close_price = open_price * random.uniform(0.95, 1.05)
        # 最高价：不低于开盘价和收盘价
        high_price = max(open_price, close_price) * random.uniform(1.0, 1.05)
        # 最低价：不高于开盘价和收盘价  
        low_price = min(open_price, close_price) * random.uniform(0.95, 1.0)
        
        # 确保价格关系: low ≤ close ≤ high, low ≤ open ≤ high
        low_price = min(low_price, open_price, close_price, high_price)
        high_price = max(high_price, open_price, close_price, low_price)
        
        # 成交量和成交额
        volume = random.randint(1000000, 100000000)
        amount = round(volume * ((open_price + close_price) / 2), 2)
        
        return {
            'open': round(open_price, 2),
            'close': round(close_price, 2),
            'high': round(high_price, 2),
            'low': round(low_price, 2),
            'volume': volume,
            'amount': amount
        }
    
    def random_dividend_data(self, close_price):
        """
        生成随机但合理的股息数据
        
        Args:
            close_price: 收盘价用于计算股息率
            
        Returns:
            dict: 包含dividend_per_share, dividend_yield
        """
        # 合理股息率范围：0.5% - 20%
        dividend_yield = random.uniform(0.5, 20.0)
        dividend_per_share = round(close_price * dividend_yield / 100, 3)
        
        return {
            'dividend_per_share': dividend_per_share,
            'dividend_yield': round(dividend_yield, 2)
        }
    
    def random_financial_metrics(self):
        """
        生成随机但合理的财务指标
        
        Returns:
            dict: 包含pe_ratio, pb_ratio, roe, debt_ratio
        """
        # PE范围：5-100
        pe_ratio = random.uniform(5.0, 100.0)
        # PB范围：0.5-10
        pb_ratio = random.uniform(0.5, 10.0)
        # ROE范围：-20%到50%
        roe = random.uniform(-20.0, 50.0)
        # 负债率范围：20%-80%
        debt_ratio = random.uniform(20.0, 80.0)
        
        return {
            'pe_ratio': round(pe_ratio, 2),
            'pb_ratio': round(pb_ratio, 2),
            'roe': round(roe, 2),
            'debt_ratio': round(debt_ratio, 2)
        }


def mock_datetime_context(year=2026, month=3, day=21, hour=15, minute=30, second=0):
    """
    创建模拟datetime.now()的上下文管理器
    
    Args:
        year, month, day, hour, minute, second: 模拟的时间参数
        
    Returns:
        context manager: 用于with语句的上下文管理器
    """
    mock_now = datetime(year, month, day, hour, minute, second)
    tz_now = pytz.timezone('Asia/Shanghai').localize(mock_now)
    
    class MockDateTime:
        @classmethod
        def now(cls, tz=None):
            if tz:
                return tz_now.astimezone(tz)
            return mock_now
    
    return patch('datetime.datetime', MockDateTime)


def verify_price_relationships(price_data):
    """
    验证价格关系恒等式：low ≤ close ≤ high
    
    Args:
        price_data: 包含open, close, high, low的字典
        
    Returns:
        tuple: (是否通过, 错误信息)
    """
    try:
        low = price_data['low']
        close = price_data['close']
        high = price_data['high']
        
        if not (low <= close <= high):
            return False, f"价格关系不满足: low={low} ≤ close={close} ≤ high={high}"
            
        return True, "价格关系验证通过"
    except KeyError as e:
        return False, f"缺少必要价格字段: {e}"


def calculate_dividend_yield(dividend_per_share, close_price):
    """
    计算股息率：dividend_yield = (dividend_per_share / close_price) * 100
    
    Args:
        dividend_per_share: 每股股息
        close_price: 收盘价
        
    Returns:
        float: 股息率百分比
    """
    if close_price == 0:
        return None
    return (dividend_per_share / close_price) * 100


def verify_roe_consistency(pe_ratio, pb_ratio, roe, tolerance=5.0):
    """
    验证ROE一致性：|roe - (pb/pe)*100| ≤ tolerance
    
    Args:
        pe_ratio: 市盈率
        pb_ratio: 市净率
        roe: 净资产收益率
        tolerance: 容差百分比
        
    Returns:
        tuple: (是否通过, 错误信息)
    """
    if pe_ratio == 0:
        return False, "PE比率不能为零"
    
    roe_calculated = (pb_ratio / pe_ratio) * 100
    difference = abs(roe - roe_calculated)
    
    if difference <= tolerance:
        return True, f"ROE一致性验证通过: 差异{difference:.2f}% ≤ {tolerance}%"
    else:
        return False, f"ROE不一致: 计算值{roe_calculated:.2f}% vs 报告值{roe}% (差异{difference:.2f}%)"


if __name__ == "__main__":
    # 模块自测
    gen = RandomTestParameterGenerator(seed=42)
    
    print("随机股票代码:", gen.random_stock_code())
    print("随机时间点:", gen.random_time_point())
    
    price_data = gen.random_price_data()
    print("随机价格数据:", price_data)
    print("价格关系验证:", verify_price_relationships(price_data))
    
    dividend_data = gen.random_dividend_data(price_data['close'])
    print("随机股息数据:", dividend_data)
    print("股息率计算:", calculate_dividend_yield(
        dividend_data['dividend_per_share'], price_data['close']))
    
    financial_data = gen.random_financial_metrics()
    print("随机财务指标:", financial_data)
    print("ROE一致性验证:", verify_roe_consistency(
        financial_data['pe_ratio'], 
        financial_data['pb_ratio'], 
        financial_data['roe']))