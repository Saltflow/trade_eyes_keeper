"""
缓存更新策略类
决定何时使用缓存、何时更新缓存
"""

import logging
import random
from datetime import datetime, timedelta
import pandas as pd

logger = logging.getLogger(__name__)


class CacheStrategy:
    def __init__(self, cache_manager, baostock_fetcher, config=None):
        """
        初始化缓存策略

        Args:
            cache_manager: CacheManager实例
            baostock_fetcher: BaostockFetcher实例
            config: 配置字典
        """
        self.cache_manager = cache_manager
        self.baostock_fetcher = baostock_fetcher
        self.config = config or {}

        # 从配置获取参数
        self.cache_days = self.config.get("historical_cache_days", 7)
        self.force_update_interval = self.config.get("force_update_interval_days", 30)
        self.check_dividend_interval = self.config.get(
            "check_dividend_interval_days", 7
        )

    def should_update_cache(self, stock_code, start_date, end_date):
        """
        判断是否应该更新缓存

        Args:
            stock_code: 股票代码
            start_date: 开始日期 (YYYYMMDD格式)
            end_date: 结束日期 (YYYYMMDD格式)

        Returns:
            tuple: (should_update, reason, strategy)
                should_update: 是否需要更新缓存
                reason: 更新原因描述
                strategy: 更新策略 ("full"全量更新, "incremental"增量更新, "none"不更新)
        """
        # 随机化：1%概率强制更新
        if random.random() < 0.01:
            logger.debug(f"随机强制更新: {stock_code}")
            return True, "random_force_update", "full"

        # 检查是否有缓存
        cached_data, metadata = self.cache_manager.get_historical_cache(
            stock_code, start_date, end_date
        )

        # 如果没有缓存，需要全量更新
        if cached_data is None or metadata is None:
            logger.debug(f"无缓存数据，需要全量更新: {stock_code}")
            return True, "no_cache", "full"

        # 检查缓存过期时间
        last_updated_str = metadata.get("last_updated")
        if last_updated_str:
            try:
                last_updated = datetime.fromisoformat(last_updated_str)
                days_since_update = (datetime.now() - last_updated).days

                # 强制更新间隔
                if days_since_update >= self.force_update_interval:
                    logger.debug(
                        f"缓存超过{self.force_update_interval}天，需要全量更新: {stock_code}"
                    )
                    return (
                        True,
                        f"force_update_interval_exceeded_{days_since_update}_days",
                        "full",
                    )

                # 检查除权间隔
                last_dividend_check = metadata.get("last_dividend_check")
                if last_dividend_check:
                    last_check = datetime.fromisoformat(last_dividend_check)
                    days_since_check = (datetime.now() - last_check).days
                    if days_since_check >= self.check_dividend_interval:
                        # 需要检查除权
                        return self._check_dividend_based_update(
                            stock_code, cached_data, metadata
                        )
                else:
                    # 从未检查过除权，需要检查
                    return self._check_dividend_based_update(
                        stock_code, cached_data, metadata
                    )
            except Exception as e:
                logger.warning(f"解析缓存时间失败: {e}")
                # 如果解析失败，强制更新
                return True, "parse_date_failed", "full"
        else:
            # 没有更新时间戳，强制更新
            logger.debug(f"缓存无更新时间戳，需要更新: {stock_code}")
            return True, "no_update_timestamp", "full"

        # 默认不更新
        logger.debug(f"缓存有效，无需更新: {stock_code}")
        return False, "cache_valid", "none"

    def _check_dividend_based_update(self, stock_code, cached_data, metadata):
        """
        基于除权检查的更新决策

        Args:
            stock_code: 股票代码
            cached_data: 缓存的数据DataFrame
            metadata: 缓存元数据

        Returns:
            tuple: (should_update, reason, strategy)
        """
        try:
            # 获取上次检查的除权日期
            last_dividend_date_str = metadata.get("last_dividend_date")
            last_dividend_date = None
            if last_dividend_date_str:
                last_dividend_date = (
                    last_dividend_date_str  # 保持字符串格式供check_dividend_change使用
                )

            # 检查是否有除权发生
            has_change, dividend_df, latest_date = (
                self.baostock_fetcher.check_dividend_change(
                    stock_code, last_dividend_date
                )
            )

            # 更新元数据中的除权检查时间
            new_metadata = metadata.copy()
            new_metadata["last_dividend_check"] = datetime.now().isoformat()

            if latest_date:
                new_metadata["last_dividend_date"] = latest_date.strftime("%Y-%m-%d")

            if has_change:
                logger.info(f"检测到除权变更，需要全量更新: {stock_code}")
                return True, "dividend_change_detected", "full"
            else:
                # 没有除权变更，检查是否需要增量更新
                return self._check_incremental_update(
                    stock_code, cached_data, new_metadata
                )

        except Exception as e:
            logger.error(f"除权检查失败: {e}")
            # 检查失败时保守起见进行全量更新
            return True, "dividend_check_failed", "full"

    def _check_incremental_update(self, stock_code, cached_data, metadata):
        """
        检查是否需要增量更新

        Args:
            stock_code: 股票代码
            cached_data: 缓存的数据DataFrame
            metadata: 缓存元数据

        Returns:
            tuple: (should_update, reason, strategy)
        """
        try:
            # 获取缓存的最新日期
            if "date" not in cached_data.columns:
                logger.warning(f"缓存数据缺少date字段: {stock_code}")
                return True, "missing_date_field", "full"

            # 转换日期列为datetime
            cached_dates = pd.to_datetime(cached_data["date"])
            latest_cached_date = cached_dates.max()

            # 如果缓存的最新日期早于今天，需要增量更新
            today = datetime.now().date()
            if latest_cached_date.date() < today:
                days_diff = (today - latest_cached_date.date()).days
                if days_diff <= 7:  # 只差几天，增量更新
                    logger.debug(
                        f"缓存缺失最近{days_diff}天数据，增量更新: {stock_code}"
                    )
                    return True, f"missing_recent_days_{days_diff}", "incremental"
                else:
                    # 缺失数据较多，全量更新
                    logger.debug(f"缓存缺失{days_diff}天数据，全量更新: {stock_code}")
                    return True, f"missing_many_days_{days_diff}", "full"

            # 缓存已包含最新数据，无需更新
            logger.debug(f"缓存包含最新数据，无需更新: {stock_code}")
            return False, "cache_up_to_date", "none"

        except Exception as e:
            logger.error(f"增量更新检查失败: {e}")
            return True, "incremental_check_failed", "full"

    def get_update_date_range(self, stock_code, start_date, end_date, strategy):
        """
        根据更新策略获取实际需要更新的日期范围

        Args:
            stock_code: 股票代码
            start_date: 原始开始日期 (YYYYMMDD格式)
            end_date: 原始结束日期 (YYYYMMDD格式)
            strategy: 更新策略 ("full", "incremental", "none")

        Returns:
            tuple: (actual_start_date, actual_end_date, reason)
        """
        if strategy == "none":
            return None, None, "no_update_needed"

        elif strategy == "full":
            # 全量更新：使用原始日期范围
            return start_date, end_date, "full_update"

        elif strategy == "incremental":
            # 增量更新：从缓存最新日期的下一天开始
            try:
                cached_data, metadata = self.cache_manager.get_historical_cache(
                    stock_code, start_date, end_date
                )

                if cached_data is None or "date" not in cached_data.columns:
                    # 如果无法获取缓存，回退到全量更新
                    logger.warning(
                        f"增量更新时无法读取缓存，回退到全量更新: {stock_code}"
                    )
                    return start_date, end_date, "fallback_full"

                # 获取缓存的最新日期
                cached_dates = pd.to_datetime(cached_data["date"])
                latest_cached_date = cached_dates.max()

                # 计算下一天
                next_day = latest_cached_date + timedelta(days=1)
                incremental_start = next_day.strftime("%Y%m%d")

                # 如果增量开始日期晚于结束日期，说明缓存已包含所有数据
                if incremental_start > end_date:
                    logger.debug(f"缓存已包含全部数据，无需增量更新: {stock_code}")
                    return None, None, "cache_already_complete"

                logger.info(
                    f"增量更新: {stock_code} 从 {incremental_start} 到 {end_date}"
                )
                return incremental_start, end_date, "incremental_update"

            except Exception as e:
                logger.error(f"增量更新日期范围计算失败: {e}")
                # 失败时回退到全量更新
                return start_date, end_date, "error_fallback_full"

        else:
            # 未知策略，使用全量更新
            logger.warning(f"未知更新策略: {strategy}，使用全量更新")
            return start_date, end_date, "unknown_strategy_fallback"
