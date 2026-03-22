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
        self.announcement_content_cache_dir = self.cache_dir / "announcement_content"
        self.announcement_extraction_cache_dir = (
            self.cache_dir / "announcement_extraction"
        )
        self.pdf_files_cache_dir = self.cache_dir / "pdf_files"
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
            self.announcement_content_cache_dir,
            self.announcement_extraction_cache_dir,
            self.pdf_files_cache_dir,
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

    def get_analysis_cache(self, stock_code):
        """
        获取LLM分析缓存

        Args:
            stock_code: 股票代码

        Returns:
            dict: 缓存的分析结果，如果不存在或过期返回None
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

            with open(cache_file, "w", encoding="utf-8") as f:
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
