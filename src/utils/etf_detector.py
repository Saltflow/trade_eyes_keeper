"""
ETF检测工具
用于识别中国市场的ETF基金代码
"""

import logging

logger = logging.getLogger(__name__)


def is_etf(stock_code: str) -> bool:
    """
    判断给定的股票代码是否为ETF基金

    Args:
        stock_code: 股票代码字符串

    Returns:
        bool: True如果是ETF，否则False
    """
    if not isinstance(stock_code, str):
        stock_code = str(stock_code)

    # 中国ETF代码常见前缀
    # 51开头: 上海ETF (如510880, 510300)
    # 52开头: 上海ETF (如512810)
    # 15开头: 深圳ETF (如159915, 159919)
    # 16开头: 深圳ETF (如161725)
    # 18开头: 深圳ETF (如180603)
    # 58开头: 科创板ETF (如588000)
    # 还有其他如508091, 513910等

    etf_prefixes = ("51", "52", "15", "16", "18", "58")

    # 检查是否是ETF代码
    if stock_code.startswith(etf_prefixes):
        # 进一步验证：ETF代码通常是6位数字
        if stock_code.isdigit() and len(stock_code) == 6:
            return True

    # 特殊ETF代码列表（非标准前缀但仍然可能是ETF）
    special_etfs = {"508091", "513910", "588000"}
    if stock_code in special_etfs:
        return True

    return False


def get_etf_adjustment_type(stock_code: str) -> str:
    """
    获取ETF的复权类型建议

    Args:
        stock_code: ETF代码

    Returns:
        str: 建议的复权类型:
            - "qfq": 前复权（股票标准）
            - "hfq": 后复权
            - "none": 不复权（部分ETF可能不支持复权）
    """
    if not is_etf(stock_code):
        return "qfq"  # 股票默认使用前复权

    # 目前先返回前复权，后续根据测试结果调整
    return "qfq"


if __name__ == "__main__":
    # 测试代码
    test_codes = [
        "510880",  # 华泰柏瑞红利ETF
        "512810",  # 华宝中证军工ETF
        "159915",  # 易方达创业板ETF
        "161725",  # 招商中证白酒指数
        "180603",  # 某ETF
        "588000",  # 科创板ETF
        "508091",  # 特殊ETF
        "513910",  # 特殊ETF
        "600036",  # 招商银行（非ETF）
        "000001",  # 平安银行（非ETF）
    ]

    print("ETF检测测试:")
    for code in test_codes:
        result = is_etf(code)
        print(f"  {code}: {'ETF' if result else '股票'}")
