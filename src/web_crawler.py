"""
网页爬虫模块
从公开网站获取股票真实数据
"""

import logging
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import requests
import time
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
        data_sources = [
            self._fetch_from_sina,       # 新浪财经（有历史数据API）
            self._fetch_from_qq,         # 腾讯财经（有历史数据API）
            self._fetch_from_eastmoney,  # 东方财富（API常失败）
        ]
        
        for source_func in data_sources:
            try:
                logger.info(f"尝试从 {source_func.__name__} 获取股票 {stock_code} 数据")
                data = source_func(stock_code, days)
                if data is not None and not data.empty:
                    logger.info(f"从 {source_func.__name__} 成功获取股票 {stock_code} 的 {len(data)} 条数据")
                    
                    # 计算MA60
                    if 'close' in data.columns:
                        data['ma60'] = data['close'].rolling(window=60, min_periods=1).mean()
                    data['stock_code'] = stock_code
                    
                    return data
            except Exception as e:
                logger.warning(f"从 {source_func.__name__} 获取股票 {stock_code} 数据失败: {e}")
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
            market = "0" if stock_code.startswith(('0', '3')) else "1"  # 0:深市, 1:沪市
            
            # 东方财富日线数据API
            end_date = datetime.now().strftime('%Y%m%d')
            start_date = (datetime.now() - timedelta(days=days+30)).strftime('%Y%m%d')  # 多取一些
            
            url = f"http://push2his.eastmoney.com/api/qt/stock/kline/get"
            params = {
                'secid': f'{market}.{stock_code}',
                'fields1': 'f1,f2,f3,f4,f5,f6',
                'fields2': 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61',
                'klt': '101',  # 日线
                'fqt': '1',    # 前复权
                'beg': start_date,
                'end': end_date,
                'lmt': '10000'  # 足够大的数量
            }
            
            headers = {
                'User-Agent': self.user_agent,
                'Referer': 'http://quote.eastmoney.com/'
            }
            
            response = requests.get(url, params=params, headers=headers, timeout=self.timeout)
            response.raise_for_status()
            
            data_json = response.json()
            if data_json.get('data') and data_json['data'].get('klines'):
                klines = data_json['data']['klines']
                
                data_list = []
                for kline in klines:
                    items = kline.split(',')
                    if len(items) >= 11:
                        data_list.append({
                            'date': items[0],
                            'open': float(items[1]),
                            'close': float(items[2]),
                            'high': float(items[3]),
                            'low': float(items[4]),
                            'volume': float(items[5]),
                            'amount': float(items[6]),
                            'amplitude': float(items[7]) if items[7] else 0.0,
                            'change_pct': float(items[8]) if items[8] else 0.0,
                            'change': float(items[9]) if items[9] else 0.0,
                            'turnover': float(items[10]) if len(items) > 10 and items[10] else 0.0
                        })
                
                if data_list:
                    df = pd.DataFrame(data_list)
                    df['date'] = pd.to_datetime(df['date'])
                    df = df.sort_values('date')
                    return df
                    
        except Exception as e:
            logger.warning(f"从东方财富获取股票 {stock_code} 数据失败: {e}")
        
        # 如果API失败，返回空DataFrame（不生成模拟数据）
        logger.warning(f"东方财富API失败，不生成模拟数据")
        return pd.DataFrame()
    
    def _parse_eastmoney_web(self, stock_code, days):
        """
        解析东方财富网页获取数据（备用方案）
        """
        try:
            market = "SZ" if stock_code.startswith(('0', '3')) else "SH"
            url = f"http://quote.eastmoney.com/{market}{stock_code}.html"
            
            headers = {
                'User-Agent': self.user_agent,
                'Referer': f'http://quote.eastmoney.com/{market}{stock_code}.html'
            }
            
            response = requests.get(url, headers=headers, timeout=self.timeout)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 尝试找到价格信息（简化版，实际需要更复杂的解析）
            # 这里只获取最新价格作为示例
            price_elem = soup.find('span', class_='price')
            if price_elem:
                current_price = float(price_elem.text)
                
                # 生成最近days天的模拟数据，但基于真实最新价格
                return self._generate_data_from_price(stock_code, current_price, days)
            
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
                logger.info(f"从新浪财经历史API成功获取股票 {stock_code} 的 {len(historical_data)} 条真实历史数据")
                return historical_data
            else:
                logger.warning(f"新浪财经历史数据API返回空数据，跳过该数据源")
                return pd.DataFrame()
                
        except Exception as e:
            logger.warning(f"从新浪财经获取股票 {stock_code} 数据失败: {e}")
            return pd.DataFrame()
    
    def _fetch_historical_from_sina(self, stock_code, days):
        """
        从新浪财经历史数据API获取真实历史数据
        """
        try:
            market = "sh" if stock_code.startswith('6') or stock_code.startswith('5') else "sz"
            symbol = f"{market}{stock_code}"
            
            # 新浪财经历史数据API
            url = "http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
            params = {
                'symbol': symbol,
                'scale': '240',  # 日线
                'datalen': str(days)  # 数据长度
            }
            
            headers = {
                'User-Agent': self.user_agent,
                'Referer': 'http://finance.sina.com.cn/'
            }
            
            response = requests.get(url, params=params, headers=headers, timeout=self.timeout)
            response.raise_for_status()
            
            # 解析JSON数据
            data_list = response.json()
            
            if not data_list:
                logger.warning(f"新浪财经历史数据API返回空数据")
                return pd.DataFrame()
            
            # 转换为DataFrame
            records = []
            for item in data_list:
                record = {
                    'date': item['day'],
                    'open': float(item['open']),
                    'close': float(item['close']),
                    'high': float(item['high']),
                    'low': float(item['low']),
                    'volume': float(item['volume']),
                    'amount': float(item['volume']) * float(item['close']),  # 估算成交额
                    'amplitude': (float(item['high']) - float(item['low'])) / float(item['open']) * 100 if float(item['open']) > 0 else 0.0,
                    'change_pct': 0.0,  # 稍后计算
                    'change': float(item['close']) - float(item['open']),
                    'turnover': 0.0  # 新浪不提供换手率
                }
                records.append(record)
            
            df = pd.DataFrame(records)
            df['date'] = pd.to_datetime(df['date'])
            
            # 计算涨跌幅（基于前一日收盘价）
            if len(df) > 1:
                df['change_pct'] = df['close'].pct_change() * 100
                # 第一天的涨跌幅用当天变化计算
                if len(df) > 0:
                    df.loc[0, 'change_pct'] = (df.loc[0, 'close'] - df.loc[0, 'open']) / df.loc[0, 'open'] * 100
            
            # 按日期排序
            df = df.sort_values('date')
            
            logger.info(f"从新浪财经历史API获取股票 {stock_code} 的 {len(df)} 条真实历史数据")
            return df
            
        except Exception as e:
            logger.warning(f"从新浪财经历史数据API获取股票 {stock_code} 数据失败: {e}")
            return pd.DataFrame()
    
    def _fetch_realtime_from_sina(self, stock_code, days):
        """
        从新浪财经获取实时数据（备用方案）
        """
        try:
            market = "sz" if stock_code.startswith(('0', '3')) else "sh"
            url = f"http://hq.sinajs.cn/list={market}{stock_code}"
            
            headers = {
                'User-Agent': self.user_agent,
                'Referer': 'http://finance.sina.com.cn/'
            }
            
            response = requests.get(url, headers=headers, timeout=self.timeout)
            response.raise_for_status()
            
            # 解析新浪财经格式
            content = response.text
            match = re.search(r'="(.+)"', content)
            if match:
                data_str = match.group(1)
                items = data_str.split(',')
                
                if len(items) >= 30:
                    # 最新数据
                    current_data = {
                        'date': datetime.now().strftime('%Y-%m-%d'),
                        'open': float(items[1]),
                        'close': float(items[3]),  # 当前价
                        'high': float(items[4]),
                        'low': float(items[5]),
                        'volume': float(items[8]),  # 成交量
                        'amount': float(items[9]),  # 成交额
                        'amplitude': (float(items[4]) - float(items[5])) / float(items[1]) * 100 if float(items[1]) > 0 else 0.0,
                        'change_pct': (float(items[3]) - float(items[2])) / float(items[2]) * 100 if float(items[2]) > 0 else 0.0,
                        'change': float(items[3]) - float(items[2]),
                        'turnover': 0.0  # 新浪不直接提供换手率
                    }
                    
                    # 获取历史数据（新浪历史数据API比较复杂，这里只返回最新数据）
                    df = pd.DataFrame([current_data])
                    df['date'] = pd.to_datetime(df['date'])
                    
                    # 为了计算MA60，我们需要更多历史数据
                    # 这里用简单方法生成基于当前价格的历史序列
                    logger.warning(f"注意：股票 {stock_code} 的历史数据基于当前价格 {current_data['close']:.2f} 生成，非完全真实数据")
                    logger.warning(f"建议：请确保使用真实历史数据进行投资决策")
                    return self._generate_historical_data(stock_code, current_data, days)
            
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
                logger.info(f"从腾讯财经历史API成功获取股票 {stock_code} 的 {len(historical_data)} 条真实历史数据")
                return historical_data
            else:
                logger.warning(f"腾讯财经历史数据API返回空数据，跳过该数据源")
                return pd.DataFrame()
                
        except Exception as e:
            logger.warning(f"从腾讯财经获取股票 {stock_code} 数据失败: {e}")
            return pd.DataFrame()
    
    def _fetch_historical_from_qq(self, stock_code, days):
        """
        从腾讯财经历史数据API获取真实历史数据
        """
        try:
            market = "sh" if stock_code.startswith('6') or stock_code.startswith('5') else "sz"
            symbol = f"{market}{stock_code}"
            
            # 腾讯财经历史数据API
            url = "http://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
            params = {
                'param': f'{symbol},day,,,{days},qfq',  # qfq: 前复权
                '_var': 'kline_day'
            }
            
            headers = {
                'User-Agent': self.user_agent,
                'Referer': 'http://gu.qq.com/'
            }
            
            response = requests.get(url, params=params, headers=headers, timeout=self.timeout)
            response.raise_for_status()
            
            # 解析响应（格式：kline_day={...})
            content = response.text
            if content.startswith('kline_day='):
                json_str = content[len('kline_day='):]
                data = json.loads(json_str)
                
                if data.get('code') == 0 and 'data' in data:
                    stock_data = data['data'].get(symbol)
                    if stock_data and 'qfqday' in stock_data:
                        qfqday = stock_data['qfqday']
                        
                        records = []
                        for item in qfqday:
                            # 每个item格式: ["2025-08-29","7.379","7.429","7.439","7.359","1237045.000"]
                            # 可能还有额外字段，我们只取前6个
                            if len(item) >= 5:
                                record = {
                                    'date': item[0],
                                    'open': float(item[1]),
                                    'close': float(item[2]),
                                    'high': float(item[3]),
                                    'low': float(item[4]),
                                    'volume': float(item[5]) if len(item) > 5 else 0.0,
                                    'amount': 0.0,  # 腾讯不直接提供成交额
                                    'amplitude': (float(item[3]) - float(item[4])) / float(item[1]) * 100 if float(item[1]) > 0 else 0.0,
                                    'change_pct': 0.0,  # 稍后计算
                                    'change': float(item[2]) - float(item[1]),
                                    'turnover': 0.0  # 腾讯不直接提供换手率
                                }
                                records.append(record)
                        
                        df = pd.DataFrame(records)
                        df['date'] = pd.to_datetime(df['date'])
                        
                        # 计算涨跌幅（基于前一日收盘价）
                        if len(df) > 1:
                            df['change_pct'] = df['close'].pct_change() * 100
                            # 第一天的涨跌幅用当天变化计算
                            if len(df) > 0:
                                df.loc[0, 'change_pct'] = (df.loc[0, 'close'] - df.loc[0, 'open']) / df.loc[0, 'open'] * 100
                        
                        # 按日期排序
                        df = df.sort_values('date')
                        
                        logger.info(f"从腾讯财经历史API获取股票 {stock_code} 的 {len(df)} 条真实历史数据")
                        return df
            
            logger.warning(f"腾讯财经历史数据API返回数据格式异常")
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
            market = "sz" if stock_code.startswith(('0', '3')) else "sh"
            url = f"http://qt.gtimg.cn/q={market}{stock_code}"
            
            headers = {
                'User-Agent': self.user_agent
            }
            
            response = requests.get(url, headers=headers, timeout=self.timeout)
            response.raise_for_status()
            
            content = response.text
            items = content.split('~')
            
            if len(items) > 40:
                current_data = {
                    'date': datetime.now().strftime('%Y-%m-%d'),
                    'open': float(items[5]) if items[5] else 0.0,
                    'close': float(items[3]) if items[3] else 0.0,
                    'high': float(items[33]) if items[33] else 0.0,
                    'low': float(items[34]) if items[34] else 0.0,
                    'volume': float(items[6]) if items[6] else 0.0,
                    'amount': float(items[37]) if items[37] else 0.0,
                    'amplitude': float(items[43]) if items[43] else 0.0,
                    'change_pct': float(items[32]) if items[32] else 0.0,
                    'change': float(items[31]) if items[31] else 0.0,
                    'turnover': float(items[38]) if items[38] else 0.0
                }
                
                df = pd.DataFrame([current_data])
                df['date'] = pd.to_datetime(df['date'])
                
                logger.warning(f"注意：股票 {stock_code} 的历史数据基于当前价格 {current_data['close']:.2f} 生成，非完全真实数据")
                logger.warning(f"建议：请确保使用真实历史数据进行投资决策")
                return self._generate_historical_data(stock_code, current_data, days)
                
        except Exception as e:
            logger.warning(f"从腾讯财经实时数据获取股票 {stock_code} 数据失败: {e}")
        
        return pd.DataFrame()
    
    def _generate_historical_data(self, stock_code, current_data, days):
        """
        基于当前价格生成合理的历史数据序列
        用于计算MA60等指标
        """
        try:
            # 使用当前价格作为基准
            base_price = current_data['close']
            
            # 生成日期序列（最近days个工作日）
            end_date = datetime.now()
            date_range = pd.date_range(end=end_date, periods=days, freq='B')  # B表示工作日
            
            # 根据股票类型设置合理波动
            if stock_code == '601728':  # 中国电信
                volatility = 0.02
                trend = 0.0001  # 轻微上涨趋势
            elif stock_code == '600938':  # 中国海油
                volatility = 0.03
                trend = 0.0002
            else:
                volatility = 0.025
                trend = 0.00015
            
            # 生成价格序列（随机游走）
            np.random.seed(int(stock_code) % 10000)
            n_days = len(date_range)
            
            # 从历史到现在的序列
            returns = np.random.normal(trend, volatility, n_days)
            price_series = base_price * np.exp(-np.cumsum(returns[::-1]))[::-1]  # 反转使最新价格为base_price
            
            data_list = []
            for i, date in enumerate(date_range):
                close_price = price_series[i]
                open_price = close_price * (1 + np.random.normal(0, 0.01))
                high_price = max(open_price, close_price) * (1 + abs(np.random.normal(0, 0.005)))
                low_price = min(open_price, close_price) * (1 - abs(np.random.normal(0, 0.005)))
                
                # 如果是最后一天（今天），使用真实数据
                if i == n_days - 1:
                    open_price = current_data.get('open', open_price)
                    close_price = current_data.get('close', close_price)
                    high_price = current_data.get('high', high_price)
                    low_price = current_data.get('low', low_price)
                
                volume = np.random.randint(1000000, 10000000)
                amount = volume * close_price
                
                data_list.append({
                    'date': date,
                    'open': round(open_price, 2),
                    'close': round(close_price, 2),
                    'high': round(high_price, 2),
                    'low': round(low_price, 2),
                    'volume': volume,
                    'amount': round(amount, 2),
                    'amplitude': round((high_price - low_price) / open_price * 100, 2),
                    'change_pct': round((close_price - open_price) / open_price * 100, 2),
                    'change': round(close_price - open_price, 2),
                    'turnover': round(np.random.uniform(0.5, 5.0), 2)
                })
            
            df = pd.DataFrame(data_list)
            df = df.sort_values('date')
            
            logger.warning(f"注意：股票 {stock_code} 的历史数据基于当前价格 {base_price:.2f} 生成，非完全真实数据")
            logger.warning(f"建议：请确保使用真实历史数据进行投资决策")
            
            return df
            
        except Exception as e:
            logger.error(f"生成历史数据失败: {e}")
            return pd.DataFrame()
    
    def _generate_data_from_price(self, stock_code, current_price, days):
        """
        从当前价格生成数据
        """
        current_data = {
            'open': current_price * 0.99,
            'close': current_price,
            'high': current_price * 1.02,
            'low': current_price * 0.98,
            'volume': 5000000,
            'amount': current_price * 5000000,
            'amplitude': 4.0,
            'change_pct': 1.0,
            'change': current_price * 0.01
        }
        
        return self._generate_historical_data(stock_code, current_data, days)
    

    
    def fetch_dividend_data(self, stock_code):
        """
        获取股票分红数据（从公开财报或利润分配公告扒取）
        
        Args:
            stock_code: 股票代码
            
        Returns:
            dict: 包含分红数据的字典，键包括：
                - dividend_per_share: 最近一年每股分红（元）
                - dividend_yield: 当前股息率（%）
                - last_dividend_date: 最近分红日期
                - dividend_history: 历史分红列表
        """
        try:
            stock_code = str(stock_code)
            logger.info(f"尝试获取股票 {stock_code} 的分红数据")
            
            # 尝试多个数据源
            data_sources = [
                self._fetch_dividend_from_sina,
                self._fetch_dividend_from_eastmoney,
            ]
            
            for source_func in data_sources:
                try:
                    logger.info(f"尝试从 {source_func.__name__} 获取股票 {stock_code} 分红数据")
                    dividend_data = source_func(stock_code)
                    if dividend_data and dividend_data.get('dividend_per_share'):
                        logger.info(f"从 {source_func.__name__} 成功获取股票 {stock_code} 分红数据")
                        return dividend_data
                except Exception as e:
                    logger.warning(f"从 {source_func.__name__} 获取股票 {stock_code} 分红数据失败: {e}")
                    continue
            
            logger.warning(f"所有数据源都失败，无法获取股票 {stock_code} 分红数据")
            return None
            
        except Exception as e:
            logger.error(f"获取股票 {stock_code} 分红数据时发生错误: {e}")
            return None
    
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
            market = "sh" if stock_code.startswith(('6', '5')) else "sz"
            url = f"http://vip.stock.finance.sina.com.cn/corp/go.php/vISSUE_ShareBonus/stockid/{stock_code}.phtml"
            
            headers = {
                'User-Agent': self.user_agent,
                'Referer': f'http://finance.sina.com.cn/realstock/company/{market}{stock_code}/nc.shtml'
            }
            
            response = requests.get(url, headers=headers, timeout=self.timeout)
            response.encoding = 'gb2312'  # 新浪页面使用gb2312编码
            
            if response.status_code != 200:
                logger.warning(f"新浪财经分红页面请求失败: {response.status_code}")
                return None
            
            # 解析HTML，查找分红表格
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 查找分红表格（通常有class='datatbl'）
            tables = soup.find_all('table', class_='datatbl')
            
            dividend_history = []
            latest_dividend = None
            latest_date = None
            
            for table in tables:
                # 查找表格行
                rows = table.find_all('tr')
                for row in rows[1:]:  # 跳过表头
                    cols = row.find_all('td')
                    if len(cols) >= 6:
                        try:
                            # 解析分红信息
                            # 格式可能因页面而异，这里需要根据实际页面调整
                            dividend_date = cols[0].text.strip()  # 分红年度
                            dividend_scheme = cols[1].text.strip()  # 分红方案
                            
                            # 解析分红方案，例如"10派2.5元"表示每10股派2.5元
                            import re
                            match = re.search(r'10派([\d\.]+)元', dividend_scheme)
                            if match:
                                dividend_per_10 = float(match.group(1))  # 每10股分红
                                dividend_per_share = dividend_per_10 / 10.0  # 每股分红
                                
                                dividend_info = {
                                    'date': dividend_date,
                                    'scheme': dividend_scheme,
                                    'dividend_per_share': dividend_per_share,
                                    'dividend_per_10': dividend_per_10
                                }
                                
                                dividend_history.append(dividend_info)
                                
                                # 更新最新分红
                                if not latest_dividend or dividend_date > latest_date:
                                    latest_dividend = dividend_per_share
                                    latest_date = dividend_date
                        except Exception as e:
                            logger.debug(f"解析分红行失败: {e}")
                            continue
            
            if latest_dividend:
                return {
                    'dividend_per_share': latest_dividend,
                    'last_dividend_date': latest_date,
                    'dividend_history': dividend_history
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
            market = "0" if stock_code.startswith(('0', '3')) else "1"
            url = f"http://f10.eastmoney.com/BonusFinancingAjax/CompanyBonusDetail"
            params = {
                'code': f'{market}.{stock_code}',
                'type': '1'  # 分红类型
            }
            
            headers = {
                'User-Agent': self.user_agent,
                'Referer': f'http://f10.eastmoney.com/f10_v2/CashDividend.aspx?code={market}.{stock_code}'
            }
            
            response = requests.get(url, params=params, headers=headers, timeout=self.timeout)
            
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
                logger.info(f"东方财富分红API返回数据，但解析逻辑需要根据实际API格式实现")
                return None
                
            except Exception as e:
                logger.warning(f"解析东方财富分红API响应失败: {e}")
                return None
                
        except Exception as e:
            logger.warning(f"从东方财富获取分红数据失败: {e}")
            return None