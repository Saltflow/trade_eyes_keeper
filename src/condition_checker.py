"""
条件检查模块
支持多层级警报系统和单锚点检查
"""

import logging
import pandas as pd
from typing import Optional

logger = logging.getLogger(__name__)


class ConditionChecker:
    """条件检查器"""

    def __init__(self, config):
        self.config = config
        self.use_multi_alert = config.get("alerts", {}).get("enabled", False)

        if self.use_multi_alert:
            try:
                from .alert_processor import AlertProcessor
                from config import get_alerts_config

                alerts_config = get_alerts_config(
                    config.get("alerts", {}).get("config_path")
                )
                cache_dir = config.get("storage", {}).get("cache_dir", "./cache")
                self.alert_processor = AlertProcessor(alerts_config, cache_dir)
                logger.info("多层级警报系统已启用")
            except Exception as e:
                logger.error(f"多层级警报系统初始化失败: {e}")
                self.use_multi_alert = False
                self.alert_processor = None
        else:
            self.alert_processor = None

    def _validate_price_relationships(self, stock_data):
        """验证价格关系"""
        for _, row in stock_data.iterrows():
            code = row.get("stock_code", "")
            close = row.get("close")
            low = row.get("low")
            high = row.get("high")

            if close and low and close < low:
                logger.warning(f"股票 {code} 收盘价{close:.2f} < 最低价{low:.2f}")
            if close and high and close > high:
                logger.warning(f"股票 {code} 收盘价{close:.2f} > 最高价{high:.2f}")
            if low and high and low > high:
                logger.warning(f"股票 {code} 最低价{low:.2f} > 最高价{high:.2f}")

    def _check_multi(self, stock_data):
        """使用多层级警报系统"""
        try:
            if not self.alert_processor:
                return self._check_single(stock_data)

            alerts = self.alert_processor.process_stock_dataframe(stock_data)
            result = []

            for alert in alerts:
                code = alert.get("stock_code", "")
                anchor = alert.get("anchor_name", "")
                interval = alert.get("interval_label", "")
                pct = alert.get("percentage")
                days = alert.get("consecutive_days", 1)
                low_price = alert.get("low_price")  # 统一使用low_price字段名
                anchor_val = alert.get("anchor_value")

                # 安全计算price_difference，避免None值导致的TypeError
                price_difference = None
                if anchor_val is not None and low_price is not None:
                    price_difference = anchor_val - low_price

                result.append(
                    {
                        "stock_code": code,
                        "low_price": low_price,  # 直接使用low_price字段
                        "anchor_name": anchor,
                        "anchor_value": anchor_val,
                        "interval_label": interval,
                        "percentage": pct,
                        "consecutive_days": days,
                        "date": self._get_date(stock_data, code),
                        "condition": f"{anchor} 区间 {interval}",
                        "price_difference": price_difference,
                        "percentage_difference": pct,
                    }
                )

                logger.info(f"股票 {code} {anchor} 区间 {interval} (连续{days}天)")

            logger.info(f"多层级检查完成: {len(result)} 个警报")
            return result

        except Exception as e:
            logger.error(f"多层级检查失败: {e}")
            return self._check_single(stock_data)

    def _get_date(self, stock_data, stock_code):
        """获取股票日期"""
        if not stock_data.empty and "date" in stock_data.columns:
            matches = stock_data.loc[stock_data["stock_code"] == stock_code, "date"]
            if not matches.empty:
                return matches.iloc[0]
        return None

    def _check_single(self, stock_data):
        """单锚点检查: 低价格 < MA60"""
        result = []

        for _, row in stock_data.iterrows():
            code = row.get("stock_code", "")
            low = row.get("low")
            ma60 = row.get("ma60")

            if pd.isna(low) or pd.isna(ma60):
                continue

            if low < ma60:
                diff = ma60 - low
                pct = diff / ma60 * 100

                result.append(
                    {
                        "stock_code": code,
                        "low_price": low,
                        "ma60": ma60,
                        "date": row.get("date"),
                        "condition": "low < ma60",
                        "price_difference": diff,
                        "percentage_difference": pct,
                    }
                )

                logger.info(f"股票 {code} 满足条件: 低价格{low:.2f} < MA60{ma60:.2f}")

        logger.info(f"单锚点检查完成: {len(result)} 只股票")
        return result

    def check_from_session(self, session, session_manager=None):
        """
        从Session读取数据并检查条件，结果存入Session（新数据流）

        Args:
            session: SessionContext对象
            session_manager: SessionManager对象（可选，用于更新session）
        """
        if session_manager is None:
            from .session_manager import SessionManager

            session_manager = SessionManager(self.config)

        # 从Session获取DataFrame
        stock_data = session.get_all_dataframe()

        if stock_data.empty:
            logger.warning("Session中无股票数据")
            return

        # 复用现有检查逻辑
        result = []

        if self.use_multi_alert and self.alert_processor:
            result = self._check_multi(stock_data)
        else:
            result = self._check_single(stock_data)

        # 将结果存入Session
        for alert_dict in result:
            session_manager.add_alert_from_dict(session, alert_dict)

        logger.info(
            f"条件检查完成: {len(session.alerts)} 个警报, {len(session.errors)} 个错误"
        )
