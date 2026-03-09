"""
股票数据获取模块
获取A股真实交易数据
优先使用akshare，失败时使用网页爬虫
绝不使用模拟数据进行投资决策
"""

import logging
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path
import akshare as ak
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
        self.data_source = config.get('data_source', {}).get('type', 'akshare')
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
                
            # 解析缓存股票日期
            cached_date_str = cached_stock_data['date']
            cached_date = datetime.fromisoformat(cached_date_str.replace('Z', '+00:00')).date()
            today = datetime.now().date()
            
            # 检查日期是否为今天
            if cached_date == today:
                return False  # 缓存数据是今天的，可以使用
                
            # 缓存数据不是今天的，检查当前时间
            now = datetime.now()
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
                    required_fields = ['open', 'close', 'high', 'low', 'ma60', 'dividend_per_share', 'dividend_yield', 'earnings_growth']
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
                
                if self.data_source == 'akshare':
                    stock_data = self._fetch_from_akshare(stock_code, start_date, end_date)
                else:
                    logger.error(f"不支持的数源类型: {self.data_source}")
                    continue
                
                if stock_data is not None and not stock_data.empty:
                    # 计算MA60
                    stock_data['ma60'] = stock_data['close'].rolling(window=60).mean()
                    stock_data['stock_code'] = stock_code
                    
                    # 只保留最新一天的数据用于条件检查
                    latest_data = stock_data.iloc[-1:].copy()
                    
                    # 获取基本面数据（分红、股息率、业绩增长）
                    fundamental_data = self._fetch_fundamental_data(stock_code)
                    
                    # 将基本面数据添加到latest_data
                    latest_data['dividend_per_share'] = fundamental_data['dividend_per_share']
                    latest_data['earnings_growth'] = fundamental_data['earnings_growth']
                    
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
    
    def _fetch_from_akshare(self, stock_code, start_date, end_date):
        """
        从akshare获取股票数据
        
        Args:
            stock_code: 股票代码
            start_date: 开始日期 (YYYYMMDD)
            end_date: 结束日期 (YYYYMMDD)
            
        Returns:
            pandas.DataFrame: 股票数据
        """
        try:
            # 确保股票代码是字符串
            stock_code = str(stock_code)
            
            # 根据市场代码判断
            if stock_code.startswith('6') or stock_code.startswith('5'):
                symbol = f"sh{stock_code}"  # 上海证券交易所（包含A股和ETF）
            elif stock_code.startswith('0') or stock_code.startswith('3'):
                symbol = f"sz{stock_code}"  # 深圳证券交易所
            else:
                logger.warning(f"无法识别的股票代码格式: {stock_code}")
                return None
            
            # 获取日线数据
            stock_zh_a_hist_df = ak.stock_zh_a_hist(
                symbol=symbol,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust="qfq"  # 前复权
            )
            
            if stock_zh_a_hist_df.empty:
                logger.warning(f"未获取到股票 {stock_code} 的数据")
                raise ValueError(f"akshare返回空数据，股票 {stock_code} 可能未更新或代码错误")
            
            # 重命名列以统一格式
            stock_zh_a_hist_df = stock_zh_a_hist_df.rename(columns={
                '日期': 'date',
                '开盘': 'open',
                '收盘': 'close',
                '最高': 'high',
                '最低': 'low',
                '成交量': 'volume',
                '成交额': 'amount',
                '振幅': 'amplitude',
                '涨跌幅': 'change_pct',
                '涨跌额': 'change',
                '换手率': 'turnover'
            })
            
            # 确保日期为datetime类型
            stock_zh_a_hist_df['date'] = pd.to_datetime(stock_zh_a_hist_df['date'])
            
            # 按日期排序
            stock_zh_a_hist_df = stock_zh_a_hist_df.sort_values('date')
            
            # 重置索引
            stock_zh_a_hist_df = stock_zh_a_hist_df.reset_index(drop=True)
            
            logger.info(f"成功获取股票 {stock_code} 的 {len(stock_zh_a_hist_df)} 条数据")
            return stock_zh_a_hist_df
            
        except Exception as e:
            logger.error(f"从akshare获取股票 {stock_code} 数据失败: {e}")
            
            # 尝试使用网页爬虫作为备用方案
            logger.warning(f"akshare失败，尝试使用网页爬虫获取股票 {stock_code} 真实数据")
            try:
                return self._fetch_from_web_crawler(stock_code, start_date, end_date)
            except Exception as crawler_error:
                logger.error(f"网页爬虫也失败，无法获取股票 {stock_code} 数据: {crawler_error}")
                logger.critical(f"警告：股票 {stock_code} 数据获取完全失败，系统无法进行有效分析")
                logger.critical(f"投资决策必须基于真实数据，请检查网络连接或数据源配置")
                return None
    
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
        获取股票基本面数据：分红、股息率、业绩增长
        
        Args:
            stock_code: 股票代码
            
        Returns:
            dict: 包含基本面数据的字典
        """
        try:
            # 初始化结果字典
            fundamental_data = {
                'dividend_per_share': None,  # 过去1年每股分红（元）
                'dividend_yield': None,      # 当前价年化股息率（%）
                'earnings_growth': None      # 最近一次报告业绩增长参考值（%）
            }
            
            # 确保股票代码是字符串
            stock_code = str(stock_code)
            
            # 不再使用硬编码的分红近似值，避免提供不准确的数据
            # 当网页爬虫和akshare都无法提供可靠数据时，分红字段保持为None
            
            # 首先尝试使用web_crawler获取准确的分红数据（从财报或公告扒取）
            try:
                # 初始化web_crawler如果尚未初始化
                if self.web_crawler is None:
                    from .web_crawler import StockWebCrawler
                    self.web_crawler = StockWebCrawler(self.config)
                
                dividend_data = self.web_crawler.fetch_dividend_data(stock_code)
                if dividend_data and dividend_data.get('dividend_per_share'):
                    fundamental_data['dividend_per_share'] = dividend_data['dividend_per_share']
                    logger.info(f"股票 {stock_code} 从网页爬虫获取准确分红数据: {dividend_data['dividend_per_share']:.3f}元")
                else:
                    # web_crawler失败，尝试从akshare获取准确的分红数据
                    try:
                        import akshare as ak
                        
                        dividend_found = False
                        
                        # 首先尝试cninfo最新分红数据（最准确）
                        try:
                            cninfo_df = ak.stock_dividend_cninfo(symbol=stock_code)
                            if not cninfo_df.empty and len(cninfo_df) > 0:
                                logger.info(f"股票 {stock_code} 从cninfo获取到 {len(cninfo_df)} 条分红记录")
                                
                                # 寻找日期列并排序，确保取最新记录
                                date_col = None
                                for col in cninfo_df.columns:
                                    col_str = str(col)
                                    if "����" in col_str or "日期" in col_str or "ʵʩ" in col_str:
                                        date_col = col
                                        break
                                if date_col is not None:
                                    try:
                                        cninfo_df[date_col] = pd.to_datetime(cninfo_df[date_col], errors='coerce')
                                        cninfo_df = cninfo_df.sort_values(date_col, ascending=False)
                                        logger.debug(f"股票 {stock_code} cninfo数据按日期排序，最新日期: {cninfo_df.iloc[0][date_col]}")
                                    except Exception as e:
                                        logger.debug(f"股票 {stock_code} cninfo日期列处理失败: {e}")
                                
                                # 找到最新的一条分红记录
                                # 寻找包含分红金额的列
                                amount_col = None
                                desc_col = None
                    
                                # 检查已知的列名模式
                                for col in cninfo_df.columns:
                                    col_str = str(col)
                                    # 金额列通常包含"Ϣ��"或"金额"
                                    if "Ϣ��" in col_str or "金额" in col_str:
                                        amount_col = col
                                    # 描述列通常包含"ֺ˵"或"分红说明"
                                    if "ֺ˵" in col_str or "分红说明" in col_str or "˵��" in col_str:
                                        desc_col = col
                    
                                # 如果没找到，尝试其他方法识别
                                if amount_col is None or desc_col is None:
                                    # 检查第一行数据来识别列
                                    first_row = cninfo_df.iloc[0]
                                    for col in cninfo_df.columns:
                                        val = first_row[col]
                                        if pd.notna(val):
                                            val_str = str(val)
                                            # 如果值看起来像金额（数字且大于0小于100）
                                            if isinstance(val, (int, float)) and 0 < val < 100:
                                                amount_col = col
                                            # 如果值包含"��"字符（中文顿号，用于"10股"格式）
                                            elif isinstance(val, str) and "��" in val_str:
                                                desc_col = col
                    
                                logger.info(f"股票 {stock_code} cninfo列检测: amount_col={amount_col}, desc_col={desc_col}")
                    
                                if amount_col is not None and desc_col is not None:
                                    # 计算12个月前的时间点
                                    twelve_months_ago = datetime.now() - timedelta(days=365)
                                    
                                    # 初始化总分红
                                    total_dividend_per_share = 0.0
                                    dividend_count = 0
                                    recent_dividends_found = False
                                    
                                    # 遍历所有分红记录，累加过去12个月的分红
                                    for idx, row in cninfo_df.iterrows():
                                        # 检查日期是否在12个月内
                                        row_date = None
                                        if date_col is not None:
                                            row_date = row[date_col]
                                        else:
                                            # 尝试使用第一列作为日期
                                            try:
                                                row_date = pd.to_datetime(row.iloc[0], errors='coerce')
                                            except:
                                                pass
                                        
                                        if pd.isna(row_date):
                                            continue
                                        
                                        # 跳过12个月前的记录
                                        if row_date < twelve_months_ago:
                                            continue
                                        
                                        # 获取现金分红比例
                                        dividend_amount = row[amount_col]
                                        dividend_desc = row[desc_col]
                                        
                                        if pd.notna(dividend_amount) and dividend_amount > 0:
                                            # 从描述中解析每10股分红金额，例如"10��1.7Ԫ(��˰)"表示10股1.7元
                                            # 默认按10股计算
                                            shares_for_dividend = 10.0
                                            if isinstance(dividend_desc, str) and "��" in dividend_desc:
                                                try:
                                                    # 格式: "10��1.7Ԫ(��˰)"
                                                    parts = dividend_desc.split("��")
                                                    if len(parts) >= 2:
                                                        shares_part = parts[0].strip()
                                                        shares_for_dividend = float(shares_part)
                                                except:
                                                    pass
                                            
                                            # 计算每股分红
                                            dividend_per_share = dividend_amount / shares_for_dividend
                                            
                                            # 验证合理性（通常每股分红在0-5元之间）
                                            if 0 < dividend_per_share < 5:
                                                total_dividend_per_share += dividend_per_share
                                                dividend_count += 1
                                                recent_dividends_found = True
                                                
                                                # 记录单次分红详情
                                                logger.debug(f"股票 {stock_code} 分红记录: {row_date.strftime('%Y-%m-%d')}, 每股: {dividend_per_share:.3f}元, 累计: {total_dividend_per_share:.3f}元")
                                            else:
                                                logger.warning(f"股票 {stock_code} cninfo分红数据不合理: {dividend_per_share:.3f}元/股 (日期: {row_date})")
                                    
                                    # 如果找到近期分红，使用总和
                                    if recent_dividends_found:
                                        fundamental_data["dividend_per_share"] = round(total_dividend_per_share, 3)
                                        logger.info(f"股票 {stock_code} 从cninfo获取过去12个月分红总计: {total_dividend_per_share:.3f}元/股 ({dividend_count}次分红)")
                                        dividend_found = True
                                    else:
                                        logger.debug(f"股票 {stock_code} 过去12个月内无有效分红记录")
                        except Exception as cninfo_error:
                            logger.debug(f"股票 {stock_code} cninfo分红数据获取失败，尝试其他方法: {cninfo_error}")
                        
                        # 如果cninfo数据没有找到或无效，尝试年均股息数据
                        if not dividend_found:
                            try:
                                dividend_df = ak.stock_history_dividend()
                                stock_dividend = dividend_df[dividend_df["代码"] == stock_code]
                                
                                if not stock_dividend.empty:
                                    latest_dividend = stock_dividend.iloc[0]
                                    annual_dividend = latest_dividend.get("年均股息", 0)
                                
                                    if annual_dividend and annual_dividend > 0:
                                        original = annual_dividend
                                        unit_note = "原始值"
                                        
                                        # 单位修正：
                                        # 1. 如果数值过大（>100），可能以分为单位，转换为元
                                        if annual_dividend > 100:
                                            annual_dividend = annual_dividend / 100.0
                                            unit_note = "分转元"
                                            logger.info(f"股票 {stock_code} 分红数据单位修正({unit_note}): {original:.3f} → {annual_dividend:.3f}元")
                                        # 2. 如果数值在2-100之间，可能为每10股金额，转换为每股
                                        elif annual_dividend > 2:
                                            annual_dividend = annual_dividend / 10.0
                                            unit_note = "每10股转每股"
                                            logger.info(f"股票 {stock_code} 分红数据单位修正({unit_note}): {original:.3f} → {annual_dividend:.3f}元/股")
                                        
                                        if 0.01 <= annual_dividend <= 5.0:
                                            fundamental_data["dividend_per_share"] = round(annual_dividend, 3)
                                            logger.info(f"股票 {stock_code} 使用年均股息: {annual_dividend:.3f}元/股 (原始值: {original:.3f}, {unit_note})")
                                            dividend_found = True
                                        else:
                                            logger.warning(f"股票 {stock_code} 年均股息数据不合理: {annual_dividend:.3f}元/股 (原始值: {original:.3f}, {unit_note})")
                                    else:
                                        logger.warning(f"股票 {stock_code} 无有效年均股息数据")
                                else:
                                    logger.warning(f"未找到股票 {stock_code} 的年均股息数据")
                            except Exception as annual_error:
                                logger.debug(f"获取股票 {stock_code} 年均股息失败: {annual_error}")
                    except Exception:
                        pass
            except Exception as e:
                logger.error(f"使用web_crawler获取股票 {stock_code} 分红数据失败: {e}")
                # web_crawler失败，无可靠分红数据，字段保持为None
                logger.info(f"股票 {stock_code} web_crawler失败，无可靠分红数据，字段保持为None")
            
            # 尝试获取财务指标（用于业绩增长）
            try:
                # 使用akshare获取财务分析指标
                import akshare as ak
                
                # 尝试获取财务指标
                financial_df = ak.stock_financial_analysis_indicator(symbol=stock_code)
                
                if not financial_df.empty and len(financial_df) > 0:
                    # 获取最近一期的净利润增长率
                    growth_columns = [col for col in financial_df.columns if '增长' in col or '增长率' in col]
                    
                    if growth_columns:
                        latest_report = financial_df.iloc[-1]
                        for col in growth_columns:
                            value = latest_report.get(col)
                            if pd.notna(value) and value != 0:
                                # 验证增长率合理性（通常-100%到+500%之间）
                                if -100 <= float(value) <= 500:
                                    fundamental_data['earnings_growth'] = round(float(value), 2)
                                    logger.info(f"股票 {stock_code} 业绩增长指标 [{col}]: {value:.2f}%")
                                    break
                                else:
                                    logger.warning(f"股票 {stock_code} 业绩增长数据不合理: {value:.2f}%，超出合理范围")
                    
                    if fundamental_data['earnings_growth'] is None:
                        logger.warning(f"股票 {stock_code} 未找到有效的业绩增长数据")
                        
                        # 尝试其他数据源或方法
                        # 这里可以添加其他获取业绩增长数据的方法
                        
                else:
                    logger.warning(f"股票 {stock_code} 无财务指标数据")
                    
            except Exception as e:
                logger.error(f"获取股票 {stock_code} 财务指标失败: {e}")
            
            return fundamental_data
            
        except Exception as e:
            logger.error(f"获取股票 {stock_code} 基本面数据失败: {e}")
            return {
                'dividend_per_share': None,
                'dividend_yield': None,
                'earnings_growth': None
            }