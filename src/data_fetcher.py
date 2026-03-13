"""
股票数据获取模块
获取A股真实交易数据
 使用网页爬虫获取真实数据，已移除不可靠的akshare API
绝不使用模拟数据进行投资决策
"""

import logging
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, cast

from .cache_manager import CacheManager

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
        self.stocks = config.get('stocks', [])
        # 数据源类型，已移除akshare，只支持web_crawler
        data_source_type = config.get('data_source', {}).get('type', 'web_crawler')
        if data_source_type == 'akshare':
            logger.warning("akshare数据源已移除，将使用web_crawler作为替代")
            data_source_type = 'web_crawler'
        self.data_source = data_source_type
        # 初始化缓存管理器
        self.cache_manager = CacheManager(config)
        # 延迟导入web_crawler，避免循环依赖
        self.web_crawler = None
        
        # 缓存绕过配置
        scheduler_config = config.get('scheduler', {})
        cutoff_str = scheduler_config.get('cache_bypass_cutoff', '15:05')
        try:
            cutoff_hour, cutoff_minute = map(int, cutoff_str.split(':'))
            self.cache_bypass_cutoff_hour = cutoff_hour
            self.cache_bypass_cutoff_minute = cutoff_minute
        except:
            self.cache_bypass_cutoff_hour = 15
            self.cache_bypass_cutoff_minute = 5
        
        # 时区配置
        import pytz
        timezone_str = scheduler_config.get('timezone', 'Asia/Shanghai')
        self.timezone = pytz.timezone(timezone_str)
        
    def _should_bypass_cache(self, cached_data):
        """
        判断是否应绕过缓存
        规则：如果当前时间 >= 15:05 且缓存数据日期不是今天，则绕过缓存
        """
        try:
            # 获取缓存中的股票数据日期
            if not cached_data or 'data' not in cached_data:
                return True
                
            cached_stock_data = cached_data['data']
            if 'date' not in cached_stock_data:
                return True
                
            # 解析缓存股票日期（转换为本地时区日期）
            cached_date_str = cached_stock_data['date']
            cached_dt = datetime.fromisoformat(cached_date_str.replace('Z', '+00:00'))
            # 转换为配置的时区再比较日期
            cached_date_local = cached_dt.astimezone(self.timezone).date()
            
            # 获取当前时间（使用时区）
            now = datetime.now(self.timezone)
            today = now.date()
            
            # 检查日期是否为今天
            if cached_date_local == today:
                return False  # 缓存数据是今天的，可以使用
                
            # 缓存数据不是今天的，检查当前时间
            cutoff_time = now.replace(
                hour=self.cache_bypass_cutoff_hour, 
                minute=self.cache_bypass_cutoff_minute, 
                second=0, 
                microsecond=0
            )
            
            if now >= cutoff_time:
                # 当前时间 >= 配置的截止时间，需要今天的数据，但缓存数据不是今天的
                logger.info(f"缓存数据日期 {cached_date_str} 不是今天，当前时间 {now.strftime('%H:%M')} >= {self.cache_bypass_cutoff_hour:02d}:{self.cache_bypass_cutoff_minute:02d}，绕过缓存")
                return True
            else:
                # 当前时间 < 配置的截止时间，可以使用旧数据
                return False
                
        except Exception as e:
            logger.warning(f"检查缓存是否应绕过时出错: {e}")
            return True  # 出错时绕过缓存
            
    def fetch_stock_data(self):
        """
        获取股票数据
        
        Returns:
            pandas.DataFrame: 包含股票代码、日期、开盘、收盘、最高、最低、成交量、成交额、MA60的数据
        """
        all_data = []
        
        for stock_code in self.stocks:
            # 确保股票代码是字符串
            stock_code = str(stock_code)
            
            try:
                # 首先尝试从缓存获取数据
                cached_data = self.cache_manager.get_stock_data_cache(stock_code)
                if cached_data and 'data' in cached_data and not self._should_bypass_cache(cached_data):
                    cached_latest_data = cached_data['data']
                    # 验证缓存数据包含必要字段
                    required_fields = ['open', 'close', 'high', 'low', 'ma60', 'dividend_per_share', 'dividend_yield', 'earnings_growth', 'pe_ratio', 'pb_ratio', 'roe', 'debt_ratio']
                    has_all_fields = all(field in cached_latest_data for field in required_fields)
                    
                    if has_all_fields:
                        logger.info(f"股票 {stock_code} 使用缓存数据")
                        # 从缓存数据构建DataFrame
                        latest_data = pd.DataFrame([cached_latest_data])
                        all_data.append(latest_data)
                        continue
                    else:
                        logger.warning(f"股票 {stock_code} 缓存数据不完整，重新获取")
                elif cached_data and 'data' in cached_data:
                    # 缓存存在但需要绕过（例如数据不是今天的且时间>=15:05）
                    logger.info(f"股票 {stock_code} 缓存数据已过期，重新获取")
                # 缓存不存在或数据不完整或需要绕过，继续获取新数据
                
                logger.info(f"获取股票 {stock_code} 数据（无缓存）")
                
                # 获取历史数据（至少61天用于计算MA60）
                end_date = datetime.now().strftime('%Y%m%d')
                start_date = (datetime.now() - timedelta(days=120)).strftime('%Y%m%d')  # 获取120天数据
                
                # 使用网页爬虫获取股票数据（已移除akshare）
                if self.web_crawler is None:
                    from .web_crawler import StockWebCrawler
                    self.web_crawler = StockWebCrawler(self.config)
                
                stock_data = self.web_crawler.fetch_stock_data(stock_code, days=120)
                
                if stock_data is not None and not stock_data.empty:
                    # 计算MA60
                    stock_data['ma60'] = stock_data['close'].rolling(window=60).mean()
                    stock_data['stock_code'] = stock_code
                    
                    # 只保留最新一天的数据用于条件检查
                    latest_data = stock_data.iloc[-1:].copy()
                    
                    # 检查数据日期是否为今天
                    if not latest_data.empty:
                        latest_date = latest_data.iloc[0].get('date')
                        if latest_date:
                            today = datetime.now(self.timezone).date()
                            if latest_date.date() != today:
                                logger.warning(f"股票 {stock_code} 最新数据日期为 {latest_date.date()}，不是今天 {today}，数据可能已过期")
                    
                    # 获取基本面数据（分红、股息率、业绩增长）
                    fundamental_data = self._fetch_fundamental_data(stock_code)
                    
                    # 将基本面数据添加到latest_data
                    latest_data['dividend_per_share'] = fundamental_data['dividend_per_share']
                    latest_data['earnings_growth'] = fundamental_data['earnings_growth']
                    latest_data['pe_ratio'] = fundamental_data['pe_ratio']
                    latest_data['pb_ratio'] = fundamental_data['pb_ratio']
                    latest_data['roe'] = fundamental_data['roe']
                    latest_data['debt_ratio'] = fundamental_data['debt_ratio']
                    
                    # 计算股息率（需要收盘价）
                    if fundamental_data['dividend_per_share'] is not None and not latest_data.empty:
                        close_price = latest_data.iloc[0].get('close')
                        if close_price and close_price > 0:
                            dividend_yield = (fundamental_data['dividend_per_share'] / close_price) * 100
                            
                            # 验证股息率合理性（通常不超过20%）
                            # 同时检查分红是否不超过股价的50%（防止数据错误）
                            if dividend_yield > 20 or fundamental_data['dividend_per_share'] > close_price * 0.5:
                                logger.warning(f"股票 {stock_code} 股息率异常: 分红={fundamental_data['dividend_per_share']:.3f}元, 股价={close_price:.2f}元, 股息率={dividend_yield:.2f}% (可能数据错误)")
                                latest_data['dividend_per_share'] = None
                                latest_data['dividend_yield'] = None
                            else:
                                latest_data['dividend_yield'] = round(dividend_yield, 2)
                                logger.info(f"股票 {stock_code} 股息率计算: 分红={fundamental_data['dividend_per_share']:.3f}元, 股价={close_price:.2f}元, 股息率={dividend_yield:.2f}%")
                                # 检查股息率是否过低（<0.5%），可能数据不准确
                                if dividend_yield < 0.5:
                                    logger.warning(f"股票 {stock_code} 股息率过低: {dividend_yield:.2f}% (可能分红数据不准确或股价过高)")
                        else:
                            latest_data['dividend_yield'] = None
                    else:
                        latest_data['dividend_yield'] = None
                    
                    all_data.append(latest_data)
                    
                    # 缓存处理后的最新数据
                    try:
                        if not latest_data.empty:
                            latest_data_dict = latest_data.iloc[0].to_dict()
                            # 转换非JSON可序列化的类型
                            for key, value in latest_data_dict.items():
                                if pd.isna(value):
                                    latest_data_dict[key] = None
                                elif isinstance(value, pd.Timestamp):
                                    latest_data_dict[key] = value.isoformat()
                                elif isinstance(value, (np.integer, np.floating)):
                                    latest_data_dict[key] = float(value)
                            self.cache_manager.set_stock_data_cache(stock_code, latest_data_dict)
                            logger.debug(f"股票 {stock_code} 最新数据已缓存")
                    except Exception as cache_error:
                        logger.warning(f"缓存股票 {stock_code} 数据失败: {cache_error}")
                    
                    # 保存完整历史数据到CSV
                    self._save_to_csv(stock_code, stock_data)
                    
            except Exception as e:
                logger.error(f"获取股票 {stock_code} 数据失败: {e}")
        
        if all_data:
            return pd.concat(all_data, ignore_index=True)
        else:
            return pd.DataFrame()
    

    
    def _save_to_csv(self, stock_code, stock_data):
        """
        保存股票数据到CSV文件
        
        Args:
            stock_code: 股票代码
            stock_data: 股票数据DataFrame
        """
        try:
            data_dir = self.config.get('storage', {}).get('data_dir', './data')
            Path(data_dir).mkdir(parents=True, exist_ok=True)
            
            csv_file = Path(data_dir) / f"{stock_code}_history.csv"
            
            # 如果文件存在，读取现有数据并合并
            if csv_file.exists():
                existing_data = pd.read_csv(csv_file, parse_dates=['date'])
                
                # 合并数据，去除重复
                combined_data = pd.concat([existing_data, stock_data], ignore_index=True)
                combined_data = combined_data.drop_duplicates(subset=['date'], keep='last')
                combined_data = combined_data.sort_values('date')
                
                stock_data = combined_data
            
            # 保存到CSV
            stock_data.to_csv(csv_file, index=False, encoding='utf-8-sig')
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
            # 延迟导入，避免循环依赖
            if self.web_crawler is None:
                from .web_crawler import StockWebCrawler
                self.web_crawler = StockWebCrawler(self.config)
            
            logger.info(f"使用网页爬虫获取股票 {stock_code} 真实数据")
            
            # 计算需要的历史天数
            start_dt = datetime.strptime(start_date, '%Y%m%d')
            end_dt = datetime.strptime(end_date, '%Y%m%d')
            days = (end_dt - start_dt).days + 30  # 多取一些天数
            
            # 获取数据
            data = self.web_crawler.fetch_stock_data(stock_code, days)
            
            if data.empty:
                logger.error(f"网页爬虫未能获取到股票 {stock_code} 数据")
                return None
            
            # 过滤日期范围
            data = data[(data['date'] >= start_dt) & (data['date'] <= end_dt)]
            
            if data.empty:
                logger.warning(f"网页爬虫获取的数据不在请求的日期范围内")
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
            'dividend_per_share': None,  # 过去1年每股分红（元）
            'dividend_yield': None,      # 当前价年化股息率（%）
            'earnings_growth': None,     # 最近一次报告业绩增长参考值（%）
            'pe_ratio': None,            # 市盈率 (PE)
            'pb_ratio': None,            # 市净率 (PB)
            'roe': None,                 # 净资产收益率 (ROE)
            'debt_ratio': None           # 资产负债率
        }
        
        # 确保股票代码是字符串
        stock_code = str(stock_code)
        
        # 1. 获取分红数据
        dividend = self._fetch_dividend_from_web_crawler(stock_code)
        if dividend is not None:
            fundamental_data['dividend_per_share'] = dividend
        
        # 2. 获取业绩增长数据（暂时返回None，后续可通过web_crawler实现）
        # 保留为None，避免使用不可靠的API
        
        # 3. 获取估值指标
        valuation_data = self._fetch_valuation_from_web_crawler(stock_code)
        if valuation_data:
            fundamental_data['pe_ratio'] = valuation_data.get('pe_ratio')  # type: ignore
            fundamental_data['pb_ratio'] = valuation_data.get('pb_ratio')  # type: ignore
            fundamental_data['roe'] = valuation_data.get('roe')  # type: ignore
            fundamental_data['debt_ratio'] = valuation_data.get('debt_ratio')  # type: ignore
        
        logger.info(f"股票 {stock_code} 基本面数据获取完成: 分红={fundamental_data['dividend_per_share']}")
        
        return fundamental_data
    
    def _fetch_dividend_from_web_crawler(self, stock_code: str) -> Optional[float]:
        """从网页爬虫获取分红数据，返回每股分红金额"""
        try:
            if self.web_crawler is None:
                from .web_crawler import StockWebCrawler
                self.web_crawler = StockWebCrawler(self.config)
            
            dividend_data = self.web_crawler.fetch_dividend_data(stock_code)
            if dividend_data and dividend_data.get('dividend_per_share'):
                dividend = dividend_data['dividend_per_share']
                logger.info(f"股票 {stock_code} 从网页爬虫获取分红数据: {dividend:.3f}元")
                return dividend
        except Exception as e:
            logger.warning(f"获取股票 {stock_code} 分红数据失败: {e}")
        return None
    
    def _fetch_valuation_from_web_crawler(self, stock_code: str) -> Optional[Dict[str, Optional[float]]]:
        """从网页爬虫获取估值指标数据"""
        if self.web_crawler is None:
            from .web_crawler import StockWebCrawler
            self.web_crawler = StockWebCrawler(self.config)
        
        valuation_data = None
        try:
            valuation_data = self.web_crawler.fetch_valuation_data(stock_code)
        except Exception as e:
            logger.warning(f"获取股票 {stock_code} 估值指标失败: {e}")
            return None
        
        if valuation_data:
            # 记录获取到的估值数据
            pe_str = f"{valuation_data.get('pe_ratio'):.2f}" if valuation_data.get('pe_ratio') is not None else "None"
            pb_str = f"{valuation_data.get('pb_ratio'):.2f}" if valuation_data.get('pb_ratio') is not None else "None"
            roe_str = f"{valuation_data.get('roe'):.2f}%" if valuation_data.get('roe') is not None else "None"
            debt_str = f"{valuation_data.get('debt_ratio'):.2f}%" if valuation_data.get('debt_ratio') is not None else "None"
            logger.info(f"股票 {stock_code} 估值指标: PE={pe_str}, PB={pb_str}, ROE={roe_str}, 负债率={debt_str}")
            return cast(Dict[str, Optional[float]], valuation_data)  # type: ignore
        else:
            logger.warning(f"股票 {stock_code} 未获取到估值指标数据")
            return None
            
