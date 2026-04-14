#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Session统一管理器 - 最小化版本
"""

import uuid
import logging
from typing import Optional
import pandas as pd

from .models import (
    SessionContext,
    StockPriceData,
    AlertStock,
    dataframe_to_stock_price_data,
    alert_dict_to_alert_stock,
)

logger = logging.getLogger(__name__)


class SessionManager:
    """Session管理器"""

    def __init__(self, config: Optional[dict] = None):
        self.global_config = config or {}
        self._sessions: dict[str, SessionContext] = {}

    def create_session(self, config: Optional[dict] = None) -> SessionContext:
        """创建新Session"""
        session_id = str(uuid.uuid4())[:8]
        session_config = {**self.global_config, **(config or {})}
        session = SessionContext(session_id=session_id, config=session_config)
        self._sessions[session_id] = session
        logger.info(f"创建Session: {session_id}")
        return session

    def get_session(self, session_id: str) -> Optional[SessionContext]:
        """获取Session"""
        return self._sessions.get(session_id)

    def update_stock_data(
        self, session: SessionContext, stock_code: str, data: StockPriceData
    ) -> bool:
        """更新Session中的股票数据"""
        # 入参校验
        if session is None:
            logger.error("update_stock_data失败: Session为None")
            return False
        if not stock_code or not isinstance(stock_code, str):
            logger.error(f"update_stock_data失败: 股票代码无效 {stock_code}")
            return False
        if data is None:
            logger.error(f"update_stock_data失败: 股票 {stock_code} 数据为None")
            return False

        try:
            # 数据有效性校验：价格不能为0或None
            if (
                data.latest.low is None
                or data.latest.close is None
                or data.latest.low <= 0
                or data.latest.close <= 0
            ):
                error_msg = f"股票 {stock_code} 价格无效: low={data.latest.low}, close={data.latest.close}"
                logger.error(error_msg)
                session.errors.append(error_msg)
                return False

            session.stocks_data[stock_code] = data
            logger.debug(f"股票 {stock_code} 数据已更新到Session")
            return True
        except Exception as e:
            error_msg = f"更新股票{stock_code}失败: {e}"
            logger.error(error_msg)
            session.errors.append(error_msg)
            return False

    def update_stock_from_dataframe(
        self, session: SessionContext, stock_code: str, df: pd.DataFrame, **kwargs
    ) -> bool:
        """从DataFrame更新股票数据（兼容旧代码）"""
        # 入参校验
        if session is None:
            logger.error("update_stock_from_dataframe失败: Session为None")
            return False
        if not stock_code or not isinstance(stock_code, str):
            logger.error(f"update_stock_from_dataframe失败: 股票代码无效 {stock_code}")
            return False
        if df is None or df.empty:
            logger.warning(
                f"update_stock_from_dataframe: 股票 {stock_code} DataFrame为空"
            )
            return False

        # 重试机制：最多重试2次
        max_retries = 2
        for retry in range(max_retries):
            try:
                stock_data = dataframe_to_stock_price_data(df, stock_code, **kwargs)
                if stock_data:
                    success = self.update_stock_data(session, stock_code, stock_data)
                    if success:
                        return True
                # 转换失败，重试
                if retry < max_retries - 1:
                    logger.warning(f"股票 {stock_code} 转换失败，重试第 {retry + 1} 次")
            except Exception as e:
                error_msg = f"股票 {stock_code} 转换异常: {e}"
                logger.error(error_msg)
                session.errors.append(error_msg)
                if retry < max_retries - 1:
                    logger.warning(f"重试第 {retry + 1} 次")

        # 所有重试都失败
        error_msg = f"股票 {stock_code} 数据转换最终失败，未存入Session"
        logger.error(error_msg)
        session.errors.append(error_msg)
        return False

    def add_alert(self, session: SessionContext, alert: AlertStock) -> bool:
        """添加警报到Session"""
        try:
            session.alerts.append(alert)
            return True
        except Exception as e:
            logger.error(f"添加警报失败: {e}")
            return False

    def add_alert_from_dict(self, session: SessionContext, alert_dict: dict) -> bool:
        """从dict添加警报（兼容旧代码）"""
        alert = alert_dict_to_alert_stock(alert_dict)
        if alert:
            return self.add_alert(session, alert)
        return False

    def get_all_dataframe(self, session: SessionContext) -> pd.DataFrame:
        """获取所有股票合并DataFrame（兼容旧代码）"""
        return session.get_all_dataframe()

    def get_alerts_as_dicts(self, session: SessionContext) -> list[dict]:
        """获取警报列表dict格式（兼容旧代码）"""
        return session.get_alerts_as_dicts()
