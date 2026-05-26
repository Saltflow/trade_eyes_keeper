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
        # 记录最后一次成功的数据源名称（供 DataSource 交叉验证使用）
        self._last_source_name = None

    def _detect_market(self, stock_code):
        """
        检测股票市场类型

        Args:
            stock_code: 股票代码

        Returns:
            str: "a_share", "us", "hk", "sg", or "unknown"
        """
        stock_code = str(stock_code).upper()

        # 新加坡股市检测 (包含.SI或si后缀，或SG前缀优先)
        if stock_code.endswith((".SI", ".si")) or stock_code.startswith(("SG", "sg")):
            return "sg"

        # 港股检测 (5位数字，或包含hk/HK前缀)
        if stock_code.startswith(("HK", "hk")) or (
            len(stock_code) == 5 and stock_code.isdigit()
        ):
            return "hk"

        # A股检测 (6位数字)
        if stock_code.isdigit() and len(stock_code) == 6:
            return "a_share"

        # 美股检测 (字母开头或包含us/US前缀，长度<=5，且不是其他市场)
        if stock_code.startswith(("US", "us")) or (
            not stock_code[0].isdigit() and len(stock_code) <= 5
        ):
            # 特殊处理：某些股票代码同时符合美股和新股市特征，需要看配置或更智能的判断
            # 这里先默认美股，用户可以通过添加.SI后缀明确指定新加坡
            return "us"

        return "unknown"

    def _normalize_stock_code(self, stock_code, market=None):
        """
        标准化股票代码为数据源所需格式

        Args:
            stock_code: 原始股票代码
            market: 市场类型（可选，自动检测）

        Returns:
            tuple: (market, qq_symbol, sina_symbol, eastmoney_secid, yahoo_symbol)
        """
        stock_code = str(stock_code).upper()

        if market is None:
            market = self._detect_market(stock_code)

        qq_symbol = ""
        sina_symbol = ""
        eastmoney_secid = ""
        yahoo_symbol = ""

        if market == "us":
            # 去除us前缀
            code = stock_code.replace("US", "").replace("us", "")
            qq_symbol = f"us{code}" if not code.startswith("us") else code
            sina_symbol = f"gb_{code.lower()}"
            # 美股代码通常需要后缀，如.OQ, .N等，这里先简化处理
            eastmoney_secid = f"105.{code}"
            yahoo_symbol = code

        elif market == "hk":
            # 去除hk前缀
            code = stock_code.replace("HK", "").replace("hk", "")
            # 确保5位数字
            if code.isdigit() and len(code) < 5:
                code = code.zfill(5)
            qq_symbol = f"hk{code}"
            sina_symbol = f"hk{code}"
            eastmoney_secid = f"116.{code}"
            # 雅虎港股统一补齐4位，如00883→0883.HK
            if code.isdigit():
                yahoo_code = code.lstrip("0") or "0"
                yahoo_code = yahoo_code.zfill(4)
            else:
                yahoo_code = code
            yahoo_symbol = f"{yahoo_code}.HK"

        elif market == "sg":
            # 新加坡股市：去除SG前缀和.SI后缀
            code = (
                stock_code.replace("SG", "")
                .replace("sg", "")
                .replace(".SI", "")
                .replace(".si", "")
            )
            qq_symbol = f"sg{code}"
            sina_symbol = f"sg_{code}"
            eastmoney_secid = f"117.{code}"
            yahoo_symbol = f"{code}.SI"

        elif market == "a_share":
            # A股原有逻辑
            if stock_code.startswith("6") or stock_code.startswith("5"):
                market_prefix_sh = "sh"
                market_prefix_em = "1"
            else:
                market_prefix_sh = "sz"
                market_prefix_em = "0"
            qq_symbol = f"{market_prefix_sh}{stock_code}"
            sina_symbol = f"{market_prefix_sh}{stock_code}"
            eastmoney_secid = f"{market_prefix_em}.{stock_code}"
            yahoo_symbol = stock_code

        return market, qq_symbol, sina_symbol, eastmoney_secid, yahoo_symbol

    def _is_etf(self, stock_code):
        """
        判断是否为ETF基金

        Args:
            stock_code: 股票代码

        Returns:
            bool: True如果是ETF，否则False
        """
        try:
            from .utils.etf_detector import is_etf
        except ImportError:
            from utils.etf_detector import is_etf

        stock_code = str(stock_code).upper()
        market = self._detect_market(stock_code)

        if market == "a_share":
            # A股ETF检测使用统一工具
            code = stock_code.replace("SH", "").replace("SZ", "")
            return is_etf(code)

        elif market == "us":
            # 美股常见ETF代码
            us_etfs = {
                "VOO",
                "SPY",
                "QQQ",
                "DIA",
                "IWM",
                "EFA",
                "EEM",
                "VTI",
                "BND",
                "XLF",
            }
            code = stock_code.replace("US", "")
            if code in us_etfs:
                return True

        elif market == "hk":
            # 港股ETF检测 (简化版)
            code = stock_code.replace("HK", "")
            if code.startswith(("2800", "3033", "2822", "2828")):
                return True

        return False

    def fetch_stock_data(self, stock_code, days=120, source_name=None):
        """
        获取股票历史数据

        Args:
            stock_code: 股票代码
            days: 需要的历史天数
            source_name: 指定数据源名称（如 "_fetch_from_qq"），跳过 fallback 链直接使用该源

        Returns:
            pandas.DataFrame: 股票历史数据
        """
        self._last_source_name = None  # 重置
        stock_code = str(stock_code)

        # 指定数据源模式：跳过 fallback，直接调用指定源
        if source_name is not None:
            source_func = getattr(self, source_name, None)
            if source_func is None:
                logger.error(f"未知数据源: {source_name}")
                return pd.DataFrame()
            try:
                logger.info(f"指定数据源 {source_name} 获取股票 {stock_code} 数据")
                data = source_func(stock_code, days)
                if data is not None and not data.empty:
                    self._last_source_name = source_name
                    data["stock_code"] = stock_code
                    logger.info(
                        f"从 {source_name} 成功获取股票 {stock_code} 的 {len(data)} 条数据"
                    )
                    return data
                logger.warning(f"指定数据源 {source_name} 返回空数据: {stock_code}")
            except Exception as e:
                logger.warning(f"指定数据源 {source_name} 失败: {e}")
            return pd.DataFrame()

        market = self._detect_market(stock_code)
        logger.info(f"检测到股票 {stock_code} 属于 {market} 市场")

        # 根据市场选择数据源
        if market in ("us", "hk", "sg"):
            # 美港股新：
            #   东方财富（支持美股secid=105.*、港股secid=116.*，国内可访问）
            #   雅虎财经（真实历史数据，但国内服务器可能被屏蔽）
            #   腾讯财经国际版（备用，仅返回1条实时数据）
            if market == "sg":
                # 新加坡：雅虎（唯一有历史数据的源，国内可能403）→ QQ国际（仅实时）
                data_sources = [
                    self._fetch_from_yahoo,  # 雅虎（唯一有历史数据的源）
                    self._fetch_from_qq_international,  # 腾讯国际版（仅实时）
                ]
            else:
                # 美股：东方财富优先 → 新浪美股K线（国内可稳定访问）→ Yahoo（备用）
                if market == "us":
                    data_sources = [
                        self._fetch_from_eastmoney,  # 东方财富（有完整历史+复权数据）
                        self._fetch_from_sina_us,  # 新浪美股K线API（国内稳定，历史完整）
                        self._fetch_from_yahoo,  # 雅虎财经（备用，国内可能403）
                        self._fetch_from_qq_international,  # 腾讯国际版（仅1条实时数据）
                    ]
                    logger.info(f"美股 {stock_code} 使用东方财富+新浪+雅虎数据源")
                else:
                    # 港股：东方财富优先 → 新浪港股（国内稳定，有完整历史）→ 腾讯财经 → Yahoo
                    data_sources = [
                        self._fetch_from_eastmoney,  # 东方财富（港股secid=116.*）
                        self._fetch_from_sina_hk,  # 新浪港股历史API（国内稳定）
                        self._fetch_from_qq,  # 腾讯财经历史API（支持港股）
                        self._fetch_from_yahoo,  # 雅虎财经（备用，国内可能403）
                        self._fetch_from_qq_international,  # 腾讯国际版（仅实时）
                    ]
                    logger.info(
                        f"港股 {stock_code} 使用东方财富+新浪港股+腾讯+雅虎数据源"
                    )
        elif market == "a_share":
            # A股原有逻辑
            if self._is_etf(stock_code):
                data_sources = [
                    self._fetch_from_qq,  # 腾讯财经（有历史数据API，使用qfq前复权）
                    self._fetch_from_eastmoney,  # 东方财富（使用fqt=1前复权）
                    self._fetch_from_sina,  # 新浪财经（没有复权参数，备用）
                ]
                logger.info(
                    f"ETF {stock_code} 检测到，调整数据源顺序优先使用复权数据源"
                )
            else:
                data_sources = [
                    self._fetch_from_sina,  # 新浪财经（有历史数据API）
                    self._fetch_from_qq,  # 腾讯财经（有历史数据API）
                    self._fetch_from_eastmoney,  # 东方财富（API常失败）
                ]
        else:
            logger.warning(f"无法识别股票 {stock_code} 的市场类型，尝试默认数据源")
            data_sources = [
                self._fetch_from_qq_international,
                self._fetch_from_sina,
                self._fetch_from_qq,
            ]

        source_func_name = ""
        for source_func in data_sources:
            try:
                source_func_name = source_func.__name__
                logger.info(f"尝试从 {source_func_name} 获取股票 {stock_code} 数据")
                data = source_func(stock_code, days)
                if data is not None and not data.empty:
                    logger.info(
                        f"从 {source_func_name} 成功获取股票 {stock_code} 的 {len(data)} 条数据"
                    )
                    self._last_source_name = source_func_name
                    data["stock_code"] = stock_code

                    return data
            except Exception as e:
                logger.warning(
                    f"从 {source_func_name} 获取股票 {stock_code} 数据失败: {e}"
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
            # 统一使用标准化函数获取东方财富secid（支持A股/美股/港股/新加坡）
            market, _, _, eastmoney_secid, _ = self._normalize_stock_code(stock_code)
            if not eastmoney_secid:
                logger.warning(f"无法确定股票 {stock_code} 的东方财富secid")
                return pd.DataFrame()

            # 构建待尝试的secid列表（美股可能有不同市场前缀：105=NASDAQ, 106=NYSE, 107=AMEX）
            secids_to_try = [eastmoney_secid]
            if market == "us":
                code = stock_code.upper().replace("US", "")
                for prefix in ["106", "107", "108"]:
                    alt_secid = f"{prefix}.{code}"
                    if alt_secid not in secids_to_try:
                        secids_to_try.append(alt_secid)

            # 东方财富日线数据API
            end_date = datetime.now().strftime("%Y%m%d")
            start_date = (datetime.now() - timedelta(days=days + 30)).strftime(
                "%Y%m%d"
            )  # 多取一些

            url = "http://push2his.eastmoney.com/api/qt/stock/kline/get"

            headers = {
                "User-Agent": self.user_agent,
                "Referer": "http://quote.eastmoney.com/",
            }

            last_error = None
            for secid in secids_to_try:
                try:
                    params = {
                        "secid": secid,
                        "fields1": "f1,f2,f3,f4,f5,f6",
                        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                        "klt": "101",  # 日线
                        "fqt": "1",  # 前复权
                        "beg": start_date,
                        "end": end_date,
                        "lmt": "10000",  # 足够大的数量
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
                                        "amplitude": float(items[7])
                                        if items[7]
                                        else 0.0,
                                        "change_pct": float(items[8])
                                        if items[8]
                                        else 0.0,
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
                            logger.info(
                                f"从东方财富({secid})成功获取股票 {stock_code} 的 {len(df)} 条数据"
                            )
                            return df
                    else:
                        logger.debug(f"东方财富secid={secid} 返回空数据，尝试下一个")

                except Exception as e:
                    last_error = e
                    logger.debug(f"东方财富secid={secid} 请求失败: {e}，尝试下一个")
                    continue

            # 所有secid都失败了
            if last_error:
                logger.warning(
                    f"从东方财富获取股票 {stock_code} 数据失败(已尝试{len(secids_to_try)}个secid): {last_error}"
                )

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

            # 获取股票名称（通过实时行情API提取简称）
            try:
                name_url = f"http://hq.sinajs.cn/list={market}{stock_code}"
                name_headers = {
                    "User-Agent": self.user_agent,
                    "Referer": "http://finance.sina.com.cn/",
                }
                name_resp = requests.get(name_url, headers=name_headers, timeout=5)
                name_resp.raise_for_status()
                name_match = re.search(r'="(.+)"', name_resp.text)
                if name_match:
                    name_items = name_match.group(1).split(",")
                    if name_items and name_items[0].strip():
                        df["stock_name"] = name_items[0].strip()
            except Exception as e:
                logger.debug(f"名称获取失败 (不影响数据): {e}")

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
                    df["stock_name"] = items[0].strip()

                    # 无法获取真实历史数据，返回空DataFrame（不生成模拟数据）
                    logger.warning(
                        f"获取到股票 {stock_code} 当前数据，但无法获取真实历史数据，无法计算MA60"
                    )
            return pd.DataFrame()

        except Exception as e:
            logger.warning(f"从新浪财经实时数据获取股票 {stock_code} 数据失败: {e}")

        return pd.DataFrame()

    def _fetch_from_qq_international(self, stock_code, days):
        """
        从腾讯财经获取国际市场（美股/港股）数据

        Args:
            stock_code: 股票代码
            days: 历史天数

        Returns:
            pandas.DataFrame: 股票数据
        """
        try:
            market, qq_symbol, _, _, _ = self._normalize_stock_code(stock_code)

            # 第一步：获取实时数据
            url = f"http://qt.gtimg.cn/q={qq_symbol}"
            headers = {"User-Agent": self.user_agent}
            response = requests.get(url, headers=headers, timeout=self.timeout)
            response.raise_for_status()

            content = response.text
            if "=" not in content:
                logger.warning(f"腾讯财经国际实时数据格式异常: {content[:100]}")
                return pd.DataFrame()

            data_str = content.split("=", 1)[1].strip('";')
            items = data_str.split("~")

            if len(items) < 30:
                logger.warning(f"腾讯财经国际实时数据字段不足: {len(items)}")
                return pd.DataFrame()

            # 解析实时数据
            current_data = {
                "date": datetime.now().strftime("%Y-%m-%d"),
                "open": float(items[5]) if items[5] else 0.0,
                "close": float(items[3]) if items[3] else 0.0,
                "high": float(items[33]) if (len(items) > 33 and items[33]) else 0.0,
                "low": float(items[34]) if (len(items) > 34 and items[34]) else 0.0,
                "volume": float(items[6]) if items[6] else 0.0,
                "amount": float(items[37]) if (len(items) > 37 and items[37]) else 0.0,
                "amplitude": (float(items[33]) - float(items[34]))
                / float(items[5])
                * 100
                if (len(items) > 34 and items[5] and float(items[5]) > 0)
                else 0.0,
                "change_pct": float(items[32])
                if (len(items) > 32 and items[32])
                else 0.0,
                "change": float(items[31]) if (len(items) > 31 and items[31]) else 0.0,
                "turnover": 0.0,
            }

            # 提取股票名称（腾讯格式 items[1] 为股票简称）
            stock_name = (
                items[1].strip() if len(items) > 1 and items[1].strip() else stock_code
            )

            # 第二步：尝试获取历史数据（腾讯国际历史数据接口格式不同，先用最近30天模拟）
            # 实际生产环境可以接入雅虎财经等补充历史数据
            records = [current_data]

            df = pd.DataFrame(records)
            df["date"] = pd.to_datetime(df["date"])
            df["stock_name"] = stock_name
            df = df.sort_values("date").reset_index(drop=True)

            # 计算涨跌幅
            if len(df) > 1:
                df["change_pct"] = df["close"].pct_change() * 100
                df.loc[0, "change_pct"] = current_data["change_pct"]

            logger.info(f"从腾讯财经国际版获取股票 {stock_code} 的 {len(df)} 条数据")
            return df

        except Exception as e:
            logger.warning(f"从腾讯财经国际版获取股票 {stock_code} 数据失败: {e}")
            return pd.DataFrame()

    def _fetch_from_yahoo(self, stock_code, days):
        """
        从雅虎财经获取股票数据（备用方案）

        Args:
            stock_code: 股票代码
            days: 历史天数

        Returns:
            pandas.DataFrame: 股票数据
        """
        try:
            # 使用新的代码标准化方法获取雅虎格式
            market, _, _, _, yahoo_symbol = self._normalize_stock_code(stock_code)

            # 雅虎财经API
            end_date = int(datetime.now().timestamp())
            start_date = int((datetime.now() - timedelta(days=days + 30)).timestamp())

            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}"
            params = {
                "period1": start_date,
                "period2": end_date,
                "interval": "1d",
                "events": "history",
            }
            headers = {
                "User-Agent": self.user_agent,
            }

            response = requests.get(
                url, params=params, headers=headers, timeout=self.timeout
            )
            response.raise_for_status()

            data = response.json()
            chart_data = data.get("chart", {}).get("result", [])

            if not chart_data:
                logger.warning(f"雅虎财经返回空数据: {yahoo_symbol}")
                return pd.DataFrame()

            quotes = chart_data[0].get("indicators", {}).get("quote", [{}])[0]
            timestamps = chart_data[0].get("timestamp", [])

            if not timestamps or not quotes:
                return pd.DataFrame()

            records = []
            for i, ts in enumerate(timestamps):
                date = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                records.append(
                    {
                        "date": date,
                        "open": quotes.get("open", [])[i]
                        if i < len(quotes.get("open", []))
                        else None,
                        "close": quotes.get("close", [])[i]
                        if i < len(quotes.get("close", []))
                        else None,
                        "high": quotes.get("high", [])[i]
                        if i < len(quotes.get("high", []))
                        else None,
                        "low": quotes.get("low", [])[i]
                        if i < len(quotes.get("low", []))
                        else None,
                        "volume": quotes.get("volume", [])[i]
                        if i < len(quotes.get("volume", []))
                        else 0.0,
                        "amount": 0.0,
                        "amplitude": 0.0,
                        "change_pct": 0.0,
                        "change": 0.0,
                        "turnover": 0.0,
                    }
                )

            # 过滤无效数据
            valid_records = [r for r in records if r["close"] is not None]
            if not valid_records:
                return pd.DataFrame()

            df = pd.DataFrame(valid_records)
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").reset_index(drop=True)

            # 计算振幅和涨跌幅
            if len(df) > 1:
                df["amplitude"] = (df["high"] - df["low"]) / df["open"] * 100
                df["change_pct"] = df["close"].pct_change() * 100
                df["change"] = df["close"].diff()

            # 通过Yahoo search API获取股票名称（v7 quote已锁，search可正常使用）
            try:
                search_url = f"https://query1.finance.yahoo.com/v1/finance/search?q={yahoo_symbol}"
                search_resp = requests.get(
                    search_url, headers=headers, timeout=self.timeout
                )
                search_resp.raise_for_status()
                search_data = search_resp.json()
                quotes = search_data.get("quotes", [])
                if quotes:
                    stock_name = quotes[0].get(
                        "shortname", quotes[0].get("longname", "")
                    )
                    if stock_name:
                        df["stock_name"] = stock_name
            except Exception as e:
                logger.debug(f"名称获取失败 (不影响数据): {e}")

            logger.info(f"从雅虎财经获取股票 {stock_code} 的 {len(df)} 条数据")
            return df

        except Exception as e:
            logger.warning(f"从雅虎财经获取股票 {stock_code} 数据失败: {e}")
            return pd.DataFrame()

    def _fetch_from_sina_us(self, stock_code, days):
        """
        从新浪财经美股K线API获取历史数据

        接口：US_MinKService.getDailyK
        参数 symbol 直接使用原始股票代码（如 VOO, UPRO）
        返回完整历史数据，适合用作东方财富/雅虎的备用源

        Args:
            stock_code: 美股股票代码（如 VOO, UPRO, AAPL）
            days: 需要的天数（接口本身返回全部历史，此参数仅控制截取行数）

        Returns:
            pandas.DataFrame: 股票历史数据
        """
        try:
            # 去除可能的 US 前缀
            code = stock_code.upper().replace("US", "")

            url = (
                "http://stock.finance.sina.com.cn/usstock/"
                "api/json_v2.php/US_MinKService.getDailyK"
            )
            params = {
                "symbol": code,
                "type": "daily",
            }
            headers = {
                "User-Agent": self.user_agent,
                "Referer": "http://finance.sina.com.cn",
            }

            response = requests.get(
                url, params=params, headers=headers, timeout=self.timeout
            )
            response.raise_for_status()

            data_json = response.json()
            if not data_json or not isinstance(data_json, list):
                logger.warning(f"新浪美股K线API返回空数据: {code}")
                return pd.DataFrame()

            records = []
            for item in data_json:
                try:
                    records.append(
                        {
                            "date": item["d"],
                            "open": float(item["o"]),
                            "close": float(item["c"]),
                            "high": float(item["h"]),
                            "low": float(item["l"]),
                            "volume": int(float(item["v"])),
                            "amount": float(item["a"]) if item.get("a") else 0.0,
                        }
                    )
                except (KeyError, ValueError, TypeError) as parse_err:
                    logger.debug(f"解析新浪美股K线行失败: {parse_err}, item={item}")
                    continue

            if not records:
                logger.warning(f"新浪美股K线API无有效数据: {code}")
                return pd.DataFrame()

            df = pd.DataFrame(records)
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").reset_index(drop=True)

            # 只保留需要的天数（+30天确保有足够数据计算指标）
            if len(df) > days + 30:
                df = df.tail(days + 30).reset_index(drop=True)

            # 计算派生字段（与雅虎数据源一致）
            if len(df) > 1:
                df["amplitude"] = (df["high"] - df["low"]) / df["open"] * 100
                df["change_pct"] = df["close"].pct_change() * 100
                df["change"] = df["close"].diff()
            else:
                df["amplitude"] = 0.0
                df["change_pct"] = 0.0
                df["change"] = 0.0
            df["turnover"] = 0.0

            # 通过新浪实时行情获取股票名称
            try:
                name_url = f"http://hq.sinajs.cn/list=gb_{code.lower()}"
                name_resp = requests.get(
                    name_url,
                    headers={"Referer": "http://finance.sina.com.cn"},
                    timeout=self.timeout,
                )
                if name_resp.status_code == 200:
                    # 响应格式: var hq_str_gb_voo="名称,价格,..."
                    text = name_resp.text
                    if "=" in text:
                        quote_str = text.split("=", 1)[1].strip().strip('"').strip("'")
                        parts = quote_str.split(",")
                        if parts and parts[0]:
                            stock_name = parts[0].strip()
                            if stock_name:
                                df["stock_name"] = stock_name
            except Exception as e:
                logger.debug(f"名称获取失败 (不影响数据): {e}")

            logger.info(f"从新浪美股K线API获取股票 {stock_code} 的 {len(df)} 条数据")
            return df

        except Exception as e:
            logger.warning(f"从新浪美股K线API获取股票 {stock_code} 数据失败: {e}")
            return pd.DataFrame()

    def _fetch_from_sina_hk(self, stock_code, days):
        """
        从新浪财经获取港股历史数据

        接口：CN_MarketData.getKLineData（与A股历史API相同）
        参数 symbol 使用 hk 前缀（如 hk00883）
        返回完整历史数据，适合用作东方财富/雅虎的备用源

        Args:
            stock_code: 港股股票代码（如 00883, 01816）
            days: 需要的天数

        Returns:
            pandas.DataFrame: 股票历史数据
        """
        try:
            # 获取港股 symbol（hk + 5位数字代码）
            market, _, sina_symbol, _, _ = self._normalize_stock_code(stock_code)
            if market != "hk":
                logger.warning(f"非港股代码: {stock_code}")
                return pd.DataFrame()

            code = stock_code.replace("HK", "").replace("hk", "")

            # 新浪财经港股历史数据API（与A股同一接口，symbol不同）
            url = "http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
            params = {
                "symbol": sina_symbol,  # 如 hk00883
                "scale": "240",  # 日线
                "datalen": str(days),
            }
            headers = {
                "User-Agent": self.user_agent,
                "Referer": "http://finance.sina.com.cn/",
            }

            response = requests.get(
                url, params=params, headers=headers, timeout=self.timeout
            )
            if response.status_code != 200:
                logger.warning(
                    f"新浪港股历史API返回状态码 {response.status_code}: {code}"
                )
                return pd.DataFrame()

            data_list = response.json()
            if not data_list or not isinstance(data_list, list):
                logger.warning(f"新浪港股历史API返回空数据: {code}")
                return pd.DataFrame()

            # 转换为DataFrame（格式与A股一致）
            records = []
            for item in data_list:
                try:
                    records.append(
                        {
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
                            "turnover": 0.0,
                        }
                    )
                except (KeyError, ValueError, TypeError) as parse_err:
                    logger.debug(f"解析新浪港股K线行失败: {parse_err}, item={item}")
                    continue

            if not records:
                logger.warning(f"新浪港股历史API无有效数据: {code}")
                return pd.DataFrame()

            df = pd.DataFrame(records)
            df["date"] = pd.to_datetime(df["date"])

            # 计算涨跌幅
            if len(df) > 1:
                df["change_pct"] = df["close"].pct_change() * 100
                if len(df) > 0:
                    df.loc[0, "change_pct"] = (
                        (df.loc[0, "close"] - df.loc[0, "open"])
                        / df.loc[0, "open"]
                        * 100
                    )

            df = df.sort_values("date")

            # 通过新浪实时行情获取股票名称（港股 symbol 为 hk00883）
            try:
                name_url = f"http://hq.sinajs.cn/list={sina_symbol}"
                name_headers = {
                    "User-Agent": self.user_agent,
                    "Referer": "http://finance.sina.com.cn/",
                }
                name_resp = requests.get(name_url, headers=name_headers, timeout=5)
                name_resp.raise_for_status()
                name_match = re.search(r'="(.+)"', name_resp.text)
                if name_match:
                    name_items = name_match.group(1).split(",")
                    if name_items and name_items[0].strip():
                        df["stock_name"] = name_items[0].strip()
            except Exception as e:
                logger.debug(f"名称获取失败 (不影响数据): {e}")

            logger.info(f"从新浪港股历史API获取股票 {stock_code} 的 {len(df)} 条数据")
            return df

        except Exception as e:
            logger.warning(f"从新浪港股历史API获取股票 {stock_code} 数据失败: {e}")
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
        支持 A 股（sh/sz 前缀）和港股（hk 前缀）
        """
        try:
            # 通过 _normalize_stock_code 获取正确的 QQ symbol
            raw_market, qq_symbol, _, _, _ = self._normalize_stock_code(stock_code)
            if raw_market == "us":
                # 美股走其他数据源，QQ 不提供美股历史
                return pd.DataFrame()
            if raw_market == "hk":
                # 港股使用 hk 前缀（如 hk00883）
                symbol = qq_symbol
            elif raw_market == "a_share":
                market = (
                    "sh"
                    if stock_code.startswith("6") or stock_code.startswith("5")
                    else "sz"
                )
                symbol = f"{market}{stock_code}"
            else:
                logger.warning(
                    f"腾讯财经历史API不支持该市场: {stock_code} (market={raw_market})"
                )
                return pd.DataFrame()

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
                    if stock_data:
                        # A 股走 qfqday（前复权），港股走 day（未复权）
                        kline_data = stock_data.get("qfqday") or stock_data.get("day")
                        if kline_data:
                            records = []
                            for item in kline_data:
                                # 每个item格式: ["2025-08-29","7.379","7.429","7.439","7.359","1237045.000"]
                                # 可能还有额外字段，我们只取前6个
                                if len(item) >= 5:
                                    record = {
                                        "date": item[0],
                                        "open": float(item[1]),
                                        "close": float(item[2]),
                                        "high": float(item[3]),
                                        "low": float(item[4]),
                                        "volume": float(item[5])
                                        if len(item) > 5
                                        else 0.0,
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
                                if len(df) > 0:
                                    df.loc[0, "change_pct"] = (
                                        (df.loc[0, "close"] - df.loc[0, "open"])
                                        / df.loc[0, "open"]
                                        * 100
                                    )

                            df = df.sort_values("date")

                            # 获取股票名称（通过QQ实时行情API提取简称）
                            try:
                                name_url = f"http://qt.gtimg.cn/q={symbol}"
                                name_resp = requests.get(
                                    name_url, headers=headers, timeout=5
                                )
                                name_items = name_resp.text.split("~")
                                if len(name_items) > 1 and name_items[1].strip():
                                    df["stock_name"] = name_items[1].strip()
                            except Exception as e:
                                logger.debug(f"名称获取失败 (不影响数据): {e}")

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
        获取股票估值指标数据（PE, PB）
        使用腾讯财经(QQ)实时API获取估值指标，ROE 由调用方根据 PB/PE 计算。

        Args:
            stock_code: 股票代码

        Returns:
            dict: 包含估值指标的字典，键包括：
                - pe_ratio: 市盈率 (items[39])
                - pb_ratio: 市净率 (items[46])
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
                return value

            # QQ实时数据字段映射:
            # 39: 市盈率(PE, TTM), 46: 市净率(PB)
            # 注意: items[52] 是动态PE，items[53] 是静态PE，均不直接用于 ROE/负债率
            pe = safe_float(items[39]) if len(items) > 39 else None
            pb = safe_float(items[46]) if len(items) > 46 else None

            pe = validate_metric(pe, "pe")
            pb = validate_metric(pb, "pb")

            return {"pe_ratio": pe, "pb_ratio": pb}

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
                    if len(items) > 46:
                        return parse_qq_items(items)
            except Exception as e:
                logger.warning(f"从腾讯财经获取股票 {stock_code} 估值数据失败: {e}")
            return None

        # 尝试多个数据源
        data_sources = [fetch_from_qq]

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
