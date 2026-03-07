"""
条件检查模块
检查当天最低价 < MA60（前复权）条件
"""

import logging
import pandas as pd

logger = logging.getLogger(__name__)

class ConditionChecker:
    """条件检查器"""
    
    def __init__(self, config):
        """
        初始化条件检查器
        
        Args:
            config: 配置字典
        """
        self.config = config
    
    def check_condition(self, stock_data):
        """
        检查股票数据是否满足条件：当天最低价 < MA60
        
        Args:
            stock_data: 包含股票数据的DataFrame，需要包含'low', 'ma60'列
            
        Returns:
            list: 满足条件的股票代码列表
        """
        if stock_data.empty:
            logger.warning("股票数据为空，无法检查条件")
            return []
        
        alert_stocks = []
        
        for _, row in stock_data.iterrows():
            stock_code = row.get('stock_code', '')
            low_price = row.get('low')
            ma60 = row.get('ma60')
            
            if pd.isna(low_price) or pd.isna(ma60):
                logger.warning(f"股票 {stock_code} 数据不完整，跳过检查")
                continue
            
            # 检查条件：当天最低价 < MA60
            if low_price < ma60:
                alert_stocks.append({
                    'stock_code': stock_code,
                    'low_price': low_price,
                    'ma60': ma60,
                    'date': row.get('date'),
                    'condition': 'low < ma60',
                    'price_difference': ma60 - low_price,
                    'percentage_difference': (ma60 - low_price) / ma60 * 100
                })
                logger.info(f"股票 {stock_code} 满足条件: 最低价 {low_price:.2f} < MA60 {ma60:.2f}")
            else:
                logger.info(f"股票 {stock_code} 不满足条件: 最低价 {low_price:.2f} >= MA60 {ma60:.2f}")
        
        logger.info(f"条件检查完成，发现 {len(alert_stocks)} 只满足条件的股票")
        return alert_stocks
    
    def check_single_stock(self, stock_code, stock_data_row):
        """
        检查单只股票的条件
        
        Args:
            stock_code: 股票代码
            stock_data_row: 单行股票数据（Series或dict）
            
        Returns:
            dict or None: 如果满足条件返回详细信息，否则返回None
        """
        low_price = stock_data_row.get('low')
        ma60 = stock_data_row.get('ma60')
        
        if pd.isna(low_price) or pd.isna(ma60):
            logger.warning(f"股票 {stock_code} 数据不完整")
            return None
        
        if low_price < ma60:
            return {
                'stock_code': stock_code,
                'low_price': low_price,
                'ma60': ma60,
                'date': stock_data_row.get('date'),
                'condition': 'low < ma60',
                'price_difference': ma60 - low_price,
                'percentage_difference': (ma60 - low_price) / ma60 * 100
            }
        else:
            return None
    
    def get_condition_summary(self, stock_data):
        """
        获取条件检查摘要
        
        Args:
            stock_data: 股票数据
            
        Returns:
            dict: 检查结果摘要
        """
        if stock_data.empty:
            return {
                'total_stocks': 0,
                'alert_stocks': 0,
                'alert_percentage': 0.0,
                'details': []
            }
        
        alert_stocks = self.check_condition(stock_data)
        
        summary = {
            'total_stocks': len(stock_data),
            'alert_stocks': len(alert_stocks),
            'alert_percentage': len(alert_stocks) / len(stock_data) * 100 if len(stock_data) > 0 else 0.0,
            'details': alert_stocks
        }
        
        return summary