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
from typing import Optional

logger = logging.getLogger(__name__)


class DataSource:
    """统一数据源：缓存管理 + 数据获取 + 复权验证"""

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

        # 过期配置
        self.cache_days = storage_config.get("cache_days", 7)

        # scheduler 配置
        scheduler_config = config.get("scheduler", {})
        cutoff_str = scheduler_config.get("cache_bypass_cutoff", "15:55")
        try:
            cutoff_hour, cutoff_minute = map(int, cutoff_str.split(":"))
            self.cutoff_hour = cutoff_hour
            self.cutoff_minute = cutoff_minute
        except ValueError:
            self.cutoff_hour = 15
            self.cutoff_minute = 55

        # 时区
        import pytz

        timezone_str = scheduler_config.get("timezone", "Asia/Shanghai")
        self.timezone = pytz.timezone(timezone_str)

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

        Args:
            stock_code: 股票代码
            days: 需要的历史天数

        Returns:
            DataFrame: 已复权历史数据，空 DataFrame 表示获取失败
        """
        stock_code = str(stock_code)

        # 1. 尝试缓存
        cached = self._read_cache(stock_code)
        if (
            cached is not None
            and len(cached) >= days
            and not self._is_expired(stock_code)
        ):
            logger.info(f"DataSource 缓存命中: {stock_code} ({len(cached)} 行)")
            return cached

        # 2. 从 web_crawler 获取
        data = self._fetch_with_verify(stock_code, days)

        # 3. 写入缓存
        if data is not None and not data.empty:
            self._write_cache(stock_code, data)

        return data if data is not None else pd.DataFrame()

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

    def _read_cache(self, stock_code: str) -> Optional[pd.DataFrame]:
        """读取 CSV 缓存"""
        csv_path = self._cache_path(stock_code)
        if not csv_path.exists():
            return None
        try:
            df = pd.read_csv(csv_path, parse_dates=["date"])
            return df
        except Exception as e:
            logger.warning(f"读取缓存失败 {stock_code}: {e}")
            return None

    def _write_cache(self, stock_code: str, data: pd.DataFrame):
        """写入 CSV 缓存 + meta 元信息"""
        try:
            # CSV
            csv_path = self._cache_path(stock_code)
            data.to_csv(csv_path, index=False, encoding="utf-8-sig")

            # Meta
            meta = {
                "stock_code": stock_code,
                "fetch_time": datetime.now(self.timezone).isoformat(),
                "rows": len(data),
                "source": self.web_crawler._last_source_name,
            }
            meta_path = self._meta_path(stock_code)
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)

            logger.debug(f"DataSource 缓存已写入: {csv_path} ({len(data)} 行)")
        except Exception as e:
            logger.warning(f"写入缓存失败 {stock_code}: {e}")

    def _is_expired(self, stock_code: str) -> bool:
        """
        判断缓存是否过期
        规则：
          - 7 天以上 → 过期
          - 非今天数据，且当前时间 >= 15:55 → 过期
          - 今天的数据 → 不过期
        """
        meta_path = self._meta_path(stock_code)
        if not meta_path.exists():
            return True

        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            return True

        fetch_time_str = meta.get("fetch_time")
        if not fetch_time_str:
            return True

        try:
            fetch_time = datetime.fromisoformat(fetch_time_str)
        except Exception:
            return True

        # 转换为本地时区
        if fetch_time.tzinfo is None:
            fetch_time = fetch_time.replace(tzinfo=self.timezone)

        now = datetime.now(self.timezone)
        fetch_date = fetch_time.date()
        today = now.date()

        # 同一天 → 不过期
        if fetch_date == today:
            return False

        # 超过 cache_days 天 → 过期
        if (today - fetch_date).days >= self.cache_days:
            return True

        # 非今天数据，当前时间 >= 15:55 → 过期
        cutoff = now.replace(
            hour=self.cutoff_hour, minute=self.cutoff_minute, second=0, microsecond=0
        )
        if now >= cutoff:
            return True

        return False

    # ------------------------------------------------------------------
    # 数据获取 + 交叉验证
    # ------------------------------------------------------------------

    def _fetch_with_verify(self, stock_code: str, days: int) -> pd.DataFrame:
        """
        获取数据 + 复权交叉验证
        如果验证不通过，走 fallback 链重试
        """
        MAX_RETRIES = 2  # 最多尝试 2 轮

        for attempt in range(MAX_RETRIES):
            # 从 web_crawler 获取
            data = self.web_crawler.fetch_stock_data(stock_code, days)
            if data is None or data.empty:
                return pd.DataFrame()

            # 交叉验证
            if self._cross_check(stock_code, data):
                return data  # 验证通过

            # 验证不通过 → 记录并 fallback
            logger.warning(
                f"{stock_code} 复权数据不一致（源={self.web_crawler._last_source_name}），"
                f"尝试备用源（第 {attempt + 1} 次）"
            )

        # 所有重试都失败，返回最后一次获取的数据
        logger.warning(f"{stock_code} 所有数据源复权验证均不一致，返回最后获取的数据")
        return data

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
