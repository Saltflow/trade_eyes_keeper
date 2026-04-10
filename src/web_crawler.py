"""
网页爬虫模块
从公开网站获取股票真实数据
"""

import logging
import pandas as pd
from datetime import datetime, timedelta
import requests
import re
import json
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


class StockWebCrawler:
    """股票网页爬虫"""

    def __init__(self, config):
        """
        初始化爬虫

        Args:
            config: 配置字典
        """
        self.config = config
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        self.timeout = 30
        self.retry_times = 3
        self.retry_delay = 2

    def _is_etf(self, stock_code):
        """
        判断是否为ETF基金

        Args:
            stock_code: 股票代码

        Returns:
            bool: True如果是ETF，否则False
        """
        stock_code = str(stock_code)
        # ETF代码常见前缀
        etf_prefixes = ("51", "52", "15", "16", "18", "58")
        if stock_code.startswith(etf_prefixes):
            if stock_code.isdigit() and len(stock_code) == 6:
                return True
        # 特殊ETF代码
        special_etfs = {"508091", "513910", "588000"}
        if stock_code in special_etfs:
            return True
        return False

    def fetch_stock_data(self, stock_code, days=120):
        """
        获取股票历史数据

        Args:
            stock_code: 股票代码
            days: 需要的历史天数

        Returns:
            pandas.DataFrame: 股票历史数据
        """
        stock_code = str(stock_code)

        # 尝试多个数据源（优先使用历史数据API）
        # 对于ETF，优先使用支持复权的数据源（腾讯、东方财富）
        # 新浪财经API没有复权参数，可能返回未复权数据
        if self._is_etf(stock_code):
            data_sources = [
                self._fetch_from_qq,  # 腾讯财经（有历史数据API，使用qfq前复权）
                self._fetch_from_eastmoney,  # 东方财富（使用fqt=1前复权）
                self._fetch_from_sina,  # 新浪财经（没有复权参数，备用）
            ]
            logger.info(f"ETF {stock_code} 检测到，调整数据源顺序优先使用复权数据源")
        else:
            data_sources = [
                self._fetch_from_sina,  # 新浪财经（有历史数据API）
                self._fetch_from_qq,  # 腾讯财经（有历史数据API）
                self._fetch_from_eastmoney,  # 东方财富（API常失败）
            ]

        for source_func in data_sources:
            try:
                logger.info(f"尝试从 {source_func.__name__} 获取股票 {stock_code} 数据")
                data = source_func(stock_code, days)
                if data is not None and not data.empty:
                    logger.info(
                        f"从 {source_func.__name__} 成功获取股票 {stock_code} 的 {len(data)} 条数据"
                    )

                    # 计算MA60
                    if "close" in data.columns:
                        data["ma60"] = (
                            data["close"].rolling(window=60, min_periods=1).mean()
                        )
                    data["stock_code"] = stock_code

                    return data
            except Exception as e:
                logger.warning(
                    f"从 {source_func.__name__} 获取股票 {stock_code} 数据失败: {e}"
                )
                continue

        logger.error(f"所有数据源都失败，无法获取股票 {stock_code} 数据")
        return pd.DataFrame()

    def _fetch_from_eastmoney(self, stock_code, days):
        """
        从东方财富获取股票数据

        Args:
            stock_code: 股票代码
            days: 历史天数

        Returns:
            pandas.DataFrame: 股票数据
        """
        try:
            # 东方财富API
            # 首先获取实时数据确定股票市场
            market = "0" if stock_code.startswith(("0", "3")) else "1"  # 0:深市, 1:沪市

            # 东方财富日线数据API
            end_date = datetime.now().strftime("%Y%m%d")
            start_date = (datetime.now() - timedelta(days=days + 30)).strftime(
                "%Y%m%d"
            )  # 多取一些

            url = "http://push2his.eastmoney.com/api/qt/stock/kline/get"
            params = {
                "secid": f"{market}.{stock_code}",
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                "klt": "101",  # 日线
                "fqt": "1",  # 前复权
                "beg": start_date,
                "end": end_date,
                "lmt": "10000",  # 足够大的数量
            }

            headers = {
                "User-Agent": self.user_agent,
                "Referer": "http://quote.eastmoney.com/",
            }

            response = requests.get(
                url, params=params, headers=headers, timeout=self.timeout
            )
            response.raise_for_status()

            data_json = response.json()
            if data_json.get("data") and data_json["data"].get("klines"):
                klines = data_json["data"]["klines"]

                data_list = []
                for kline in klines:
                    items = kline.split(",")
                    if len(items) >= 11:
                        data_list.append(
                            {
                                "date": items[0],
                                "open": float(items[1]),
                                "close": float(items[2]),
                                "high": float(items[3]),
                                "low": float(items[4]),
                                "volume": float(items[5]),
                                "amount": float(items[6]),
                                "amplitude": float(items[7]) if items[7] else 0.0,
                                "change_pct": float(items[8]) if items[8] else 0.0,
                                "change": float(items[9]) if items[9] else 0.0,
                                "turnover": float(items[10])
                                if len(items) > 10 and items[10]
                                else 0.0,
                            }
                        )

                if data_list:
                    df = pd.DataFrame(data_list)
                    df["date"] = pd.to_datetime(df["date"])
                    df = df.sort_values("date")
                    return df

        except Exception as e:
            logger.warning(f"从东方财富获取股票 {stock_code} 数据失败: {e}")

        # 如果API失败，返回空DataFrame（不生成模拟数据）
        logger.warning("东方财富API失败，不生成模拟数据")
        return pd.DataFrame()

    def _parse_eastmoney_web(self, stock_code, days):
        """
        解析东方财富网页获取数据（备用方案）
        """
        try:
            market = "SZ" if stock_code.startswith(("0", "3")) else "SH"
            url = f"http://quote.eastmoney.com/{market}{stock_code}.html"

            headers = {
                "User-Agent": self.user_agent,
                "Referer": f"http://quote.eastmoney.com/{market}{stock_code}.html",
            }

            response = requests.get(url, headers=headers, timeout=self.timeout)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, "html.parser")

            # 尝试找到价格信息（简化版，实际需要更复杂的解析）
            # 这里只获取最新价格作为示例
            price_elem = soup.find("span", class_="price")
            if price_elem:
                current_price = float(price_elem.text)

                # 无法获取真实历史数据，返回空DataFrame（不生成模拟数据）
                logger.warning(
                    f"获取到股票 {stock_code} 当前价格 {current_price}，但无法获取真实历史数据"
                )
                return pd.DataFrame()

        except Exception as e:
            logger.warning(f"解析东方财富网页失败: {e}")

        return pd.DataFrame()

    def _fetch_from_sina(self, stock_code, days):
        """
        从新浪财经获取股票数据（使用历史数据API）
        只使用真实历史数据，不生成模拟数据
        """
        try:
            # 只尝试获取历史数据
            historical_data = self._fetch_historical_from_sina(stock_code, days)
            if historical_data is not None and not historical_data.empty:
                logger.info(
                    f"从新浪财经历史API成功获取股票 {stock_code} 的 {len(historical_data)} 条真实历史数据"
                )
                return historical_data
            else:
                logger.warning("新浪财经历史数据API返回空数据，跳过该数据源")
                return pd.DataFrame()

        except Exception as e:
            logger.warning(f"从新浪财经获取股票 {stock_code} 数据失败: {e}")
            return pd.DataFrame()

    def _fetch_historical_from_sina(self, stock_code, days):
        """
        从新浪财经历史数据API获取真实历史数据
        """
        try:
            market = (
                "sh"
                if stock_code.startswith("6") or stock_code.startswith("5")
                else "sz"
            )
            symbol = f"{market}{stock_code}"

            # 新浪财经历史数据API
            url = "http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
            params = {
                "symbol": symbol,
                "scale": "240",  # 日线
                "datalen": str(days),  # 数据长度
            }

            headers = {
                "User-Agent": self.user_agent,
                "Referer": "http://finance.sina.com.cn/",
            }

            response = requests.get(
                url, params=params, headers=headers, timeout=self.timeout
            )
            response.raise_for_status()

            # 解析JSON数据
            data_list = response.json()

            if not data_list:
                logger.warning("新浪财经历史数据API返回空数据")
                return pd.DataFrame()

            # 转换为DataFrame
            records = []
            for item in data_list:
                record = {
                    "date": item["day"],
                    "open": float(item["open"]),
                    "close": float(item["close"]),
                    "high": float(item["high"]),
                    "low": float(item["low"]),
                    "volume": float(item["volume"]),
                    "amount": float(item["volume"])
                    * float(item["close"]),  # 估算成交额
                    "amplitude": (float(item["high"]) - float(item["low"]))
                    / float(item["open"])
                    * 100
                    if float(item["open"]) > 0
                    else 0.0,
                    "change_pct": 0.0,  # 稍后计算
                    "change": float(item["close"]) - float(item["open"]),
                    "turnover": 0.0,  # 新浪不提供换手率
                }
                records.append(record)

            df = pd.DataFrame(records)
            df["date"] = pd.to_datetime(df["date"])

            # 计算涨跌幅（基于前一日收盘价）
            if len(df) > 1:
                df["change_pct"] = df["close"].pct_change() * 100
                # 第一天的涨跌幅用当天变化计算
                if len(df) > 0:
                    df.loc[0, "change_pct"] = (
                        (df.loc[0, "close"] - df.loc[0, "open"])
                        / df.loc[0, "open"]
                        * 100
                    )

            # 按日期排序
            df = df.sort_values("date")

            logger.info(
                f"从新浪财经历史API获取股票 {stock_code} 的 {len(df)} 条真实历史数据"
            )
            return df

        except Exception as e:
            logger.warning(f"从新浪财经历史数据API获取股票 {stock_code} 数据失败: {e}")
            return pd.DataFrame()

    def _fetch_realtime_from_sina(self, stock_code, days):
        """
        从新浪财经获取实时数据（备用方案）
        """
        try:
            market = "sz" if stock_code.startswith(("0", "3")) else "sh"
            url = f"http://hq.sinajs.cn/list={market}{stock_code}"

            headers = {
                "User-Agent": self.user_agent,
                "Referer": "http://finance.sina.com.cn/",
            }

            response = requests.get(url, headers=headers, timeout=self.timeout)
            response.raise_for_status()

            # 解析新浪财经格式
            content = response.text
            match = re.search(r'="(.+)"', content)
            if match:
                data_str = match.group(1)
                items = data_str.split(",")

                if len(items) >= 30:
                    # 最新数据
                    current_data = {
                        "date": datetime.now().strftime("%Y-%m-%d"),
                        "open": float(items[1]),
                        "close": float(items[3]),  # 当前价
                        "high": float(items[4]),
                        "low": float(items[5]),
                        "volume": float(items[8]),  # 成交量
                        "amount": float(items[9]),  # 成交额
                        "amplitude": (float(items[4]) - float(items[5]))
                        / float(items[1])
                        * 100
                        if float(items[1]) > 0
                        else 0.0,
                        "change_pct": (float(items[3]) - float(items[2]))
                        / float(items[2])
                        * 100
                        if float(items[2]) > 0
                        else 0.0,
                        "change": float(items[3]) - float(items[2]),
                        "turnover": 0.0,  # 新浪不直接提供换手率
                    }

                    # 获取历史数据（新浪历史数据API比较复杂，这里只返回最新数据）
                    df = pd.DataFrame([current_data])
                    df["date"] = pd.to_datetime(df["date"])

                    # 无法获取真实历史数据，返回空DataFrame（不生成模拟数据）
                    logger.warning(
                        f"获取到股票 {stock_code} 当前数据，但无法获取真实历史数据，无法计算MA60"
                    )
                    return pd.DataFrame()

        except Exception as e:
            logger.warning(f"从新浪财经实时数据获取股票 {stock_code} 数据失败: {e}")

        return pd.DataFrame()

    def _fetch_from_qq(self, stock_code, days):
        """
        从腾讯财经获取股票数据
        只使用真实历史数据，不生成模拟数据
        """
        try:
            # 只尝试获取历史数据
            historical_data = self._fetch_historical_from_qq(stock_code, days)
            if historical_data is not None and not historical_data.empty:
                logger.info(
                    f"从腾讯财经历史API成功获取股票 {stock_code} 的 {len(historical_data)} 条真实历史数据"
                )
                return historical_data
            else:
                logger.warning("腾讯财经历史数据API返回空数据，跳过该数据源")
                return pd.DataFrame()

        except Exception as e:
            logger.warning(f"从腾讯财经获取股票 {stock_code} 数据失败: {e}")
            return pd.DataFrame()

    def _fetch_historical_from_qq(self, stock_code, days):
        """
        从腾讯财经历史数据API获取真实历史数据
        """
        try:
            market = (
                "sh"
                if stock_code.startswith("6") or stock_code.startswith("5")
                else "sz"
            )
            symbol = f"{market}{stock_code}"

            # 腾讯财经历史数据API
            url = "http://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
            params = {
                "param": f"{symbol},day,,,{days},qfq",  # qfq: 前复权
                "_var": "kline_day",
            }

            headers = {"User-Agent": self.user_agent, "Referer": "http://gu.qq.com/"}

            response = requests.get(
                url, params=params, headers=headers, timeout=self.timeout
            )
            response.raise_for_status()

            # 解析响应（格式：kline_day={...})
            content = response.text
            if content.startswith("kline_day="):
                json_str = content[len("kline_day=") :]
                data = json.loads(json_str)

                if data.get("code") == 0 and "data" in data:
                    stock_data = data["data"].get(symbol)
                    if stock_data and "qfqday" in stock_data:
                        qfqday = stock_data["qfqday"]

                        records = []
                        for item in qfqday:
                            # 每个item格式: ["2025-08-29","7.379","7.429","7.439","7.359","1237045.000"]
                            # 可能还有额外字段，我们只取前6个
                            if len(item) >= 5:
                                record = {
                                    "date": item[0],
                                    "open": float(item[1]),
                                    "close": float(item[2]),
                                    "high": float(item[3]),
                                    "low": float(item[4]),
                                    "volume": float(item[5]) if len(item) > 5 else 0.0,
                                    "amount": 0.0,  # 腾讯不直接提供成交额
                                    "amplitude": (float(item[3]) - float(item[4]))
                                    / float(item[1])
                                    * 100
                                    if float(item[1]) > 0
                                    else 0.0,
                                    "change_pct": 0.0,  # 稍后计算
                                    "change": float(item[2]) - float(item[1]),
                                    "turnover": 0.0,  # 腾讯不直接提供换手率
                                }
                                records.append(record)

                        df = pd.DataFrame(records)
                        df["date"] = pd.to_datetime(df["date"])

                        # 计算涨跌幅（基于前一日收盘价）
                        if len(df) > 1:
                            df["change_pct"] = df["close"].pct_change() * 100
                            # 第一天的涨跌幅用当天变化计算
                            if len(df) > 0:
                                df.loc[0, "change_pct"] = (
                                    (df.loc[0, "close"] - df.loc[0, "open"])
                                    / df.loc[0, "open"]
                                    * 100
                                )

                        # 按日期排序
                        df = df.sort_values("date")

                        logger.info(
                            f"从腾讯财经历史API获取股票 {stock_code} 的 {len(df)} 条真实历史数据"
                        )
                        return df

            logger.warning("腾讯财经历史数据API返回数据格式异常")
            return pd.DataFrame()

        except json.JSONDecodeError as e:
            logger.warning(f"解析腾讯财经历史数据JSON失败: {e}")
            return pd.DataFrame()
        except Exception as e:
            logger.warning(f"从腾讯财经历史数据API获取股票 {stock_code} 数据失败: {e}")
            return pd.DataFrame()

    def _fetch_realtime_from_qq(self, stock_code, days):
        """
        从腾讯财经获取实时数据（备用方案）
        """
        try:
            market = "sz" if stock_code.startswith(("0", "3")) else "sh"
            url = f"http://qt.gtimg.cn/q={market}{stock_code}"

            headers = {"User-Agent": self.user_agent}

            response = requests.get(url, headers=headers, timeout=self.timeout)
            response.raise_for_status()

            content = response.text
            items = content.split("~")

            if len(items) > 40:
                current_data = {
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "open": float(items[5]) if items[5] else 0.0,
                    "close": float(items[3]) if items[3] else 0.0,
                    "high": float(items[33]) if items[33] else 0.0,
                    "low": float(items[34]) if items[34] else 0.0,
                    "volume": float(items[6]) if items[6] else 0.0,
                    "amount": float(items[37]) if items[37] else 0.0,
                    "amplitude": float(items[43]) if items[43] else 0.0,
                    "change_pct": float(items[32]) if items[32] else 0.0,
                    "change": float(items[31]) if items[31] else 0.0,
                    "turnover": float(items[38]) if items[38] else 0.0,
                }

                df = pd.DataFrame([current_data])
                df["date"] = pd.to_datetime(df["date"])

                logger.warning(
                    f"获取到股票 {stock_code} 当前数据，但无法获取真实历史数据，无法计算MA60"
                )
                return pd.DataFrame()

        except Exception as e:
            logger.warning(f"从腾讯财经实时数据获取股票 {stock_code} 数据失败: {e}")

        return pd.DataFrame()

    def fetch_dividend_data(self, stock_code):
        """
        获取股票分红数据（从公开财报或利润分配公告扒取）

        Args:
            stock_code: 股票代码

        Returns:
            dict: 包含分红数据的字典，键包括：
                - dividend_per_share: 最近一年每股分红（元）
                - dividend_yield: 当前股息率（%）（由data_fetcher计算）
                - last_dividend_date: 最近分红日期
                - dividend_history: 历史分红列表
        """
        stock_code = str(stock_code)
        logger.info(f"尝试获取股票 {stock_code} 的分红数据")

        # 尝试多个数据源
        data_sources = [
            self._fetch_dividend_from_sina,
            self._fetch_dividend_from_eastmoney,
        ]

        for source_func in data_sources:
            try:
                logger.info(
                    f"尝试从 {source_func.__name__} 获取股票 {stock_code} 分红数据"
                )
                dividend_data = source_func(stock_code)
                if (
                    dividend_data
                    and dividend_data.get("dividend_per_share") is not None
                ):
                    logger.info(
                        f"从 {source_func.__name__} 成功获取股票 {stock_code} 分红数据"
                    )
                    return dividend_data
            except Exception as e:
                logger.warning(
                    f"从 {source_func.__name__} 获取股票 {stock_code} 分红数据失败: {e}"
                )
                continue

        logger.warning(f"所有数据源都失败，无法获取股票 {stock_code} 分红数据")
        return None

    def fetch_valuation_data(self, stock_code):
        """
        获取股票估值指标数据（PE, PB, ROE, 负债率）
         使用腾讯财经(QQ)实时API获取估值指标，包含数据有效性验证

        Args:
            stock_code: 股票代码

        Returns:
            dict: 包含估值指标的字典，键包括：
                - pe_ratio: 市盈率
                - pb_ratio: 市净率
                - roe: 净资产收益率（%）
                - debt_ratio: 资产负债率（%）
        """
        stock_code = str(stock_code)
        logger.info(f"尝试获取股票 {stock_code} 的估值指标数据")

        def parse_qq_items(items):
            """解析QQ实时数据项"""

            def safe_float(val):
                if not val:
                    return None
                try:
                    return float(val)
                except (ValueError, TypeError):
                    return None

            def validate_metric(value, metric_name):
                if value is None:
                    return None
                if metric_name == "pe":
                    # PE可以为负值（亏损企业），但0无效，极高值(>1000)视为异常
                    if value == 0 or abs(value) > 1000:
                        return None
                elif metric_name == "pb":
                    # PB可以为负值（净资产为负），但0无效，极高值(>50)视为异常
                    if value == 0 or abs(value) > 50:
                        return None
                elif metric_name == "roe":
                    # ROE百分比范围 -100 到 100
                    if value < -100 or value > 100:
                        return None
                elif metric_name == "debt":
                    # 负债率百分比范围 0 到 100
                    if value < 0 or value > 100:
                        return None
                return value

            # QQ实时数据字段映射:
            # 39: 市盈率(PE), 46: 市净率(PB), 52: 净资产收益率(ROE%), 53: 资产负债率(%)
            pe = safe_float(items[39]) if len(items) > 39 else None
            pb = safe_float(items[46]) if len(items) > 46 else None
            roe = safe_float(items[52]) if len(items) > 52 else None
            debt = safe_float(items[53]) if len(items) > 53 else None

            pe = validate_metric(pe, "pe")
            pb = validate_metric(pb, "pb")
            roe = validate_metric(roe, "roe")
            debt = validate_metric(debt, "debt")

            # 计算基于PB/PE的ROE并验证一致性
            roe_calculated = None
            if pe is not None and pb is not None and pe != 0:
                roe_calculated = (pb / pe) * 100

                # 验证ROE数据一致性（允许±5%的差异）
                if roe is not None and abs(roe - roe_calculated) > 5.0:
                    logger.warning(
                        f"ROE数据不一致: 股票估值数据中ROE={roe}%与计算值ROE(PB/PE)={roe_calculated:.2f}%差异过大"
                    )
                    # 使用计算值以确保数据一致性
                    roe = roe_calculated
                elif roe is None:
                    # 如果缺少ROE数据，使用计算值
                    roe = roe_calculated
            elif roe is None:
                # 无法计算ROE且无原始ROE数据，保持None
                pass

            return {"pe_ratio": pe, "pb_ratio": pb, "roe": roe, "debt_ratio": debt}

        def fetch_from_qq(stock_code):
            """从腾讯财经获取估值指标"""
            try:
                market = "sh" if stock_code.startswith(("6", "5")) else "sz"
                url = f"http://qt.gtimg.cn/q={market}{stock_code}"
                headers = {"User-Agent": self.user_agent}
                response = requests.get(url, headers=headers, timeout=self.timeout)
                response.raise_for_status()
                content = response.text
                if "=" in content:
                    data_str = content.split("=")[1].strip('";')
                    items = data_str.split("~")
                    if len(items) > 53:
                        return parse_qq_items(items)
            except Exception as e:
                logger.warning(f"从腾讯财经获取股票 {stock_code} 估值数据失败: {e}")
            return None

        def fetch_from_eastmoney(stock_code):
            """从东方财富获取估值指标（ROE和负债率）"""
            # 占位符实现，暂时返回None
            logger.warning(f"东方财富估值数据获取未实现，股票 {stock_code} 返回None值")
            return None

        # 尝试多个数据源
        data_sources = [fetch_from_qq, fetch_from_eastmoney]

        for source_func in data_sources:
            try:
                logger.info(
                    f"尝试从 {source_func.__name__} 获取股票 {stock_code} 估值数据"
                )
                valuation_data = source_func(stock_code)
                if valuation_data and any(
                    v is not None for v in valuation_data.values()
                ):
                    logger.info(
                        f"从 {source_func.__name__} 成功获取股票 {stock_code} 估值数据"
                    )
                    return valuation_data
            except Exception as e:
                logger.warning(
                    f"从 {source_func.__name__} 获取股票 {stock_code} 估值数据失败: {e}"
                )
                continue

        logger.warning(f"所有数据源都失败，无法获取股票 {stock_code} 估值数据")
        return {  # type: ignore
            "pe_ratio": None,
            "pb_ratio": None,
            "roe": None,
            "debt_ratio": None,
        }

    def _fetch_dividend_from_sina(self, stock_code):
        """
        从新浪财经获取分红数据

        Args:
            stock_code: 股票代码

        Returns:
            dict: 分红数据
        """
        try:
            # 新浪财经分红页面
            market = "sh" if stock_code.startswith(("6", "5")) else "sz"
            url = f"http://vip.stock.finance.sina.com.cn/corp/go.php/vISSUE_ShareBonus/stockid/{stock_code}.phtml"

            headers = {
                "User-Agent": self.user_agent,
                "Referer": f"http://finance.sina.com.cn/realstock/company/{market}{stock_code}/nc.shtml",
            }

            response = requests.get(url, headers=headers, timeout=self.timeout)
            response.encoding = "gb2312"  # 新浪页面使用gb2312编码

            if response.status_code != 200:
                logger.warning(f"新浪财经分红页面请求失败: {response.status_code}")
                return None

            # 解析HTML，查找分红表格
            soup = BeautifulSoup(response.text, "html.parser")

            # 查找所有表格
            tables = soup.find_all("table")

            dividend_history = []
            latest_dividend = None
            latest_date = None

            # 寻找分红表格 - 首先尝试通过ID查找
            dividend_table = soup.find("table", id="sharebonus_1")
            if not dividend_table:
                # 回退到旧方法：寻找包含"分红"和"派息"文本的表格
                for table in tables:
                    table_text = table.get_text()
                    if "分红" in table_text and "派息" in table_text:
                        dividend_table = table
                        break

            if not dividend_table:
                logger.warning(f"未在新浪财经页面找到股票 {stock_code} 的分红表格")
                return None

            # 解析表格行
            rows = dividend_table.find_all("tr")

            # 寻找表头行（包含"送股"、"转增"、"派息"等关键词）
            header_row_index = -1
            for i, row in enumerate(rows):
                row_text = row.get_text()
                if "派息" in row_text and ("送股" in row_text or "转增" in row_text):
                    header_row_index = i
                    break

            if header_row_index < 0:
                logger.warning("未找到分红表格的表头行")
                return None

            # 解析数据行（表头行之后的行）
            for i in range(header_row_index + 1, len(rows)):
                row = rows[i]
                cols = row.find_all("td")
                if len(cols) >= 4:
                    try:
                        # 列结构：日期, 送股, 转增, 派息(税前), 状态, 除权除息日, 股权登记日, ...
                        date = cols[0].text.strip()
                        stock_div = cols[1].text.strip()
                        cap_div = cols[2].text.strip()
                        cash_div_per_10 = cols[3].text.strip()

                        # 跳过没有现金分红的数据
                        if not cash_div_per_10 or cash_div_per_10 == "--":
                            continue

                        # 转换现金分红（从每10股到每股）
                        try:
                            cash_div_per_10_float = float(cash_div_per_10)
                            dividend_per_share = cash_div_per_10_float / 10.0

                            dividend_info = {
                                "date": date,
                                "stock_dividend": stock_div,
                                "capitalization": cap_div,
                                "cash_dividend_per_10": cash_div_per_10_float,
                                "dividend_per_share": dividend_per_share,
                                "scheme": f"10派{cash_div_per_10_float}元",
                                "dividend_per_10": cash_div_per_10_float,
                            }

                            dividend_history.append(dividend_info)

                            # 更新最新分红
                            if not latest_dividend or date > latest_date:
                                latest_dividend = dividend_per_share
                                latest_date = date

                        except ValueError as e:
                            logger.debug(
                                f"解析分红数值失败: {cash_div_per_10}, 错误: {e}"
                            )
                            continue

                    except Exception as e:
                        logger.debug(f"解析行 {i} 失败: {e}")
                        continue

            if latest_dividend:
                # 计算过去365天的分红总和
                one_year_ago = datetime.now() - timedelta(days=365)
                annual_dividend_sum = 0.0

                for dividend_info in dividend_history:
                    try:
                        # 解析分红日期，尝试多种格式
                        date_str = dividend_info.get("date")
                        if not date_str:
                            continue

                        # 尝试常见日期格式
                        dividend_date = None
                        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y年%m月%d日"):
                            try:
                                dividend_date = datetime.strptime(date_str, fmt)
                                break
                            except ValueError:
                                continue

                        if dividend_date is None:
                            logger.debug(f"无法解析分红日期格式: {date_str}")
                            continue

                        # 检查是否在过去365天内
                        if dividend_date >= one_year_ago:
                            annual_dividend_sum += dividend_info.get(
                                "dividend_per_share", 0.0
                            )

                    except Exception as e:
                        logger.debug(f"处理分红记录时出错: {e}")
                        continue

                # 使用年度分红总和作为dividend_per_share（符合"最近一年每股分红"定义）
                dividend_per_share = (
                    annual_dividend_sum if annual_dividend_sum > 0 else None
                )

                logger.info(
                    f"从新浪财经成功获取股票 {stock_code} 的分红数据: 年度总和={annual_dividend_sum:.4f}元/股, 最新单次={latest_dividend:.4f}元/股 (日期: {latest_date})"
                )
                return {
                    "dividend_per_share": dividend_per_share,
                    "last_dividend_date": latest_date,
                    "dividend_history": dividend_history,
                    "latest_dividend": latest_dividend,  # 保留最新单次分红供参考
                }
            else:
                logger.warning(f"未在新浪财经页面找到股票 {stock_code} 的分红数据")
                return None

        except Exception as e:
            logger.warning(f"从新浪财经获取分红数据失败: {e}")
            return None

    def _fetch_dividend_from_eastmoney(self, stock_code):
        """
        从东方财富获取分红数据

        Args:
            stock_code: 股票代码

        Returns:
            dict: 分红数据
        """
        try:
            # 东方财富分红API
            market = "0" if stock_code.startswith(("0", "3")) else "1"
            url = "http://f10.eastmoney.com/BonusFinancingAjax/CompanyBonusDetail"
            params = {
                "code": f"{market}.{stock_code}",
                "type": "1",  # 分红类型
            }

            headers = {
                "User-Agent": self.user_agent,
                "Referer": f"http://f10.eastmoney.com/f10_v2/CashDividend.aspx?code={market}.{stock_code}",
            }

            response = requests.get(
                url, params=params, headers=headers, timeout=self.timeout
            )

            if response.status_code != 200:
                logger.warning(f"东方财富分红API请求失败: {response.status_code}")
                return None

            # 尝试解析响应（可能是JSON格式）
            try:
                data = response.json()
                # 东方财富API返回格式可能变化，需要根据实际响应调整
                logger.debug(f"东方财富分红API响应: {data}")

                # 这里需要根据实际API响应解析分红数据
                # 暂时返回None，需要进一步分析API格式
                logger.info(
                    "东方财富分红API返回数据，但解析逻辑需要根据实际API格式实现"
                )
                return None

            except Exception as e:
                logger.warning(f"解析东方财富分红API响应失败: {e}")
                return None

        except Exception as e:
            logger.warning(f"从东方财富获取分红数据失败: {e}")
            return None


# End of file
