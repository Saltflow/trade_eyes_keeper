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
