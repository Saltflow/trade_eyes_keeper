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
    

