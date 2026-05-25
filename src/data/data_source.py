"""
统一数据源模块
职责：
  1. 缓存管理（CSV + meta，自动过期）
  2. 数据获取（组合 web_crawler）
  3. 复权交叉验证（两数据源取上3日收盘价比对）
  4. 数据校验（价格完整性）
"""

import json
import logging
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


class DataSource:
    """统一数据源：缓存管理 + 数据获取 + 复权验证

    缓存设计原则：
      - 历史股价数据不可变，缓存永不过期
      - 唯一需要全量重拉的情况：除权除息导致前复权修正（由 _check_forward_adjustment 检测）
      - 增量拉取：缓存只缺尾巴时，只拉缺失天数 + 5天缓冲用于重叠比对
      - 合并：concat + drop_duplicates('date', keep='last')
    """

    # 主源 → 备用复权源的映射
    ALT_SOURCE_MAP = {
        "_fetch_from_qq": "_fetch_from_eastmoney",
        "_fetch_from_eastmoney": "_fetch_from_qq",
        "_fetch_from_sina_us": "_fetch_from_eastmoney",
        "_fetch_from_yahoo": "_fetch_from_eastmoney",
        "_fetch_from_sina": "_fetch_from_qq",
    }

    def __init__(self, config: dict):
        self.config = config

        # 缓存目录
        storage_config = config.get("storage", {})
        cache_dir = storage_config.get("cache_dir", "./cache")
        self.cache_dir = Path(cache_dir) / "data"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # web_crawler 延迟初始化
        self._web_crawler = None

    @property
    def web_crawler(self):
        if self._web_crawler is None:
            from .web_crawler import StockWebCrawler

            self._web_crawler = StockWebCrawler(self.config)
        return self._web_crawler

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------

    def fetch_stock_data(self, stock_code: str, days: int = 120) -> pd.DataFrame:
        """
        获取股票历史数据（含缓存 + 复权验证）

        缓存策略（按日期范围判定，不按行数，不过期）：
          1. 缓存日期范围全覆盖请求范围 → 直接返回
          2. 缓存覆盖开头但缺尾巴 → 增量拉取尾巴 + 5天缓冲，merge
          3. 缓存缺开头或无缓存 → 拉全量 + 30天缓冲，merge
          4. merge 前检测前复权修正（重叠日期收盘价比对 > 5%）
             如果检测到修正，重新全量拉取覆盖缓存

        Args:
            stock_code: 股票代码
            days: 需要的历史天数

        Returns:
            DataFrame: 已复权历史数据，空 DataFrame 表示获取失败
        """
        stock_code = str(stock_code)
        today = datetime.now()
        requested_start = today - timedelta(days=days)
        # 统一用 date 维度做 DataFrame 过滤，避免时间分量导致首行被误删
        requested_start_ts = pd.Timestamp(requested_start.date())

        # 1. 尝试缓存（日期范围判定 + bypass 检查）
        cached_df, cache_start, cache_end = self._read_cache(stock_code)
        if cached_df is not None and cache_start is not None and cache_end is not None:
            if (
                cache_start.date() <= requested_start.date()
                and cache_end.date() >= today.date()
            ):
                # 检查是否需要绕过缓存（15:55 后非当日缓存失效）
                if not self._should_bypass_cache(cache_end.date()):
                    logger.info(
                        f"DataSource 缓存命中: {stock_code} "
                        f"({len(cached_df)} 行, {cache_start.date()}~{cache_end.date()})"
                    )
                    return cached_df[
                        cached_df["date"] >= requested_start_ts
                    ]
                logger.info(
                    f"{stock_code} 缓存范围覆盖但触发 bypass，强制刷新"
                )

        # 2. 确定需要拉取的天数
        if (
            cached_df is not None
            and cache_start is not None
            and cache_start.date() <= requested_start.date()
        ):
            # 缓存覆盖了开头，仅缺尾巴
            tail_days = (today.date() - cache_end.date()).days + 5
            effective_days = max(tail_days, 5)
            logger.info(
                f"{stock_code} 缓存缺尾巴, 增量拉取 {effective_days} 天 "
                f"(缓存到 {cache_end.date()})"
            )
        else:
            # 缓存缺开头或无缓存
            effective_days = days + 30
            logger.info(f"{stock_code} 缓存不完整或无缓存, 拉取 {effective_days} 天")

        # 3. 获取数据（含复权交叉验证）
        new_data = self._fetch_with_verify(stock_code, effective_days)
        if new_data is None or new_data.empty:
            if cached_df is not None and not cached_df.empty:
                logger.warning(f"{stock_code} 拉取新数据失败, 返回缓存")
                return cached_df[
                    cached_df["date"] >= requested_start_ts
                ]
            return pd.DataFrame()

        # 4. 检测前复权修正（缓存 vs 新数据的重叠日期收盘价比对）
        if cached_df is not None and not cached_df.empty:
            if self._check_forward_adjustment(stock_code, cached_df, new_data):
                logger.warning(f"{stock_code} 检测到前复权修正, 重新全量拉取")
                new_data = self._fetch_with_verify(stock_code, days + 30)
                if new_data is None or new_data.empty:
                    result = cached_df if cached_df is not None else pd.DataFrame()
                    if not result.empty:
                        result = result[
                            result["date"] >= requested_start_ts
                        ]
                    return result
                # 全量覆盖写缓存（不复用旧缓存）
                self._write_cache(stock_code, new_data)
                return new_data[
                    new_data["date"] >= requested_start_ts
                ]

        # 5. 合并缓存 + 新数据
        if cached_df is not None and not cached_df.empty:
            merged = pd.concat([cached_df, new_data], ignore_index=True)
            merged = merged.drop_duplicates(subset=["date"], keep="last")
            merged = merged.sort_values("date").reset_index(drop=True)
            logger.info(
                f"{stock_code} 合并缓存+新数据: "
                f"{len(cached_df)} + {len(new_data)} = {len(merged)} 行"
            )
        else:
            merged = new_data

        # 6. 写缓存
        self._write_cache(stock_code, merged)
        return merged[
            merged["date"] >= requested_start_ts
        ]

    def fetch_raw_data(self, stock_code: str, days: int = 120) -> pd.DataFrame:
        """
        强制从 web_crawler 获取原始数据（跳过缓存和交叉验证）
        供需要原始数据的场景使用

        Args:
            stock_code: 股票代码
            days: 需要的历史天数

        Returns:
            DataFrame: 原始历史数据
        """
        stock_code = str(stock_code)
        return self.web_crawler.fetch_stock_data(stock_code, days)

    # ------------------------------------------------------------------
    # 缓存管理
    # ------------------------------------------------------------------

    def _cache_path(self, stock_code: str) -> Path:
        return self.cache_dir / f"{stock_code}.csv"

    def _meta_path(self, stock_code: str) -> Path:
        return self.cache_dir / f"{stock_code}.csv.meta"

    def _read_cache(
        self, stock_code: str
    ) -> Tuple[Optional[pd.DataFrame], Optional[datetime], Optional[datetime]]:
        """
        读取 CSV 缓存，返回 (DataFrame, start_date, end_date)
        无缓存或读取失败时返回 (None, None, None)
        """
        csv_path = self._cache_path(stock_code)
        if not csv_path.exists():
            return None, None, None
        try:
            df = pd.read_csv(csv_path, parse_dates=["date"])
            if df.empty:
                return None, None, None
            start_date = df["date"].min()
            end_date = df["date"].max()
            return df, start_date, end_date
        except Exception as e:
            logger.warning(f"读取缓存失败 {stock_code}: {e}")
            return None, None, None

    def _write_cache(self, stock_code: str, data: pd.DataFrame):
        """写入 CSV 缓存 + meta 元信息"""
        try:
            # CSV
            csv_path = self._cache_path(stock_code)
            data.to_csv(csv_path, index=False, encoding="utf-8-sig")

            # Meta
            start_date = (
                str(data["date"].min().date()) if "date" in data.columns else None
            )
            end_date = (
                str(data["date"].max().date()) if "date" in data.columns else None
            )
            meta = {
                "stock_code": stock_code,
                "fetch_time": datetime.now().isoformat(),
                "start_date": start_date,
                "end_date": end_date,
                "rows": len(data),
                "source": getattr(self.web_crawler, "_last_source_name", None),
            }
            meta_path = self._meta_path(stock_code)
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)

            logger.debug(
                f"DataSource 缓存已写入: {csv_path} ({len(data)} 行, "
                f"{start_date} ~ {end_date})"
            )
        except Exception as e:
            logger.warning(f"写入缓存失败 {stock_code}: {e}")

    def _should_bypass_cache(self, cache_end_date) -> bool:
        """
        判断是否应该绕过缓存。

        规则：当前时间 >= cache_bypass_cutoff 且缓存最后一天不是今天 → 绕过。
        该检查按投资标的粒度生效，用于除权除息后强制刷新前复权数据。

        Args:
            cache_end_date: 缓存数据的最后一天 (datetime.date)

        Returns:
            bool: True 表示应绕过缓存，False 表示可使用缓存
        """
        now = datetime.now()
        cutoff_str = self.config.get("scheduler", {}).get(
            "cache_bypass_cutoff", "15:55"
        )
        try:
            hour, minute = map(int, cutoff_str.split(":"))
            cutoff_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except (ValueError, AttributeError):
            logger.warning(
                f"无效的 cache_bypass_cutoff 格式: {cutoff_str}，使用默认 15:55"
            )
            cutoff_time = now.replace(hour=15, minute=55, second=0, microsecond=0)

        if now >= cutoff_time:
            today = now.date()
            if cache_end_date != today:
                logger.info(
                    f"缓存 bypass: 当前时间 {now.strftime('%H:%M')} >= cutoff "
                    f"{cutoff_str}，缓存结束日期 {cache_end_date} != 今天 {today}"
                )
                return True
        return False

    # ------------------------------------------------------------------
    # 数据获取 + 交叉验证
    # ------------------------------------------------------------------

    def _fetch_with_verify(self, stock_code: str, days: int) -> pd.DataFrame:
        """
        获取数据 + 复权交叉验证
        如果验证不通过，走备用源重试
        """
        MAX_RETRIES = 2
        last_data = None

        for attempt in range(MAX_RETRIES):
            if attempt == 0:
                # 首次：走默认 fallback 链
                data = self.web_crawler.fetch_stock_data(stock_code, days)
            else:
                # 重试：用备用源强制拉取（绕过默认链，直接指定源）
                primary_source = self.web_crawler._last_source_name
                alt_source = (
                    self.ALT_SOURCE_MAP.get(primary_source) if primary_source else None
                )
                if alt_source:
                    logger.info(
                        f"{stock_code} 使用备用源 {alt_source} 重试 "
                        f"(第 {attempt + 1} 次)"
                    )
                    data = self.web_crawler.fetch_stock_data(
                        stock_code, days, source_name=alt_source
                    )
                else:
                    # 无备用源映射，重新走默认链
                    data = self.web_crawler.fetch_stock_data(stock_code, days)

            if data is None or data.empty:
                logger.warning(
                    f"{stock_code} 第 {attempt + 1} 次获取为空数据, 继续重试"
                )
                continue  # 继续重试，不 return

            last_data = data

            # 交叉验证
            if self._cross_check(stock_code, data):
                return data  # 验证通过

            # 验证不通过 → 记录并 fallback
            logger.warning(
                f"{stock_code} 复权数据不一致（源={self.web_crawler._last_source_name}），"
                f"尝试备用源（第 {attempt + 1} 次）"
            )

        # 所有重试都失败，返回最后一次获取的数据
        if last_data is not None:
            logger.warning(
                f"{stock_code} 所有数据源复权验证均不一致，返回最后获取的数据"
            )
            return last_data
        logger.error(f"{stock_code} 所有数据源均返回空数据")
        return pd.DataFrame()

    def _check_forward_adjustment(
        self, stock_code: str, cached: pd.DataFrame, new_data: pd.DataFrame
    ) -> bool:
        """
        检测缓存数据是否因除权除息导致前复权修正。
        比对重叠日期的收盘价，如果差值显著(>5%)，说明历史数据已调整。

        Args:
            stock_code: 股票代码
            cached: 缓存的历史数据
            new_data: 新拉取的数据

        Returns:
            True 表示检测到前复权修正，需要全量重拉
        """
        if cached is None or cached.empty or new_data is None or new_data.empty:
            return False

        # 找共同日期（取缓存和新数据的交集）
        common_dates = pd.merge(
            cached[["date"]], new_data[["date"]], on="date", how="inner"
        )
        if len(common_dates) < 3:
            return False  # 重叠数据太少，无法判断

        # 取共同日期的收盘价
        cached_prices = cached[cached["date"].isin(common_dates["date"])][
            ["date", "close"]
        ].set_index("date")
        new_prices = new_data[new_data["date"].isin(common_dates["date"])][
            ["date", "close"]
        ].set_index("date")

        # 逐日比对
        for date_idx in cached_prices.index:
            cp = float(cached_prices.loc[date_idx, "close"])
            np_val = float(new_prices.loc[date_idx, "close"])
            if cp > 0 and abs(cp - np_val) / cp > 0.05:
                logger.warning(
                    f"{stock_code} 检测到前复权修正: {date_idx.date()}, "
                    f"旧收盘价={cp:.2f}, 新收盘价={np_val:.2f}, "
                    f"变动={abs(cp - np_val) / cp * 100:.1f}%"
                )
                return True

        return False

    def _cross_check(self, stock_code: str, primary_data: pd.DataFrame) -> bool:
        """
        复权交叉验证
        从备用源取上 3 个交易日的收盘价做比对
        """
        if len(primary_data) < 3:
            return True  # 数据太少，跳过验证

        primary_source = self.web_crawler._last_source_name
        alt_source = self.ALT_SOURCE_MAP.get(primary_source)
        if alt_source is None:
            return True  # 无备用复权源，跳过

        # 从备用源取少量数据
        alt_data = self.web_crawler.fetch_stock_data(
            stock_code, 5, source_name=alt_source
        )
        if alt_data is None or alt_data.empty or len(alt_data) < 3:
            return True  # 备用源不可用，跳过验证

        # 比对最后 3 行收盘价
        primary_closes = primary_data.tail(3)["close"].values
        alt_closes = alt_data.tail(3)["close"].values

        for i, (p, a) in enumerate(zip(primary_closes, alt_closes)):
            diff = abs(float(p) - float(a))
            if diff > 0.01:
                logger.warning(
                    f"{stock_code} 复权不一致（第{-3 + i}日): "
                    f"主源={float(p):.2f}, 备用={float(a):.2f}, 差值={diff:.4f}"
                )
                return False

        logger.debug(
            f"{stock_code} 复权交叉验证通过 ({primary_source} vs {alt_source})"
        )
        return True
