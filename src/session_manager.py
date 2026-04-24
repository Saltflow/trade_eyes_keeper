#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Session统一管理器 - 统一数据源版本
支持多数据源（web_crawler 主数据源，baostock 备用）
"""

import uuid
import logging
import warnings
from typing import Optional
from datetime import datetime, timedelta
import pandas as pd

from models.schemas import (
    SessionContext,
    StockPriceData,
    AlertStock,
)
from models.converters import (
    dataframe_to_stock_price_data,
    alert_dict_to_alert_stock,
)
from utils import safe_session_write

logger = logging.getLogger(__name__)


class DataSourceSelector:
    """数据源选择器 - 只使用 web_crawler（ETF除权支持更好）"""

    def __init__(
        self,
        config: dict,
        web_crawler=None,
        cache_manager=None,
    ):
        self.config = config
        self.web_crawler = web_crawler
        self.cache_manager = cache_manager

    def _is_etf(self, stock_code: str) -> bool:
        """判断是否为ETF基金"""
        try:
            from .utils.etf_detector import is_etf
        except ImportError:
            from utils.etf_detector import is_etf

        return is_etf(stock_code)

    def get_historical_data(
        self,
        stock_code: str,
        start_date: str,
        end_date: str,
        source: str = "web_crawler",
    ) -> pd.DataFrame:
        """获取历史数据 - 统一入口（只使用 web_crawler）

        Args:
            stock_code: 股票代码
            start_date: 开始日期 (YYYYMMDD)
            end_date: 结束日期 (YYYYMMDD)
            source: 数据源（忽略，始终使用 web_crawler）

        Returns:
            DataFrame: 历史数据，包含 date, open, high, low, close 等字段
        """
        # 转换日期格式为天数
        start_dt = datetime.strptime(start_date, "%Y%m%d")
        end_dt = datetime.strptime(end_date, "%Y%m%d")
        days = (end_dt - start_dt).days + 1

        # 所有股票都使用 web_crawler（ETF除权支持更好）
        return self._fetch_from_web_crawler(stock_code, days)

    def _fetch_from_web_crawler(self, stock_code: str, days: int) -> pd.DataFrame:
        """从 web_crawler 获取数据"""
        if self.web_crawler is None:
            try:
                from .web_crawler import StockWebCrawler

                self.web_crawler = StockWebCrawler(self.config)
                logger.debug("web_crawler 延迟初始化完成")
            except Exception as e:
                logger.error(f"web_crawler 初始化失败: {e}")
                return pd.DataFrame()

        try:
            data = self.web_crawler.fetch_stock_data(stock_code, days=days)
            if data is not None and not data.empty:
                logger.debug(f"web_crawler 获取 {stock_code} 数据成功: {len(data)}条")
                return data
            else:
                logger.warning(f"web_crawler 返回空数据: {stock_code}")
                return pd.DataFrame()
        except Exception as e:
            logger.error(f"web_crawler 获取数据异常 {stock_code}: {e}")
            return pd.DataFrame()

        try:
            data = self.web_crawler.fetch_stock_data(stock_code, days=days)
            if data is not None and not data.empty:
                logger.debug(f"web_crawler 获取 {stock_code} 数据成功: {len(data)}条")
                return data
            else:
                logger.warning(f"web_crawler 返回空数据: {stock_code}")
                return pd.DataFrame()
        except Exception as e:
            logger.error(f"web_crawler 获取数据异常 {stock_code}: {e}")
            return pd.DataFrame()

    def _fetch_from_baostock(
        self, stock_code: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        """从 baostock 获取数据"""
        if self.baostock_fetcher is None:
            try:
                from .baostock_fetcher import BaostockFetcher

                baostock_config = self.config.get("baostock", {})
                self.baostock_fetcher = BaostockFetcher(baostock_config)
                logger.debug("baostock_fetcher 延迟初始化完成")
            except Exception as e:
                logger.error(f"baostock_fetcher 初始化失败: {e}")
                return pd.DataFrame()

        try:
            # 发出废弃警告
            warnings.warn(
                "baostock_fetcher 已废弃，建议使用 web_crawler 作为主数据源",
                DeprecationWarning,
            )

            # 登录
            if not self.baostock_fetcher.login():
                logger.error(f"baostock 登录失败: {stock_code}")
                return pd.DataFrame()

            # 获取数据
            raw_data = self.baostock_fetcher.query_history_k_data(
                stock_code, start_date, end_date
            )

            if raw_data is None or raw_data.empty:
                logger.warning(f"baostock 返回空数据: {stock_code}")
                return pd.DataFrame()

            # 转换为标准格式
            standard_data = self.baostock_fetcher.convert_to_standard(raw_data)
            logger.debug(f"baostock 获取 {stock_code} 数据成功: {len(standard_data)}条")
            return standard_data

        except Exception as e:
            logger.error(f"baostock 获取数据异常 {stock_code}: {e}")
            return pd.DataFrame()


class SessionManager:
    """Session管理器 - 统一数据源版本"""

    def __init__(
        self,
        config: Optional[dict] = None,
        web_crawler=None,
        cache_manager=None,
    ):
        self.global_config = config or {}
        self._sessions: dict[str, SessionContext] = {}

        # 初始化数据源选择器
        self.data_source_selector = DataSourceSelector(
            config=self.global_config,
            web_crawler=web_crawler,
            cache_manager=cache_manager,
        )

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

    @safe_session_write
    def update_stock_data(
        self, session: SessionContext, stock_code: str, data: StockPriceData
    ) -> bool:
        """更新Session中的股票数据"""
        # 入参校验
        if session is None:
            logger.error("update_stock_data失败: Session为None")
            return False
        # 支持整数和字符串类型的股票代码
        if isinstance(stock_code, int):
            stock_code = str(stock_code)
        if not stock_code or not isinstance(stock_code, str):
            logger.error(f"update_stock_data失败: 股票代码无效 {stock_code}")
            return False
        if data is None:
            logger.error(f"update_stock_data失败: 股票 {stock_code} 数据为None")
            return False

        try:
            # 数据有效性校验：价格不能为0或None（扁平模型，直接访问 data.low / data.close）
            if (
                data.low is None
                or data.close is None
                or data.low <= 0
                or data.close <= 0
            ):
                error_msg = (
                    f"股票 {stock_code} 价格无效: low={data.low}, close={data.close}"
                )
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

    @safe_session_write
    def update_stock_from_dataframe(
        self, session: SessionContext, stock_code: str, df: pd.DataFrame, **kwargs
    ) -> bool:
        """从DataFrame更新股票数据（兼容旧代码）"""
        # 入参校验
        if session is None:
            logger.error("update_stock_from_dataframe失败: Session为None")
            return False
        # 支持整数和字符串类型的股票代码
        if isinstance(stock_code, int):
            stock_code = str(stock_code)
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

    def get_historical_data(
        self,
        stock_code: str,
        start_date: str,
        end_date: str,
        source: str = "auto",
    ) -> pd.DataFrame:
        """获取历史数据 - 统一入口

        Args:
            stock_code: 股票代码
            start_date: 开始日期 (YYYYMMDD)
            end_date: 结束日期 (YYYYMMDD)
            source: 数据源优先级 ("auto", "web_crawler", "baostock")

        Returns:
            DataFrame: 历史数据，包含 date, open, high, low, close 等字段
        """
        logger.debug(
            f"获取历史数据: {stock_code} [{start_date} - {end_date}], source={source}"
        )
        return self.data_source_selector.get_historical_data(
            stock_code, start_date, end_date, source
        )

    def get_backtest_data(self, stock_code: str, days: int = 730) -> pd.DataFrame:
        """获取回测数据 - 专门为回测框架设计

        Args:
            stock_code: 股票代码
            days: 需要的历史天数（默认 730 天 = 2 年）

        Returns:
            DataFrame: 完整历史数据，用于回测
        """
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")

        logger.debug(f"获取回测数据: {stock_code} [{days}天]")
        return self.get_historical_data(stock_code, start_date, end_date, source="auto")
