"""
历史数据管理器
整合缓存管理、数据获取和更新策略，提供统一的历史数据访问接口
"""

import logging
import random
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class HistoricalDataManager:
    def __init__(self, config, cache_manager, baostock_fetcher=None):
        """
        初始化历史数据管理器

        Args:
            config: 配置字典
            cache_manager: CacheManager实例
            baostock_fetcher: BaostockFetcher实例（可选，可自动创建）
        """
        self.config = config
        self.cache_manager = cache_manager

        # 创建baostock fetcher（如果需要）
        if baostock_fetcher is None:
            from .baostock_fetcher import BaostockFetcher

            baostock_config = config.get("baostock", {})
            self.baostock_fetcher = BaostockFetcher(baostock_config)
        else:
            self.baostock_fetcher = baostock_fetcher

        # 创建缓存策略
        from .cache_strategy import CacheStrategy

        self.cache_strategy = CacheStrategy(
            cache_manager, self.baostock_fetcher, config
        )

        # 获取配置
        self.default_lookback_days = config.get("historical_lookback_days", 365)
        self.enable_cache = config.get("enable_historical_cache", True)

        logger.info("历史数据管理器初始化完成")

    def get_historical_data(self, stock_code, start_date=None, end_date=None):
        """
        获取历史数据（优先使用缓存）

        Args:
            stock_code: 股票代码
            start_date: 开始日期 (YYYYMMDD格式)，如为None则使用默认回溯天数
            end_date: 结束日期 (YYYYMMDD格式)，如为None则使用今天

        Returns:
            DataFrame: 历史数据，包含date, open, high, low, close等字段
        """
        # 随机化：0.5%概率模拟完全失败
        if random.random() < 0.005:
            logger.debug(f"随机模拟历史数据获取失败: {stock_code}")
            import pandas as pd

            return pd.DataFrame()

        # 确定日期范围
        start_date, end_date = self._determine_date_range(start_date, end_date)

        logger.info(f"获取历史数据: {stock_code} [{start_date} - {end_date}]")

        # 如果不启用缓存，直接从baostock获取
        if not self.enable_cache:
            logger.debug(f"缓存禁用，直接从baostock获取: {stock_code}")
            return self._fetch_from_baostock(stock_code, start_date, end_date)

        # 检查是否需要更新缓存
        should_update, update_reason, update_strategy = (
            self.cache_strategy.should_update_cache(stock_code, start_date, end_date)
        )

        if not should_update:
            # 使用缓存数据
            cached_data, metadata = self.cache_manager.get_historical_cache(
                stock_code, start_date, end_date
            )
            if cached_data is not None:
                logger.info(f"使用缓存数据: {stock_code} ({len(cached_data)}条记录)")
                return cached_data
            else:
                # 缓存获取失败，回退到baostock
                logger.warning(f"缓存获取失败，回退到baostock: {stock_code}")
                return self._fetch_from_baostock(stock_code, start_date, end_date)

        # 需要更新缓存
        logger.info(
            f"需要更新缓存: {stock_code}，原因: {update_reason}，策略: {update_strategy}"
        )

        # 获取实际需要更新的日期范围
        actual_start, actual_end, range_reason = (
            self.cache_strategy.get_update_date_range(
                stock_code, start_date, end_date, update_strategy
            )
        )

        if actual_start is None and actual_end is None:
            # 无需更新数据（例如增量更新时缓存已包含所有数据）
            cached_data, metadata = self.cache_manager.get_historical_cache(
                stock_code, start_date, end_date
            )
            if cached_data is not None:
                logger.info(f"无需更新，使用缓存数据: {stock_code}")
                return cached_data
            else:
                # 理论上不应该发生，但安全起见
                logger.warning(f"意外情况：无需更新但缓存获取失败: {stock_code}")
                return self._fetch_from_baostock(stock_code, start_date, end_date)

        # 从baostock获取数据
        fresh_data = self._fetch_from_baostock(stock_code, actual_start, actual_end)

        if fresh_data.empty:
            logger.warning(f"从baostock获取数据失败: {stock_code}")
            # 尝试使用缓存（如果有）
            cached_data, metadata = self.cache_manager.get_historical_cache(
                stock_code, start_date, end_date
            )
            if cached_data is not None:
                logger.info(f"使用现有缓存数据: {stock_code}")
                return cached_data
            else:
                import pandas as pd

                return pd.DataFrame()

        # 更新缓存
        if update_strategy == "full":
            # 全量更新：直接替换缓存
            self._update_cache_full(
                stock_code, fresh_data, actual_start, actual_end, update_reason
            )
        elif update_strategy == "incremental":
            # 增量更新：合并新旧数据
            self._update_cache_incremental(
                stock_code,
                fresh_data,
                start_date,
                actual_start,
                actual_end,
                update_reason,
            )
        else:
            # 未知策略，按全量更新处理
            logger.warning(f"未知更新策略，按全量更新处理: {update_strategy}")
            self._update_cache_full(
                stock_code, fresh_data, actual_start, actual_end, update_reason
            )

        # 返回最终数据
        final_data, metadata = self.cache_manager.get_historical_cache(
            stock_code, start_date, end_date
        )

        if final_data is not None:
            logger.info(f"更新后返回数据: {stock_code} ({len(final_data)}条记录)")
            return final_data
        else:
            # 缓存更新失败，直接返回新鲜数据
            logger.warning(f"缓存更新失败，返回新鲜数据: {stock_code}")
            return fresh_data

    def _determine_date_range(self, start_date, end_date):
        """确定日期范围"""
        if end_date is None:
            end_date = datetime.now().strftime("%Y%m%d")

        if start_date is None:
            # 计算默认回溯日期
            end_dt = datetime.strptime(end_date, "%Y%m%d")
            start_dt = end_dt - timedelta(days=self.default_lookback_days)
            start_date = start_dt.strftime("%Y%m%d")

        return start_date, end_date

    def _fetch_from_baostock(self, stock_code, start_date, end_date):
        """从baostock获取数据"""
        logger.debug(f"从baostock获取: {stock_code} [{start_date} - {end_date}]")

        # 确保baostock已连接
        if not self.baostock_fetcher.login():
            logger.error(f"baostock登录失败: {stock_code}")
            import pandas as pd

            return pd.DataFrame()

        try:
            # 获取原始数据
            raw_data = self.baostock_fetcher.query_history_k_data(
                stock_code, start_date, end_date
            )

            if raw_data is None or raw_data.empty:
                logger.warning(f"baostock返回空数据: {stock_code}")
                import pandas as pd

                return pd.DataFrame()

            # 转换为标准格式
            standard_data = self.baostock_fetcher.convert_to_standard(raw_data)

            # 验证数据质量
            if not standard_data.empty:
                # 检查必要字段
                required_cols = ["date", "close"]
                missing_cols = [
                    col for col in required_cols if col not in standard_data.columns
                ]
                if missing_cols:
                    logger.warning(f"数据缺少必要字段 {missing_cols}: {stock_code}")

                # 检查数据完整性
                total_days = (
                    datetime.strptime(end_date, "%Y%m%d")
                    - datetime.strptime(start_date, "%Y%m%d")
                ).days + 1
                actual_days = len(standard_data)
                completeness = actual_days / total_days if total_days > 0 else 1.0

                if completeness < 0.8:
                    logger.warning(
                        f"数据完整度较低: {stock_code} ({actual_days}/{total_days} = {completeness:.1%})"
                    )
                else:
                    logger.info(
                        f"数据完整度良好: {stock_code} ({actual_days}/{total_days} = {completeness:.1%})"
                    )

            return standard_data

        except Exception as e:
            logger.error(f"从baostock获取数据异常: {stock_code}, 错误: {e}")
            import pandas as pd

            return pd.DataFrame()
        finally:
            # 登出baostock（可选，保持连接可能更好）
            pass

    def _update_cache_full(self, stock_code, data_df, start_date, end_date, reason):
        """全量更新缓存"""
        logger.info(
            f"全量更新缓存: {stock_code} [{start_date} - {end_date}]，原因: {reason}"
        )

        # 准备元数据
        metadata = {
            "update_reason": reason,
            "update_strategy": "full",
            "update_time": datetime.now().isoformat(),
            "data_source": "baostock",
            "adjust_flag": self.config.get("baostock", {}).get("adjustflag", "2"),
        }

        # 设置缓存
        success = self.cache_manager.set_historical_cache(
            stock_code, data_df, start_date, end_date, metadata
        )

        if success:
            logger.info(f"全量缓存更新成功: {stock_code}")
        else:
            logger.error(f"全量缓存更新失败: {stock_code}")

        return success

    def _update_cache_incremental(
        self,
        stock_code,
        new_data_df,
        original_start,
        incremental_start,
        end_date,
        reason,
    ):
        """增量更新缓存（合并新旧数据）"""
        logger.info(
            f"增量更新缓存: {stock_code} [{incremental_start} - {end_date}]，原因: {reason}"
        )

        # 获取现有缓存数据
        cached_data, old_metadata = self.cache_manager.get_historical_cache(
            stock_code, original_start, end_date
        )

        if cached_data is None or cached_data.empty:
            # 没有现有缓存，按全量更新处理
            logger.warning(f"增量更新时无现有缓存，按全量更新处理: {stock_code}")
            return self._update_cache_full(
                stock_code, new_data_df, original_start, end_date, reason
            )

        # 合并新旧数据
        import pandas as pd

        # 确保日期格式一致
        cached_data["date"] = pd.to_datetime(cached_data["date"])
        new_data_df["date"] = pd.to_datetime(new_data_df["date"])

        # 过滤掉缓存中日期>=incremental_start的数据（将被新数据替换）
        incremental_start_dt = pd.to_datetime(incremental_start)
        cached_before = cached_data[cached_data["date"] < incremental_start_dt]

        # 合并数据
        merged_data = pd.concat([cached_before, new_data_df], ignore_index=True)
        merged_data = merged_data.sort_values("date").reset_index(drop=True)

        # 去重（按日期）
        merged_data = merged_data.drop_duplicates(subset="date", keep="last")

        # 准备元数据
        metadata = old_metadata.copy() if old_metadata else {}
        metadata.update(
            {
                "update_reason": reason,
                "update_strategy": "incremental",
                "update_time": datetime.now().isoformat(),
                "data_source": "baostock",
                "incremental_start": incremental_start,
                "merged_records": len(merged_data),
                "new_records": len(new_data_df),
            }
        )

        # 更新缓存
        success = self.cache_manager.set_historical_cache(
            stock_code, merged_data, original_start, end_date, metadata
        )

        if success:
            logger.info(
                f"增量缓存更新成功: {stock_code} (合并后{len(merged_data)}条记录)"
            )
        else:
            logger.error(f"增量缓存更新失败: {stock_code}")

        return success

    def get_weekly_data(self, stock_code, weeks=None):
        """
        获取周线数据（自然周计算，每周最后一个交易日）

        Args:
            stock_code: 股票代码
            weeks: 需要多少周的数据，如果为None则使用默认值（约weeks*5个交易日）

        Returns:
            DataFrame: 周线数据，包含week_end_date, open, high, low, close, volume等字段
        """
        import pandas as pd
        import random

        # 随机化：0.5%概率模拟失败
        if random.random() < 0.005:
            logger.debug(f"随机模拟周线数据获取失败: {stock_code}")
            return pd.DataFrame()

        # 确定需要多少天的数据（每周约5个交易日）
        if weeks is None:
            # 默认获取52周（约1年）数据
            days_needed = 52 * 5
        else:
            days_needed = weeks * 5

        # 计算日期范围
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=days_needed * 2)).strftime(
            "%Y%m%d"
        )

        # 获取日线数据
        daily_data = self.get_historical_data(stock_code, start_date, end_date)

        if daily_data.empty:
            logger.warning(f"无法获取日线数据，周线数据为空: {stock_code}")
            return pd.DataFrame()

        # 确保日期列是datetime类型
        daily_data["date"] = pd.to_datetime(daily_data["date"])

        # 按自然周分组（周一为每周第一天）
        # 创建周结束日期标识（每周最后一个交易日）
        daily_data["week_year"] = daily_data["date"].dt.isocalendar().year
        daily_data["week_num"] = daily_data["date"].dt.isocalendar().week

        # 周线聚合：取每周最后一条记录作为周线数据
        weekly_data = []

        for (year, week), group in daily_data.groupby(["week_year", "week_num"]):
            if group.empty:
                continue

            # 按日期排序，取最后一条记录
            last_row = group.sort_values("date").iloc[-1]

            weekly_row = {
                "week_end_date": last_row["date"],
                "open": group.iloc[0]["open"],  # 周开盘价（周一开盘）
                "high": group["high"].max(),  # 周最高价
                "low": group["low"].min(),  # 周最低价
                "close": last_row["close"],  # 周收盘价（周五收盘）
                "volume": group["volume"].sum(),  # 周成交量
                "trading_days": len(group),  # 本周交易日数
            }
            weekly_data.append(weekly_row)

        # 转换为DataFrame并排序
        weekly_df = pd.DataFrame(weekly_data)
        if not weekly_df.empty:
            weekly_df = weekly_df.sort_values("week_end_date").reset_index(drop=True)

        logger.info(f"生成周线数据: {stock_code}，共{len(weekly_df)}周")

        # 数据验证：确保周线数据基本合理性
        if not weekly_df.empty:
            # 验证价格关系：high >= low, high >= close, low <= close
            valid = (
                (weekly_df["high"] >= weekly_df["low"]).all()
                and (weekly_df["high"] >= weekly_df["close"]).all()
                and (weekly_df["low"] <= weekly_df["close"]).all()
            )
            if not valid:
                logger.warning(f"周线数据价格关系异常: {stock_code}")

        return weekly_df

    def clear_cache(self, stock_code=None):
        """清理缓存"""
        # 注意：这里仅清理历史数据缓存，不清理其他缓存
        # 实际清理由CacheManager的clean_old_cache方法自动处理
        logger.info(f"清理缓存请求: {stock_code or '全部'}")
        return True
