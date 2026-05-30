"""
股票数据获取模块
获取A股真实交易数据
 使用网页爬虫获取真实数据，已移除不可靠的akshare API
绝不使用模拟数据进行投资决策
"""

import logging
import pandas as pd
import numpy as np
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, cast

from ..data.cache_manager import CacheManager
from ..data.technical_indicators import TechnicalIndicators
from ..models.converters import dataframe_to_stock_price_data

logger = logging.getLogger(__name__)


class StockDataFetcher:
    """股票数据获取器"""

    def __init__(self, config):
        """
        初始化数据获取器

        Args:
            config: 配置字典
        """
        self.config = config
        self.stocks = config.get("stocks", [])
        # 数据源类型，已移除akshare，只支持web_crawler
        data_source_type = config.get("data_source", {}).get("type", "web_crawler")
        if data_source_type == "akshare":
            logger.warning("akshare数据源已移除，将使用web_crawler作为替代")
        # data_source 通过 @property 延迟初始化 DataSource 实例
        self._data_source = None
        self._web_crawler = None
        self._cache_manager = None

        # 时区配置
        scheduler_config = config.get("scheduler", {})
        import pytz

        timezone_str = scheduler_config.get("timezone", "Asia/Shanghai")
        self.timezone = pytz.timezone(timezone_str)

        # 初始化技术指标计算器（用于多锚点警报）
        # 注意：需要在 fetch_to_session 中设置 session_context
        self.technical_indicators = TechnicalIndicators()

    @property
    def data_source(self):
        if self._data_source is None:
            from ..data.data_source import DataSource

            self._data_source = DataSource(self.config)
        return self._data_source

    @property
    def web_crawler(self):
        if self._web_crawler is None:
            from ..data.web_crawler import StockWebCrawler

            self._web_crawler = StockWebCrawler(self.config)
        return self._web_crawler

    @property
    def cache_manager(self):
        if self._cache_manager is None:
            from ..data.cache_manager import CacheManager

            self._cache_manager = CacheManager(self.config)
        return self._cache_manager

    def _save_to_csv(self, stock_code, stock_data):
        """
        保存股票数据到CSV文件

        Args:
            stock_code: 股票代码
            stock_data: 股票数据DataFrame
        """
        try:
            data_dir = self.config.get("storage", {}).get("data_dir", "./data")
            Path(data_dir).mkdir(parents=True, exist_ok=True)

            csv_file = Path(data_dir) / f"{stock_code}_history.csv"

            # 如果文件存在，读取现有数据并合并
            if csv_file.exists():
                existing_data = pd.read_csv(csv_file, parse_dates=["date"])

                # 合并数据，去除重复
                combined_data = pd.concat(
                    [existing_data, stock_data], ignore_index=True
                )
                combined_data = combined_data.drop_duplicates(
                    subset=["date"], keep="last"
                )
                combined_data = combined_data.sort_values("date")

                stock_data = combined_data

            # 保存到CSV
            stock_data.to_csv(csv_file, index=False, encoding="utf-8-sig")
            logger.info(f"股票 {stock_code} 数据已保存到: {csv_file}")

        except Exception as e:
            logger.error(f"保存股票 {stock_code} 数据到CSV失败: {e}")

    def _fetch_from_web_crawler(self, stock_code, start_date, end_date):
        """
        使用网页爬虫获取股票真实数据

        Args:
            stock_code: 股票代码
            start_date: 开始日期 (YYYYMMDD)
            end_date: 结束日期 (YYYYMMDD)

        Returns:
            pandas.DataFrame: 股票真实数据
        """
        try:
            logger.info(f"使用网页爬虫获取股票 {stock_code} 真实数据")

            # 计算需要的历史天数
            start_dt = datetime.strptime(start_date, "%Y%m%d")
            end_dt = datetime.strptime(end_date, "%Y%m%d")
            days = (end_dt - start_dt).days + 30  # 多取一些天数

            # 获取数据
            data = self.web_crawler.fetch_stock_data(stock_code, days)

            if data.empty:
                logger.error(f"网页爬虫未能获取到股票 {stock_code} 数据")
                return None

            # 过滤日期范围
            data = data[(data["date"] >= start_dt) & (data["date"] <= end_dt)]

            if data.empty:
                logger.warning("网页爬虫获取的数据不在请求的日期范围内")
                # 返回所有数据，让调用方处理
                data = self.web_crawler.fetch_stock_data(stock_code, days)

            logger.info(f"网页爬虫成功获取股票 {stock_code} 的 {len(data)} 条真实数据")
            return data

        except Exception as e:
            logger.error(f"网页爬虫获取股票 {stock_code} 数据失败: {e}")
            raise

    def _fetch_fundamental_data(self, stock_code):
        """
        获取股票基本面数据：分红、股息率、业绩增长、估值指标

        Args:
            stock_code: 股票代码

        Returns:
            dict: 包含基本面数据的字典
        """
        # 初始化结果字典 (值类型为 Optional[float])
        fundamental_data: Dict[str, Optional[float]] = {
            "dividend_per_share": None,  # 过去1年每股分红（元）
            "dividend_yield": None,  # 当前价年化股息率（%）
            "pe_ratio": None,  # 市盈率 (PE)
            "pb_ratio": None,  # 市净率 (PB)
            "roe": None,  # 净资产收益率 (ROE)，由 PB/PE 计算得出
        }

        # 确保股票代码是字符串
        stock_code = str(stock_code)

        # 1. 获取分红数据
        dividend = self._fetch_dividend_from_web_crawler(stock_code)
        if dividend is not None:
            fundamental_data["dividend_per_share"] = dividend

        # 2. 获取业绩增长数据（暂时返回None，后续可通过web_crawler实现）
        # 保留为None，避免使用不可靠的API

        # 3. 获取估值指标（PE、PB），ROE 由 PB/PE 计算
        valuation_data = self._fetch_valuation_from_web_crawler(stock_code)
        if valuation_data:
            pe = valuation_data.get("pe_ratio")
            pb = valuation_data.get("pb_ratio")
            fundamental_data["pe_ratio"] = pe  # type: ignore
            fundamental_data["pb_ratio"] = pb  # type: ignore
            if pe is not None and pb is not None and pe != 0:
                roe = (pb / pe) * 100
                fundamental_data["roe"] = round(roe, 3)
                logger.info(
                    f"股票 {stock_code} ROE 计算: PE={pe:.2f}, PB={pb:.2f}, "
                    f"ROE={roe:.2f}%"
                )
            else:
                logger.warning(
                    f"股票 {stock_code} PE/PB 为空，无法计算 ROE "
                    f"(PE={pe}, PB={pb})"
                )

        logger.info(
            f"股票 {stock_code} 基本面数据获取完成: "
            f"分红={fundamental_data['dividend_per_share']}"
        )

        return fundamental_data

    def _fetch_dividend_from_web_crawler(self, stock_code: str) -> Optional[float]:
        """获取分红数据，优先使用LLM提取结果，其次使用网页爬虫"""
        logger.info(f"开始获取股票 {stock_code} 的分红数据")
        # 1. 尝试从LLM提取缓存获取分红数据
        try:
            llm_extraction = self.cache_manager.get_latest_llm_extraction_for_stock(
                stock_code, days=365
            )
            if llm_extraction:
                dividend = llm_extraction.get("dividend_per_share")
                if dividend is None:
                    dividend = llm_extraction.get("cash_dividend_per_share")
                if dividend is not None:
                    logger.info(
                        f"股票 {stock_code} 从LLM提取缓存获取分红数据: {dividend:.3f}元"
                    )
                    return dividend
        except Exception as e:
            logger.warning(f"从LLM提取缓存获取股票 {stock_code} 分红数据失败: {e}")

        # 2. 如果LLM提取缓存没有，尝试网页爬虫作为备选
        try:
            dividend_data = self.web_crawler.fetch_dividend_data(stock_code)
            if dividend_data and dividend_data.get("dividend_per_share"):
                dividend = dividend_data["dividend_per_share"]
                logger.info(
                    f"股票 {stock_code} 从网页爬虫获取分红数据: {dividend:.3f}元"
                )
                return dividend
        except Exception as e:
            logger.warning(f"获取股票 {stock_code} 分红数据失败: {e}")
        return None

    def _fetch_valuation_from_web_crawler(
        self, stock_code: str
    ) -> Optional[Dict[str, Optional[float]]]:
        """从网页爬虫获取估值指标数据"""
        valuation_data = None
        try:
            valuation_data = self.web_crawler.fetch_valuation_data(stock_code)
        except Exception as e:
            logger.warning(f"获取股票 {stock_code} 估值指标失败: {e}")
            return None

        if valuation_data:
            # 记录获取到的估值数据
            pe_str = (
                f"{valuation_data.get('pe_ratio'):.2f}"
                if valuation_data.get("pe_ratio") is not None
                else "None"
            )
            pb_str = (
                f"{valuation_data.get('pb_ratio'):.2f}"
                if valuation_data.get("pb_ratio") is not None
                else "None"
            )
            logger.info(
                f"股票 {stock_code} 估值指标: "
                f"PE={pe_str}, PB={pb_str}"
            )
            return cast(Dict[str, Optional[float]], valuation_data)  # type: ignore
        else:
            logger.warning(f"股票 {stock_code} 未获取到估值指标数据")
            return None

    def fetch_to_session(self, session, session_manager=None):
        """
        获取股票数据并存入Session（新数据流）

        Args:
            session: SessionContext对象
            session_manager: SessionManager对象（可选，用于更新session）
        """
        if session_manager is None:
            from ..session.session_manager import SessionManager

            session_manager = SessionManager(self.config)

        # 设置 technical_indicators 的 session_context
        self.technical_indicators.session_context = session

        # 通过 DataSource 统一获取（缓存管理 + 复权验证由 DataSource 内部处理）
        all_data = []
        # 初始化历史数据暂存区（供图表模块使用，不触 Pydantic 模型）
        if not hasattr(session, "_historical"):
            object.__setattr__(session, "_historical", {})

        for stock_code in self.stocks:
            stock_code = str(stock_code)

            try:
                # 从 DataSource 获取历史数据（含缓存管理 + 复权交叉验证）
                stock_data = self.data_source.fetch_stock_data(stock_code, days=730)

                if stock_data is not None and not stock_data.empty:
                    stock_data["stock_code"] = stock_code

                    # 计算所有技术指标
                    stock_data = self.technical_indicators.calculate_indicators(
                        stock_data, stock_code=stock_code
                    )

                    # 只保留最新一天的数据
                    latest_data = stock_data.iloc[-1:].copy()

                    # 检查数据日期
                    if not latest_data.empty:
                        latest_date = latest_data.iloc[0].get("date")
                        if latest_date:
                            today = datetime.now(self.timezone).date()
                            if latest_date.date() != today:
                                logger.warning(
                                    f"股票 {stock_code} 最新数据日期为 "
                                    f"{latest_date.date()}，不是今天 {today}"
                                )

                    # 获取基本面数据
                    fundamental_data = self._fetch_fundamental_data(stock_code)

                    # 将基本面数据添加到latest_data
                    latest_data["dividend_per_share"] = fundamental_data[
                        "dividend_per_share"
                    ]
                    latest_data["pe_ratio"] = fundamental_data["pe_ratio"]
                    latest_data["pb_ratio"] = fundamental_data["pb_ratio"]
                    latest_data["roe"] = fundamental_data["roe"]

                    # 计算股息率
                    if (
                        fundamental_data["dividend_per_share"] is not None
                        and not latest_data.empty
                    ):
                        close_price = latest_data.iloc[0].get("close")
                        if close_price and close_price > 0:
                            dividend_per_share = fundamental_data["dividend_per_share"]
                            dividend_yield = (dividend_per_share / close_price) * 100

                            if dividend_yield > 30 or dividend_per_share > close_price:
                                logger.warning(
                                    f"股票 {stock_code} 股息率异常高: "
                                    f"分红={dividend_per_share:.3f}元, "
                                    f"股价={close_price:.2f}元, "
                                    f"股息率={dividend_yield:.2f}%"
                                )

                            latest_data["dividend_yield"] = round(dividend_yield, 2)
                            logger.info(
                                f"股票 {stock_code} 股息率计算: "
                                f"分红={dividend_per_share:.3f}元, "
                                f"股价={close_price:.2f}元, "
                                f"股息率={dividend_yield:.2f}%"
                            )
                        else:
                            latest_data["dividend_yield"] = None
                    else:
                        latest_data["dividend_yield"] = None

                    all_data.append(latest_data)

                    # 保存完整历史数据到CSV（兼容图表模块）
                    self._save_to_csv(stock_code, stock_data)

                    # 暂存完整历史 DataFrame 供图表模块使用
                    session._historical[stock_code] = stock_data

            except Exception as e:
                logger.error(f"获取股票 {stock_code} 数据失败: {e}")
                session.errors.append(f"获取股票 {stock_code} 数据失败: {e}")

        if all_data:
            stock_data_df = pd.concat(all_data, ignore_index=True)
        else:
            stock_data_df = pd.DataFrame()

        if stock_data_df.empty:
            logger.warning("未获取到股票数据")
            return

        # 按股票代码分组并转换
        if "stock_code" not in stock_data_df.columns:
            logger.error("股票数据缺少stock_code列")
            session.errors.append("股票数据缺少stock_code列")
            return

        # 获取所有唯一的股票代码（numpy.ndarray -> List[str]）
        unique_stock_codes = stock_data_df["stock_code"].unique().tolist()
        for stock_code in unique_stock_codes:
            # 明确提取为DataFrame（处理loc可能返回Series的情况）
            stock_df = stock_data_df.loc[stock_data_df["stock_code"] == stock_code]
            if isinstance(stock_df, pd.Series):
                stock_df = pd.DataFrame([stock_df])
            else:
                stock_df = stock_df.copy()
            success = session_manager.update_stock_from_dataframe(
                session, stock_code, stock_df
            )
            if not success:
                logger.warning(f"股票 {stock_code} 数据未成功存入Session")

        logger.info(
            f"数据已存入Session: {len(session.stocks_data)} 只股票, "
            f"{len(session.errors)} 个错误"
        )
