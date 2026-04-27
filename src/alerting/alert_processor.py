"""
警报处理器
集成规则引擎和状态管理器
"""

import logging
import pandas as pd
from datetime import datetime
from typing import List, Dict, Any

logger = logging.getLogger(__name__)


class AlertProcessor:
    """警报处理器：集成规则引擎和状态管理器"""

    def __init__(self, alerts_config, cache_dir=None):
        """
        初始化警报处理器

        Args:
            alerts_config: AlertsConfig实例
            cache_dir: 警报状态缓存目录
        """
        from .alert_engine import AlertEngine
        from .alert_state_manager import AlertStateManager

        self.alert_engine = AlertEngine(alerts_config)
        self.state_manager = AlertStateManager(alerts_config, cache_dir)
        self.current_date = datetime.now().date().isoformat()

    def process_stock(self, stock_data: pd.Series) -> List[Dict[str, Any]]:
        """
        处理单只股票，生成警报

        Args:
            stock_data: 股票数据Series

        Returns:
            要发送的警报列表
        """
        stock_code = stock_data.get("stock_code", "")
        if not stock_code:
            logger.warning("股票代码为空，跳过处理")
            return []

        # 使用规则引擎评估所有锚点
        evaluations = self.alert_engine.evaluate_stock(stock_data)
        if not evaluations:
            logger.debug(f"股票 {stock_code} 没有有效的锚点评估")
            return []

        alerts_to_send = []

        for evaluation in evaluations:
            anchor_name = evaluation["anchor_name"]
            interval = evaluation["interval"]
            interval_label = interval["label"]

            # 检查是否需要重置旧区间状态
            self.state_manager.reset_for_new_interval(
                stock_code, anchor_name, interval_label
            )

            # 检查是否应该发送警报
            should_send, consecutive_days = self.state_manager.should_alert(
                stock_code, anchor_name, interval_label, self.current_date
            )

            if should_send:
                # 添加连续天数信息
                alert = evaluation.copy()
                alert["consecutive_days"] = consecutive_days
                alert["interval_label"] = interval_label
                alerts_to_send.append(alert)

                # 打印详细数据用于调试
                logger.info(
                    f"股票 {stock_code} 警报数据详情: "
                    f"low_price={evaluation.get('low_price')}, "
                    f"anchor_name={evaluation.get('anchor_name')}, "
                    f"anchor_value={evaluation.get('anchor_value')}, "
                    f"percentage={evaluation.get('percentage')}"
                )

                # 更新状态
                self.state_manager.update(
                    stock_code, anchor_name, interval_label, self.current_date
                )

                logger.info(
                    f"股票 {stock_code} 锚点 {anchor_name} 在区间 {interval_label} "
                    f"（连续 {consecutive_days} 天），发送警报"
                )
            else:
                logger.debug(
                    f"股票 {stock_code} 锚点 {anchor_name} 在区间 {interval_label} "
                    f"（连续 {consecutive_days} 天），不发送警报"
                )

        return alerts_to_send

    def process_stock_dataframe(
        self, stock_data_df: pd.DataFrame
    ) -> List[Dict[str, Any]]:
        """
        处理整个股票数据DataFrame

        Args:
            stock_data_df: 股票数据DataFrame

        Returns:
            所有要发送的警报列表
        """
        all_alerts = []

        for _, row in stock_data_df.iterrows():
            stock_alerts = self.process_stock(row)
            all_alerts.extend(stock_alerts)

        logger.info(f"处理完成，共生成 {len(all_alerts)} 个警报")
        return all_alerts
