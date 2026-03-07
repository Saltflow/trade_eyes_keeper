"""
缓存管理模块
为股票数据和LLM分析提供按天缓存功能
"""

import os
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

class CacheManager:
    """缓存管理器"""
    
    def __init__(self, config):
        """
        初始化缓存管理器
        
        Args:
            config: 配置字典
        """
        self.config = config
        storage_config = config.get('storage', {})
        
        # 缓存目录配置
        cache_dir = storage_config.get('cache_dir', './cache')
        self.cache_dir = Path(cache_dir)
        self.data_cache_dir = self.cache_dir / 'data'
        self.analysis_cache_dir = self.cache_dir / 'analysis'
        self.cache_days = storage_config.get('cache_days', 7)  # 默认保留7天缓存
        
        # 创建缓存目录
        self._create_cache_dirs()
        
        # 清理过期缓存
        self._clean_old_cache()
    
    def _create_cache_dirs(self):
        """创建缓存目录"""
        for dir_path in [self.cache_dir, self.data_cache_dir, self.analysis_cache_dir]:
            dir_path.mkdir(parents=True, exist_ok=True)
            logger.debug(f"创建缓存目录: {dir_path}")
    
    def _clean_old_cache(self):
        """清理过期缓存（超过cache_days天）"""
        try:
            cutoff_date = datetime.now() - timedelta(days=self.cache_days)
            
            # 清理数据缓存
            for cache_type, cache_dir in [('数据', self.data_cache_dir), ('分析', self.analysis_cache_dir)]:
                if cache_dir.exists():
                    for file_path in cache_dir.glob('*.json'):
                        try:
                            # 从文件名解析日期
                            filename = file_path.stem
                            parts = filename.rsplit('_', 1)
                            if len(parts) == 2:
                                date_str = parts[-1]
                                file_date = datetime.strptime(date_str, '%Y%m%d')
                                
                                if file_date < cutoff_date:
                                    file_path.unlink()
                                    logger.debug(f"删除过期{cache_type}缓存: {file_path.name}")
                        except Exception as e:
                            logger.warning(f"处理缓存文件 {file_path} 时出错: {e}")
                            # 不要删除文件，仅记录警告
                            
            logger.info(f"缓存清理完成，保留最近{self.cache_days}天缓存")
            
        except Exception as e:
            logger.error(f"清理缓存时出错: {e}")
    
    def _get_today_str(self):
        """获取今天日期字符串 (YYYYMMDD)"""
        return datetime.now().strftime('%Y%m%d')
    
    def get_stock_data_cache(self, stock_code):
        """
        获取股票数据缓存
        
        Args:
            stock_code: 股票代码
            
        Returns:
            dict: 缓存数据，如果不存在或过期返回None
        """
        try:
            cache_file = self.data_cache_dir / f"{stock_code}_{self._get_today_str()}.json"
            
            if cache_file.exists():
                with open(cache_file, 'r', encoding='utf-8') as f:
                    cached_data = json.load(f)
                
                # 检查缓存是否有效（包含必要字段）
                if 'stock_code' in cached_data and 'date' in cached_data:
                    logger.debug(f"从缓存读取股票 {stock_code} 数据")
                    return cached_data
                else:
                    logger.warning(f"股票 {stock_code} 缓存数据格式无效")
                    
            return None
            
        except Exception as e:
            logger.error(f"读取股票 {stock_code} 缓存失败: {e}")
            return None
    
    def set_stock_data_cache(self, stock_code, stock_data):
        """
        设置股票数据缓存
        
        Args:
            stock_code: 股票代码
            stock_data: 股票数据字典
        """
        try:
            cache_file = self.data_cache_dir / f"{stock_code}_{self._get_today_str()}.json"
            
            # 确保数据包含必要信息
            cache_data = {
                'stock_code': stock_code,
                'date': self._get_today_str(),
                'cached_at': datetime.now().isoformat(),
                'data': stock_data
            }
            
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)
                
            logger.debug(f"股票 {stock_code} 数据已缓存: {cache_file}")
            
        except Exception as e:
            logger.error(f"缓存股票 {stock_code} 数据失败: {e}")
    
    def get_analysis_cache(self, stock_code):
        """
        获取LLM分析缓存
        
        Args:
            stock_code: 股票代码
            
        Returns:
            dict: 缓存的分析结果，如果不存在或过期返回None
        """
        try:
            cache_file = self.analysis_cache_dir / f"{stock_code}_{self._get_today_str()}.json"
            
            if cache_file.exists():
                with open(cache_file, 'r', encoding='utf-8') as f:
                    cached_analysis = json.load(f)
                
                # 检查缓存是否有效
                if 'stock_code' in cached_analysis and 'analysis' in cached_analysis:
                    logger.debug(f"从缓存读取股票 {stock_code} 分析结果")
                    return cached_analysis
                else:
                    logger.warning(f"股票 {stock_code} 分析缓存格式无效")
                    
            return None
            
        except Exception as e:
            logger.error(f"读取股票 {stock_code} 分析缓存失败: {e}")
            return None
    
    def set_analysis_cache(self, stock_code, analysis_result):
        """
        设置LLM分析缓存
        
        Args:
            stock_code: 股票代码
            analysis_result: 分析结果字典
        """
        try:
            cache_file = self.analysis_cache_dir / f"{stock_code}_{self._get_today_str()}.json"
            
            # 确保分析结果包含必要信息
            cache_data = {
                'stock_code': stock_code,
                'date': self._get_today_str(),
                'cached_at': datetime.now().isoformat(),
                'analysis': analysis_result
            }
            
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)
                
            logger.debug(f"股票 {stock_code} 分析结果已缓存: {cache_file}")
            
        except Exception as e:
            logger.error(f"缓存股票 {stock_code} 分析结果失败: {e}")
    
    def clear_cache(self, days_old=None):
        """
        清理缓存
        
        Args:
            days_old: 清理多少天前的缓存，如果为None则使用配置的cache_days
        """
        try:
            if days_old is None:
                days_old = self.cache_days
                
            cutoff_date = datetime.now() - timedelta(days=days_old)
            deleted_count = 0
            
            # 清理所有缓存目录
            for cache_dir in [self.data_cache_dir, self.analysis_cache_dir]:
                if cache_dir.exists():
                    for file_path in cache_dir.glob('*.json'):
                        try:
                            # 从文件名解析日期
                            filename = file_path.stem
                            parts = filename.rsplit('_', 1)
                            if len(parts) == 2:
                                date_str = parts[-1]
                                file_date = datetime.strptime(date_str, '%Y%m%d')
                                
                                if file_date < cutoff_date:
                                    file_path.unlink()
                                    deleted_count += 1
                        except Exception as e:
                            # 如果无法解析日期，记录警告并跳过（可能是格式错误的文件）
                            logger.warning(f"无法解析缓存文件日期: {file_path.name}, 错误: {e}")
                            # 不删除文件，仅记录警告
            
            logger.info(f"缓存清理完成，删除了 {deleted_count} 个过期缓存文件")
            return deleted_count
            
        except Exception as e:
            logger.error(f"清理缓存时出错: {e}")
            return 0
    
    def get_cache_stats(self):
        """
        获取缓存统计信息
        
        Returns:
            dict: 缓存统计信息
        """
        try:
            stats = {
                'total_files': 0,
                'data_cache_files': 0,
                'analysis_cache_files': 0,
                'oldest_cache_date': None,
                'newest_cache_date': None
            }
            
            all_dates = []
            
            # 统计数据缓存
            if self.data_cache_dir.exists():
                data_files = list(self.data_cache_dir.glob('*.json'))
                stats['data_cache_files'] = len(data_files)
                stats['total_files'] += len(data_files)
                
                for file_path in data_files:
                    try:
                        filename = file_path.stem
                        parts = filename.rsplit('_', 1)
                        if len(parts) == 2:
                            date_str = parts[-1]
                            all_dates.append(datetime.strptime(date_str, '%Y%m%d'))
                    except Exception:
                        pass
            
            # 统计分析缓存
            if self.analysis_cache_dir.exists():
                analysis_files = list(self.analysis_cache_dir.glob('*.json'))
                stats['analysis_cache_files'] = len(analysis_files)
                stats['total_files'] += len(analysis_files)
                
                for file_path in analysis_files:
                    try:
                        filename = file_path.stem
                        parts = filename.rsplit('_', 1)
                        if len(parts) == 2:
                            date_str = parts[-1]
                            all_dates.append(datetime.strptime(date_str, '%Y%m%d'))
                    except Exception:
                        pass
            
            # 计算最早和最晚缓存日期
            if all_dates:
                stats['oldest_cache_date'] = min(all_dates).strftime('%Y-%m-%d')
                stats['newest_cache_date'] = max(all_dates).strftime('%Y-%m-%d')
            
            return stats
            
        except Exception as e:
            logger.error(f"获取缓存统计信息失败: {e}")
            return {'error': str(e)}