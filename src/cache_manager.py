"""
缓存管理模块
为股票数据和LLM分析提供按天缓存功能
"""

import json
import hashlib
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
        storage_config = config.get("storage", {})

        # 缓存目录配置
        cache_dir = storage_config.get("cache_dir", "./cache")
        self.cache_dir = Path(cache_dir)
        self.data_cache_dir = self.cache_dir / "data"
        self.analysis_cache_dir = self.cache_dir / "analysis"
        self.financial_analysis_cache_dir = self.cache_dir / "analysis_financial"
        self.announcement_content_cache_dir = self.cache_dir / "announcement_content"
        self.announcement_extraction_cache_dir = (
            self.cache_dir / "announcement_extraction"
        )
        self.pdf_files_cache_dir = self.cache_dir / "pdf_files"
        # 历史数据缓存目录
        self.historical_cache_dir = self.cache_dir / "historical"
        self.historical_data_dir = self.historical_cache_dir / "data"
        self.historical_metadata_dir = self.historical_cache_dir / "metadata"
        self.cache_days = storage_config.get("cache_days", 7)  # 默认保留7天缓存

        # 创建缓存目录
        self._create_cache_dirs()

        # 清理过期缓存
        self._clean_old_cache()

    def _create_cache_dirs(self):
        """创建缓存目录"""
        for dir_path in [
            self.cache_dir,
            self.data_cache_dir,
            self.analysis_cache_dir,
            self.financial_analysis_cache_dir,
            self.announcement_content_cache_dir,
            self.announcement_extraction_cache_dir,
            self.pdf_files_cache_dir,
            self.historical_cache_dir,
            self.historical_data_dir,
            self.historical_metadata_dir,
        ]:
            dir_path.mkdir(parents=True, exist_ok=True)
            logger.debug(f"创建缓存目录: {dir_path}")

    def _clean_old_cache(self):
        """清理过期缓存（超过cache_days天）"""
        try:
            cutoff_date = datetime.now() - timedelta(days=self.cache_days)

            # 清理数据缓存
            for cache_type, cache_dir in [
                ("数据", self.data_cache_dir),
                ("分析", self.analysis_cache_dir),
                ("财报分析", self.financial_analysis_cache_dir),
                ("历史数据", self.historical_data_dir),
                ("历史元数据", self.historical_metadata_dir),
            ]:
                if cache_dir.exists():
                    for file_path in cache_dir.glob("*.json"):
                        try:
                            # 从文件名解析日期
                            filename = file_path.stem
                            parts = filename.rsplit("_", 1)
                            if len(parts) == 2:
                                date_str = parts[-1]
                                file_date = datetime.strptime(date_str, "%Y%m%d")

                                if file_date < cutoff_date:
                                    file_path.unlink()
                                    logger.debug(
                                        f"删除过期{cache_type}缓存: {file_path.name}"
                                    )
                        except Exception as e:
                            logger.warning(f"处理缓存文件 {file_path} 时出错: {e}")
                            # 不要删除文件，仅记录警告

            logger.info(f"缓存清理完成，保留最近{self.cache_days}天缓存")

        except Exception as e:
            logger.error(f"清理缓存时出错: {e}")

    def _get_today_str(self):
        """获取今天日期字符串 (YYYYMMDD)"""
        return datetime.now().strftime("%Y%m%d")

    def _get_historical_cache_path(self, stock_code, start_date, end_date):
        """
        获取历史数据缓存文件路径

        Args:
            stock_code: 股票代码
            start_date: 开始日期 (YYYYMMDD格式)
            end_date: 结束日期 (YYYYMMDD格式)

        Returns:
            Path: 缓存文件路径
        """
        filename = f"{stock_code}_{start_date}_{end_date}.jsonl"
        return self.historical_data_dir / filename

    def _get_historical_metadata_path(self, stock_code):
        """
        获取历史数据元数据文件路径

        Args:
            stock_code: 股票代码

        Returns:
            Path: 元数据文件路径
        """
        filename = f"{stock_code}_metadata.json"
        return self.historical_metadata_dir / filename

    def get_historical_cache(self, stock_code, start_date, end_date):
        """
        获取历史数据缓存

        Args:
            stock_code: 股票代码
            start_date: 开始日期 (YYYYMMDD格式)
            end_date: 结束日期 (YYYYMMDD格式)

        Returns:
            tuple: (数据DataFrame, 元数据dict)，失败返回(None, None)
        """
        import random  # 随机化导入

        cache_path = self._get_historical_cache_path(stock_code, start_date, end_date)
        metadata_path = self._get_historical_metadata_path(stock_code)

        if not cache_path.exists() or not metadata_path.exists():
            return None, None

        try:
            # 随机化：5%概率模拟读取失败
            if random.random() < 0.05:
                logger.debug(f"随机模拟历史缓存读取失败: {stock_code}")
                return None, None

            # 读取元数据
            with open(metadata_path, "r", encoding="utf-8") as f:
                metadata = json.load(f)

            # 验证数据哈希
            expected_hash = metadata.get("data_hash")
            if expected_hash:
                # 读取数据文件计算哈希
                import hashlib

                with open(cache_path, "r", encoding="utf-8") as f:
                    content = f.read()
                actual_hash = hashlib.md5(content.encode("utf-8")).hexdigest()
                if actual_hash != expected_hash:
                    logger.warning(f"历史缓存数据哈希不匹配: {stock_code}")
                    return None, None

            # 读取JSON Lines数据
            import pandas as pd

            data_lines = []
            with open(cache_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        data_lines.append(json.loads(line))

            if not data_lines:
                return None, None

            df = pd.DataFrame(data_lines)

            # 验证点1：数据完整性检查
            if df.empty:
                logger.warning(f"历史缓存数据为空: {stock_code}")
                return None, None

            # 验证点2：日期字段检查
            if "date" not in df.columns:
                logger.warning(f"历史缓存缺少date字段: {stock_code}")
                return None, None

            # 验证点3：记录数检查
            expected_count = metadata.get("total_records")
            if expected_count and len(df) != expected_count:
                logger.warning(f"历史缓存记录数不匹配: {stock_code}")
                return None, None

            logger.info(f"从缓存读取历史数据: {stock_code} ({len(df)}条记录)")
            return df, metadata

        except Exception as e:
            logger.error(f"读取历史缓存失败 {stock_code}: {e}")
            return None, None

    def set_historical_cache(
        self, stock_code, data_df, start_date, end_date, metadata=None
    ):
        """
        设置历史数据缓存

        Args:
            stock_code: 股票代码
            data_df: 数据DataFrame
            start_date: 开始日期 (YYYYMMDD格式)
            end_date: 结束日期 (YYYYMMDD格式)
            metadata: 元数据字典 (可选)
        """
        import random  # 随机化导入

        if data_df is None or data_df.empty:
            logger.warning(f"无法缓存空数据: {stock_code}")
            return False

        cache_path = self._get_historical_cache_path(stock_code, start_date, end_date)
        metadata_path = self._get_historical_metadata_path(stock_code)

        try:
            # 随机化：2%概率模拟写入失败
            if random.random() < 0.02:
                logger.debug(f"随机模拟历史缓存写入失败: {stock_code}")
                return False

            # 准备数据行
            data_lines = []
            for _, row in data_df.iterrows():
                # 转换为可JSON序列化的字典
                record = {}
                for col in data_df.columns:
                    val = row[col]
                    # 处理特殊类型
                    if hasattr(val, "isoformat"):  # datetime等
                        val = val.isoformat()
                    record[col] = val
                data_lines.append(json.dumps(record, ensure_ascii=False))

            # 写入JSON Lines文件
            with open(cache_path, "w", encoding="utf-8") as f:
                f.write("\n".join(data_lines))

            # 计算数据哈希
            import hashlib

            content = "\n".join(data_lines)
            data_hash = hashlib.md5(content.encode("utf-8")).hexdigest()

            # 准备元数据
            if metadata is None:
                metadata = {}

            metadata.update(
                {
                    "stock_code": stock_code,
                    "last_updated": datetime.now().isoformat(),
                    "data_start_date": start_date,
                    "data_end_date": end_date,
                    "data_hash": data_hash,
                    "total_records": len(data_df),
                    "file_size_kb": cache_path.stat().st_size / 1024
                    if cache_path.exists()
                    else 0,
                }
            )

            # 写入元数据
            with open(metadata_path, "w", encoding="utf-8") as f:
                json.dump(metadata, f, ensure_ascii=False, indent=2)

            logger.info(f"历史数据缓存成功: {stock_code} ({len(data_df)}条记录)")
            return True

        except Exception as e:
            logger.error(f"写入历史缓存失败 {stock_code}: {e}")
            return False

    def get_stock_data_cache(self, stock_code):
        """
        获取股票数据缓存

        Args:
            stock_code: 股票代码

        Returns:
            dict: 缓存数据，如果不存在或过期返回None
        """
        try:
            cache_file = (
                self.data_cache_dir / f"{stock_code}_{self._get_today_str()}.json"
            )

            if cache_file.exists():
                with open(cache_file, "r", encoding="utf-8") as f:
                    cached_data = json.load(f)

                # 检查缓存是否有效（包含必要字段）
                if "stock_code" in cached_data and "date" in cached_data:
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
            cache_file = (
                self.data_cache_dir / f"{stock_code}_{self._get_today_str()}.json"
            )

            # 确保数据包含必要信息
            cache_data = {
                "stock_code": stock_code,
                "date": self._get_today_str(),
                "cached_at": datetime.now().isoformat(),
                "data": stock_data,
            }

            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)

            logger.debug(f"股票 {stock_code} 数据已缓存: {cache_file}")

        except Exception as e:
            logger.error(f"缓存股票 {stock_code} 数据失败: {e}")

    def get_analysis_cache(self, stock_code, data_hash=None):
        """
        获取LLM分析缓存，可选的验证数据哈希

        Args:
            stock_code: 股票代码
            data_hash: 可选的数据哈希，用于验证缓存数据新鲜度

        Returns:
            dict: 缓存的分析结果，如果不存在、过期或数据哈希不匹配返回None
        """
        try:
            cache_file = (
                self.analysis_cache_dir / f"{stock_code}_{self._get_today_str()}.json"
            )

            if cache_file.exists():
                with open(cache_file, "r", encoding="utf-8") as f:
                    cached_analysis = json.load(f)

                # 检查缓存是否有效
                if "stock_code" in cached_analysis and "analysis" in cached_analysis:
                    # 如果提供了数据哈希，验证缓存的数据哈希
                    if data_hash is not None:
                        cached_hash = cached_analysis.get("data_hash")
                        if cached_hash != data_hash:
                            logger.info(
                                f"股票 {stock_code} 缓存数据哈希不匹配: "
                                f"缓存哈希={cached_hash}, 当前哈希={data_hash}"
                            )
                            return None

                    logger.debug(f"从缓存读取股票 {stock_code} 分析结果")
                    return cached_analysis
                else:
                    logger.warning(f"股票 {stock_code} 分析缓存格式无效")

            return None

        except Exception as e:
            logger.error(f"读取股票 {stock_code} 分析缓存失败: {e}")
            return None

    def set_analysis_cache(self, stock_code, analysis_result, data_hash=None):
        """
        设置LLM分析缓存，可选的存储数据哈希

        Args:
            stock_code: 股票代码
            analysis_result: 分析结果字典
            data_hash: 可选的数据哈希，用于验证缓存数据新鲜度
        """
        try:
            cache_file = (
                self.analysis_cache_dir / f"{stock_code}_{self._get_today_str()}.json"
            )

            # 确保分析结果包含必要信息
            cache_data = {
                "stock_code": stock_code,
                "date": self._get_today_str(),
                "cached_at": datetime.now().isoformat(),
                "analysis": analysis_result,
            }

            # 如果提供了数据哈希，存储它
            if data_hash is not None:
                cache_data["data_hash"] = data_hash

            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)

            logger.debug(f"股票 {stock_code} 分析结果已缓存: {cache_file}")

        except Exception as e:
            logger.error(f"缓存股票 {stock_code} 分析结果失败: {e}")

    def get_financial_analysis_cache(self, stock_code, date_str=None):
        date_str = date_str or self._get_today_str()
        cache_file = self.financial_analysis_cache_dir / f"{stock_code}_{date_str}.json"
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                cached_data = json.load(f)
            reports = cached_data.get("reports") or cached_data.get("analysis")
            return cached_data if reports else None
        except FileNotFoundError:
            return None
        except Exception as e:
            logger.error(f"读取财报分析缓存失败: {e}")
            return None

    def set_financial_analysis_cache(
        self, stock_code, reports, date_str=None, content_hash=None
    ):

        try:
            date_str = date_str or self._get_today_str()
            cache_file = (
                self.financial_analysis_cache_dir / f"{stock_code}_{date_str}.json"
            )
            cache_data = {
                "stock_code": stock_code,
                "date": date_str,
                "cached_at": datetime.now().isoformat(),
                "reports": reports,
            }
            if content_hash:
                cache_data["content_hash"] = content_hash
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"缓存财报分析失败: {e}")

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
            for cache_dir in [
                self.data_cache_dir,
                self.analysis_cache_dir,
                self.financial_analysis_cache_dir,
            ]:
                if cache_dir.exists():
                    for file_path in cache_dir.glob("*.json"):
                        try:
                            # 从文件名解析日期
                            filename = file_path.stem
                            parts = filename.rsplit("_", 1)
                            if len(parts) == 2:
                                date_str = parts[-1]
                                file_date = datetime.strptime(date_str, "%Y%m%d")

                                if file_date < cutoff_date:
                                    file_path.unlink()
                                    deleted_count += 1
                        except Exception as e:
                            # 如果无法解析日期，记录警告并跳过（可能是格式错误的文件）
                            logger.warning(
                                f"无法解析缓存文件日期: {file_path.name}, 错误: {e}"
                            )
                            # 不删除文件，仅记录警告

            logger.info(f"缓存清理完成，删除了 {deleted_count} 个过期缓存文件")
            return deleted_count

        except Exception as e:
            logger.error(f"清理缓存时出错: {e}")
            return 0

    def get_announcement_content_cache(self, stock_code, url):
        """
        获取公告内容缓存

        Args:
            stock_code: 股票代码
            url: 公告URL

        Returns:
            dict: 缓存的内容数据，如果不存在返回None
        """
        try:
            cache_key = self._get_announcement_content_key(stock_code, url)
            cache_file = self.announcement_content_cache_dir / f"{cache_key}.json"

            if cache_file.exists():
                with open(cache_file, "r", encoding="utf-8") as f:
                    cached_data = json.load(f)

                # 检查缓存是否有效
                if "content" in cached_data and "stock_code" in cached_data:
                    logger.debug(f"从缓存读取公告内容: {stock_code}")
                    return cached_data
                else:
                    logger.warning(f"公告内容缓存格式无效: {cache_file}")

            return None

        except Exception as e:
            logger.error(f"读取公告内容缓存失败: {e}")
            return None

    def set_announcement_content_cache(self, stock_code, url, content, metadata=None):
        """
        设置公告内容缓存

        Args:
            stock_code: 股票代码
            url: 公告URL
            content: 公告内容文本
            metadata: 元数据字典（可选）
        """
        try:
            cache_key = self._get_announcement_content_key(stock_code, url)
            cache_file = self.announcement_content_cache_dir / f"{cache_key}.json"

            cache_data = {
                "stock_code": stock_code,
                "url": url,
                "content": content,
                "cached_at": datetime.now().isoformat(),
                "metadata": metadata or {},
            }

            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)

            logger.debug(f"公告内容已缓存: {cache_file}")

        except Exception as e:
            logger.error(f"缓存公告内容失败: {e}")

    def get_announcement_extraction_cache(self, stock_code, title, date, content_hash):
        """
        获取公告LLM提取结果缓存

        Args:
            stock_code: 股票代码
            title: 公告标题
            date: 公告日期
            content_hash: 内容哈希（用于验证内容未变更）

        Returns:
            dict: 提取结果，如果不存在返回None
        """
        try:
            cache_key = self._get_announcement_extraction_key(
                stock_code, title, date, content_hash
            )
            cache_file = self.announcement_extraction_cache_dir / f"{cache_key}.json"

            if cache_file.exists():
                with open(cache_file, "r", encoding="utf-8") as f:
                    cached_data = json.load(f)

                # 验证内容哈希是否匹配
                if cached_data.get("content_hash") == content_hash:
                    logger.debug(f"从缓存读取公告提取结果: {stock_code}")
                    return cached_data.get("extracted_data")
                else:
                    logger.debug(f"内容哈希不匹配，缓存无效: {cache_key}")

            return None

        except Exception as e:
            logger.error(f"读取公告提取缓存失败: {e}")
            return None

    def set_announcement_extraction_cache(
        self, stock_code, title, date, content_hash, extracted_data
    ):
        """
        设置公告LLM提取结果缓存

        Args:
            stock_code: 股票代码
            title: 公告标题
            date: 公告日期
            content_hash: 内容哈希
            extracted_data: 提取的数据字典
        """
        try:
            cache_key = self._get_announcement_extraction_key(
                stock_code, title, date, content_hash
            )
            cache_file = self.announcement_extraction_cache_dir / f"{cache_key}.json"

            cache_data = {
                "stock_code": stock_code,
                "title": title,
                "date": date,
                "content_hash": content_hash,
                "extracted_data": extracted_data,
                "cached_at": datetime.now().isoformat(),
            }

            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)

            logger.debug(f"公告提取结果已缓存: {cache_file}")

        except Exception as e:
            logger.error(f"缓存公告提取结果失败: {e}")

    def save_pdf_file(self, stock_code, url, pdf_bytes, metadata=None):
        """
        保存PDF文件到缓存

        Args:
            stock_code: 股票代码
            url: 公告URL
            pdf_bytes: PDF字节内容
            metadata: 元数据字典（可选）

        Returns:
            str: 保存的PDF文件路径，如果失败返回None
        """
        try:
            cache_key = self._get_announcement_content_key(stock_code, url)
            pdf_filename = f"{cache_key}.pdf"
            pdf_filepath = self.pdf_files_cache_dir / pdf_filename

            # 保存PDF文件
            with open(pdf_filepath, "wb") as f:
                f.write(pdf_bytes)

            # 保存元数据
            metadata_file = self.pdf_files_cache_dir / f"{cache_key}_metadata.json"
            metadata_data = {
                "stock_code": stock_code,
                "url": url,
                "pdf_filename": pdf_filename,
                "file_size": len(pdf_bytes),
                "saved_at": datetime.now().isoformat(),
                "metadata": metadata or {},
            }

            with open(metadata_file, "w", encoding="utf-8") as f:
                json.dump(metadata_data, f, ensure_ascii=False, indent=2)

            logger.debug(f"PDF文件已缓存: {pdf_filepath}, 大小: {len(pdf_bytes)}字节")
            return str(pdf_filepath)

        except Exception as e:
            logger.error(f"保存PDF文件失败: {e}")
            return None

    def get_pdf_file_path(self, stock_code, url):
        """
        获取缓存的PDF文件路径

        Args:
            stock_code: 股票代码
            url: 公告URL

        Returns:
            str: PDF文件路径，如果不存在返回None
        """
        try:
            cache_key = self._get_announcement_content_key(stock_code, url)
            pdf_filepath = self.pdf_files_cache_dir / f"{cache_key}.pdf"

            if pdf_filepath.exists():
                return str(pdf_filepath)
            else:
                return None

        except Exception as e:
            logger.error(f"获取PDF文件路径失败: {e}")
            return None

    def get_pdf_metadata(self, stock_code, url):
        """
        获取缓存的PDF元数据

        Args:
            stock_code: 股票代码
            url: 公告URL

        Returns:
            dict: 元数据，如果不存在返回None
        """
        try:
            cache_key = self._get_announcement_content_key(stock_code, url)
            metadata_file = self.pdf_files_cache_dir / f"{cache_key}_metadata.json"

            if metadata_file.exists():
                with open(metadata_file, "r", encoding="utf-8") as f:
                    metadata = json.load(f)
                return metadata
            else:
                return None

        except Exception as e:
            logger.error(f"获取PDF元数据失败: {e}")
            return None

    def _get_announcement_content_key(self, stock_code, url):
        """生成公告内容缓存键"""
        key_string = f"{stock_code}_{url}"
        return hashlib.md5(key_string.encode("utf-8")).hexdigest()

    def _get_announcement_extraction_key(self, stock_code, title, date, content_hash):
        """生成公告提取结果缓存键"""
        key_string = f"{stock_code}_{title}_{date}_{content_hash}"
        return hashlib.md5(key_string.encode("utf-8")).hexdigest()

    def get_latest_llm_extraction_for_stock(self, stock_code, days=365):
        """
        获取股票最新的LLM提取结果（分红数据）

        Args:
            stock_code: 股票代码
            days: 查找最近多少天的缓存（默认365天）

        Returns:
            dict: 最新的LLM提取结果，如果找不到则返回None
        """
        try:
            import re

            stock_code = str(stock_code)
            cutoff_date = datetime.now() - timedelta(days=days)
            latest_extraction = None
            latest_date = None

            # 扫描提取缓存目录
            if not self.announcement_extraction_cache_dir.exists():
                return None

            for cache_file in self.announcement_extraction_cache_dir.glob("*.json"):
                try:
                    with open(cache_file, "r", encoding="utf-8") as f:
                        cache_data = json.load(f)

                    # 检查股票代码匹配
                    if cache_data.get("stock_code") != stock_code:
                        continue

                    # 检查提取数据是否包含分红信息
                    extracted_data = cache_data.get("extracted_data")
                    if not extracted_data or not isinstance(extracted_data, dict):
                        continue

                    # 检查是否有分红金额
                    cash_dividend_per_share = extracted_data.get(
                        "cash_dividend_per_share"
                    )
                    if cash_dividend_per_share is None:
                        continue

                    # 检查日期，优先使用公告日期，其次使用缓存时间
                    date_str = cache_data.get("date")
                    if not date_str:
                        # 尝试使用缓存时间作为日期
                        cached_at_str = cache_data.get("cached_at")
                        if not cached_at_str:
                            continue
                        # 提取日期部分 (YYYY-MM-DD)，ISO格式如"2026-03-22T10:30:45"
                        date_match = re.search(
                            r"^(\d{4}-\d{2}-\d{2})[T\s]", cached_at_str
                        )
                        if not date_match:
                            # 备用模式：匹配任何地方的YYYY-MM-DD格式
                            date_match = re.search(
                                r"(\d{4}-\d{2}-\d{2})", cached_at_str
                            )
                            if not date_match:
                                continue
                        date_str = date_match.group(1)

                    # 解析日期
                    try:
                        # 尝试多种日期格式
                        for fmt in (
                            "%Y-%m-%d %H:%M:%S",
                            "%Y-%m-%d",
                            "%Y/%m/%d",
                            "%Y%m%d",
                        ):
                            try:
                                cache_date = datetime.strptime(date_str, fmt)
                                break
                            except ValueError:
                                continue
                        else:
                            # 无法解析日期，跳过
                            continue

                        # 检查是否在时间窗口内
                        if cache_date < cutoff_date:
                            continue

                        # 更新最新结果
                        if latest_date is None or cache_date > latest_date:
                            latest_date = cache_date
                            latest_extraction = extracted_data

                    except Exception as e:
                        logger.debug(f"解析缓存文件日期失败 {cache_file}: {e}")
                        continue

                except Exception as e:
                    logger.debug(f"读取缓存文件失败 {cache_file}: {e}")
                    continue

            if latest_extraction:
                date_str = latest_date.strftime("%Y-%m-%d") if latest_date else "未知"
                logger.info(
                    f"找到股票 {stock_code} 的最新LLM提取结果（日期: {date_str}）"
                )
                # 添加兼容性字段：dividend_per_share 映射 cash_dividend_per_share
                if (
                    "cash_dividend_per_share" in latest_extraction
                    and "dividend_per_share" not in latest_extraction
                ):
                    latest_extraction["dividend_per_share"] = latest_extraction[
                        "cash_dividend_per_share"
                    ]
                return latest_extraction
            else:
                logger.debug(f"未找到股票 {stock_code} 的LLM提取结果（最近{days}天内）")
                return None

        except Exception as e:
            logger.error(f"获取股票 {stock_code} 的最新LLM提取结果失败: {e}")
            return None
