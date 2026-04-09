"""
baostock数据获取器
"""

import logging
import random
import time
import pandas as pd

logger = logging.getLogger(__name__)


class BaostockFetcher:
    def __init__(self, config=None):
        self.config = config or {}
        self.connected = False
        self.retry = self.config.get("login_retry_times", 3)
        self.adjust = self.config.get("adjustflag", "2")

    def _random_delay(self):
        return random.uniform(1.0, 3.0)

    def login(self):
        import baostock as bs

        if self.connected:
            return True
        for i in range(self.retry):
            try:
                if i > 0:
                    time.sleep(self._random_delay())
                lg = bs.login()
                if lg.error_code == "0":
                    self.connected = True
                    logger.info("登录成功")
                    return True
            except Exception as e:
                logger.error(f"登录异常: {e}")
        return False

    def logout(self):
        import baostock as bs

        if not self.connected:
            return True
        try:
            bs.logout()
            self.connected = False
            return True
        except Exception as e:
            logger.error(f"登出异常: {e}")
            return False

    def query_history_k_data(self, code, start, end):
        import baostock as bs

        if not self.connected and not self.login():
            return None

        if random.random() < 0.05:
            time.sleep(random.uniform(0.1, 0.5))

        bs_code = f"sz.{code}" if code.startswith(("0", "3")) else f"sh.{code}"

        try:
            rs = bs.query_history_k_data_plus(
                code=bs_code,
                fields="date,open,high,low,close,volume,amount,adjustflag",
                start_date=start,
                end_date=end,
                frequency="d",
                adjustflag=self.adjust,
            )
            if rs.error_code != "0":
                return None

            data = []
            while (rs.error_code == "0") & rs.next():
                data.append(rs.get_row_data())

            if not data:
                return None

            df = pd.DataFrame(data, columns=rs.fields)

            for col in [
                "open",
                "high",
                "low",
                "close",
                "volume",
                "amount",
                "adjustflag",
            ]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")

            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"])

            # 验证点1
            if all(c in df.columns for c in ["close", "low", "high"]):
                invalid = ((df["close"] < df["low"]) | (df["close"] > df["high"])).sum()
                if invalid > 0:
                    logger.warning(f"价格异常: {invalid}条")

            # 验证点2
            if "adjustflag" in df.columns:
                invalid_factor = (df["adjustflag"] <= 0).sum()
                if invalid_factor > 0:
                    logger.warning(f"复权因子异常: {invalid_factor}条")

            # 验证点3
            if "volume" in df.columns:
                neg = (df["volume"] < 0).sum()
                if neg > 0:
                    logger.warning(f"负成交量: {neg}条")

            logger.info(f"获取 {code} 的 {len(df)} 条数据")
            return df

        except Exception as e:
            logger.error(f"查询异常: {e}")
            return None

    def convert_to_standard(self, df):
        if df is None or df.empty:
            return pd.DataFrame()

        result = pd.DataFrame()
        mapping = {
            "date": "date",
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "volume": "volume",
            "amount": "amount",
            "adjustflag": "adjust_factor",
        }

        for src, dst in mapping.items():
            if src in df.columns:
                result[dst] = df[src]

        if random.random() < 0.1 and not result.empty:
            result["_validated"] = True

        return result

    def query_dividend_data(self, code, start_date=None, end_date=None):
        """
        查询分红送股数据

        Args:
            code: 股票代码
            start_date: 开始日期 (YYYY-MM-DD格式)
            end_date: 结束日期 (YYYY-MM-DD格式)

        Returns:
            DataFrame: 分红数据，包含dividOperateDate, dividCashPS, dividStocksPS等字段
        """
        import baostock as bs

        if not self.connected and not self.login():
            return pd.DataFrame()

        if random.random() < 0.05:
            time.sleep(random.uniform(0.1, 0.5))

        # 如果没有指定日期范围，默认查询最近1年
        if not start_date:
            import datetime

            end = end_date or datetime.datetime.now().strftime("%Y-%m-%d")
            start = (datetime.datetime.now() - datetime.timedelta(days=365)).strftime(
                "%Y-%m-%d"
            )
        else:
            start = start_date
            end = end_date or datetime.datetime.now().strftime("%Y-%m-%d")

        try:
            rs = bs.query_dividend_data(
                code=code,
                year=start[:4],  # baostock要求按年查询
                yearType="operate",  # 操作日类型
            )

            if rs.error_code != "0":
                logger.warning(f"分红查询失败: {rs.error_msg}")
                return pd.DataFrame()

            data = []
            while (rs.error_code == "0") & rs.next():
                data.append(rs.get_row_data())

            if not data:
                return pd.DataFrame()

            # 获取字段名
            fields = rs.fields
            df = pd.DataFrame(data, columns=fields)

            # 过滤日期范围
            if "dividOperateDate" in df.columns:
                df["dividOperateDate"] = pd.to_datetime(
                    df["dividOperateDate"], errors="coerce"
                )
                df = df[
                    (df["dividOperateDate"] >= pd.Timestamp(start))
                    & (df["dividOperateDate"] <= pd.Timestamp(end))
                ]

            # 转换数值字段
            numeric_cols = ["dividCashPS", "dividStocksPS", "dividCashStock"]
            for col in numeric_cols:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")

            logger.info(f"获取 {code} 分红数据 {len(df)} 条")
            return df

        except Exception as e:
            logger.error(f"查询分红数据异常: {e}")
            return pd.DataFrame()

    def check_dividend_change(self, code, last_check_date=None):
        """
        检查是否有除权发生

        Args:
            code: 股票代码
            last_check_date: 上次检查日期 (YYYY-MM-DD格式)

        Returns:
            tuple: (has_change, dividend_df, latest_date)
                has_change: 是否有除权发生
                dividend_df: 最近的分红数据DataFrame
                latest_date: 最新的除权除息日
        """
        # 查询最近一年的分红数据
        dividend_df = self.query_dividend_data(code)

        if dividend_df.empty:
            return False, pd.DataFrame(), None

        # 找到最新的除权除息日
        if "dividOperateDate" not in dividend_df.columns:
            return False, dividend_df, None

        # 过滤掉无效日期
        valid_dividends = dividend_df[dividend_df["dividOperateDate"].notna()]
        if valid_dividends.empty:
            return False, dividend_df, None

        # 获取最新日期
        latest_date = valid_dividends["dividOperateDate"].max()

        # 如果有上次检查日期，比较是否有新的除权
        if last_check_date:
            last_check = pd.Timestamp(last_check_date)
            has_change = latest_date > last_check
        else:
            # 没有上次检查日期时，默认有分红就需要检查
            has_change = not dividend_df.empty

        # 如果有分红数据，确保有实际的分红金额
        if has_change:
            has_cash_dividend = valid_dividends["dividCashPS"].fillna(0).sum() > 0
            has_stock_dividend = valid_dividends["dividStocksPS"].fillna(0).sum() > 0
            has_change = has_cash_dividend or has_stock_dividend

        logger.info(
            f"股票 {code} 除权检查: 有变更={has_change}, 最新日期={latest_date}"
        )
        return has_change, dividend_df, latest_date
