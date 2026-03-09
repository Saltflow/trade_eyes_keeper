#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
公告内容抓取和文本提取模块
支持PDF和HTML格式，提供缓存和重试机制
"""

import logging
import requests
import hashlib
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
import io
import re

# 尝试导入PDF解析库
try:
    import pdfplumber
    PDFPLUMBER_AVAILABLE = True
except ImportError:
    PDFPLUMBER_AVAILABLE = False
    logger = logging.getLogger(__name__)
    logger.warning("pdfplumber未安装，PDF解析功能不可用")

# 尝试导入HTML解析库
try:
    from bs4 import BeautifulSoup
    BEAUTIFULSOUP_AVAILABLE = True
except ImportError:
    BEAUTIFULSOUP_AVAILABLE = False
    logger = logging.getLogger(__name__)
    logger.warning("beautifulsoup4未安装，HTML解析功能不可用")

logger = logging.getLogger(__name__)

class ContentFetcher:
    """公告内容抓取器"""
    
    def __init__(self, config):
        """
        初始化内容抓取器
        
        Args:
            config: 配置字典
        """
        self.config = config
        announcement_config = config.get('announcements', {})
        
        # 配置参数
        self.timeout = 30
        self.retry_times = 3
        self.retry_delay = 2
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        
        # 大小限制（字节）
        self.max_pdf_size = announcement_config.get('max_pdf_size_mb', 10) * 1024 * 1024
        self.max_html_size = 5 * 1024 * 1024  # 5MB
        
        # 内容提取配置
        self.extract_max_length = 50000  # 最大提取字符数
        self.extract_timeout = 60  # 提取超时时间（秒）
        
        # 缓存管理器（可选）
        self.cache_manager = None
        try:
            from .cache_manager import CacheManager
            self.cache_manager = CacheManager(config)
        except ImportError:
            logger.warning("CacheManager不可用，内容缓存功能受限")
    
    def fetch_content(self, url, stock_code, announcement_date):
        """
        抓取并提取公告内容文本
        
        Args:
            url: 公告URL
            stock_code: 股票代码
            announcement_date: 公告日期（用于日志）
            
        Returns:
            dict: 包含提取内容和元数据的字典，格式如下：
                {
                    'success': bool,
                    'content_type': 'pdf'/'html'/'unknown',
                    'extracted_text': str,
                    'content_hash': str,
                    'metadata': dict,
                    'error': str (如果失败)
                }
        """
        logger.info(f"开始抓取公告内容: {stock_code}, URL: {url}")
        
        # 检查缓存（如果可用）
        if self.cache_manager:
            cache_key = self._get_content_cache_key(stock_code, url)
            cached_content = self.cache_manager.get_announcement_content_cache(stock_code, url)
            if cached_content:
                logger.debug(f"使用缓存内容: {stock_code}")
                return {
                    'success': True,
                    'content_type': cached_content.get('metadata', {}).get('content_type', 'unknown'),
                    'extracted_text': cached_content.get('content', ''),
                    'content_hash': cached_content.get('metadata', {}).get('content_hash', ''),
                    'metadata': cached_content.get('metadata', {}),
                    'cached': True
                }
        
        # 下载内容
        download_result = self._download_content(url)
        if not download_result['success']:
            logger.error(f"下载公告内容失败: {download_result.get('error')}")
            return {
                'success': False,
                'error': f"下载失败: {download_result.get('error')}",
                'content_type': 'unknown',
                'extracted_text': '',
                'content_hash': ''
            }
        
        content_bytes = download_result['content']
        content_type = download_result['content_type']
        headers = download_result.get('headers', {})
        
        # 计算内容哈希
        content_hash = hashlib.md5(content_bytes).hexdigest()
        
        # 如果是PDF文件，保存原始PDF到缓存
        pdf_file_path = None
        if content_type == 'pdf' and self.cache_manager:
            try:
                pdf_metadata = {
                    'stock_code': stock_code,
                    'announcement_date': announcement_date,
                    'content_hash': content_hash,
                    'content_length': len(content_bytes)
                }
                pdf_file_path = self.cache_manager.save_pdf_file(
                    stock_code, url, content_bytes, pdf_metadata
                )
                if pdf_file_path:
                    logger.info(f"PDF文件已保存到本地: {pdf_file_path}")
            except Exception as e:
                logger.warning(f"保存PDF文件失败: {e}")
        
        # 根据内容类型提取文本
        extraction_result = self._extract_text(content_bytes, content_type, url)
        
        if not extraction_result['success']:
            logger.warning(f"提取公告文本失败: {extraction_result.get('error')}")
            return {
                'success': False,
                'error': f"提取失败: {extraction_result.get('error')}",
                'content_type': content_type,
                'extracted_text': '',
                'content_hash': content_hash
            }
        
        extracted_text = extraction_result['text']
        logger.info(f"提取文本长度: {len(extracted_text)}, 前200字符: {extracted_text[:200]}...")
        metadata = extraction_result.get('metadata', {})
        
        # 构建完整结果
        result = {
            'success': True,
            'content_type': content_type,
            'extracted_text': extracted_text,
            'content_hash': content_hash,
            'pdf_file_path': pdf_file_path,  # 添加PDF文件路径
            'metadata': {
                'url': url,
                'stock_code': stock_code,
                'announcement_date': announcement_date,
                'content_type': content_type,
                'content_length': len(content_bytes),
                'content_hash': content_hash,
                'extraction_timestamp': datetime.now().isoformat(),
                'pdf_file_path': pdf_file_path,  # 也在metadata中添加
                **metadata
            }
        }
        
        # 缓存结果（如果可用）
        if self.cache_manager:
            try:
                self.cache_manager.set_announcement_content_cache(
                    stock_code, url, extracted_text, result['metadata']
                )
                logger.debug(f"公告内容已缓存: {stock_code}")
            except Exception as e:
                logger.warning(f"缓存公告内容失败: {e}")
        
        logger.info(f"公告内容抓取完成: {stock_code}, 类型: {content_type}, 长度: {len(extracted_text)}字符")
        return result
    
    def _download_content(self, url):
        """
        下载URL内容，支持重试和大小限制
        
        Returns:
            dict: 下载结果
        """
        for attempt in range(self.retry_times):
            try:
                logger.debug(f"下载内容尝试 {attempt+1}/{self.retry_times}: {url}")
                
                headers = {
                    'User-Agent': self.user_agent,
                    'Accept': 'text/html,application/xhtml+xml,application/xml,application/pdf,*/*',
                    'Accept-Language': 'zh-CN,zh;q=0.9',
                }
                
                response = requests.get(
                    url, 
                    headers=headers, 
                    timeout=self.timeout,
                    stream=True  # 流式下载以便控制大小
                )
                response.raise_for_status()
                
                # 检查内容类型
                content_type = response.headers.get('Content-Type', '').lower()
                
                # 根据内容类型确定大小限制
                if 'pdf' in content_type:
                    max_size = self.max_pdf_size
                    detected_type = 'pdf'
                else:
                    max_size = self.max_html_size
                    detected_type = 'html' if 'html' in content_type or 'text' in content_type else 'unknown'
                
                # 流式读取，限制大小
                content_bytes = b''
                for chunk in response.iter_content(chunk_size=8192):
                    content_bytes += chunk
                    if len(content_bytes) > max_size:
                        logger.warning(f"内容大小超过限制: {len(content_bytes)} > {max_size}")
                        return {
                            'success': False,
                            'error': f'内容大小超过限制 ({len(content_bytes)} > {max_size})',
                            'content_type': detected_type
                        }
                
                logger.debug(f"下载成功: {len(content_bytes)}字节, 类型: {detected_type}")
                return {
                    'success': True,
                    'content': content_bytes,
                    'content_type': detected_type,
                    'headers': dict(response.headers)
                }
                
            except requests.exceptions.Timeout:
                logger.warning(f"下载超时 (尝试 {attempt+1}): {url}")
                if attempt < self.retry_times - 1:
                    import time
                    time.sleep(self.retry_delay)
                else:
                    return {
                        'success': False,
                        'error': '下载超时',
                        'content_type': 'unknown'
                    }
            except requests.exceptions.RequestException as e:
                logger.warning(f"下载请求错误 (尝试 {attempt+1}): {e}")
                if attempt < self.retry_times - 1:
                    import time
                    time.sleep(self.retry_delay)
                else:
                    return {
                        'success': False,
                        'error': f'请求错误: {e}',
                        'content_type': 'unknown'
                    }
            except Exception as e:
                logger.error(f"下载未知错误: {e}")
                return {
                    'success': False,
                    'error': f'未知错误: {e}',
                    'content_type': 'unknown'
                }
        
        return {
            'success': False,
            'error': '下载失败（重试次数用尽）',
            'content_type': 'unknown'
        }
    
    def _extract_text(self, content_bytes, content_type, url):
        """
        从字节内容提取文本
        
        Args:
            content_bytes: 原始字节
            content_type: 内容类型 ('pdf', 'html', 'unknown')
            url: 原始URL（用于日志）
            
        Returns:
            dict: 提取结果
        """
        try:
            if content_type == 'pdf' and PDFPLUMBER_AVAILABLE:
                return self._extract_text_from_pdf(content_bytes)
            elif content_type == 'html' and BEAUTIFULSOUP_AVAILABLE:
                return self._extract_text_from_html(content_bytes, url)
            elif content_type == 'html':
                # 简单文本提取（无BeautifulSoup）
                return self._extract_text_from_html_fallback(content_bytes)
            else:
                # 未知类型或缺少库，尝试通用文本提取
                return self._extract_text_generic(content_bytes, content_type)
                
        except Exception as e:
            logger.error(f"提取文本时发生错误: {e}")
            return {
                'success': False,
                'error': f'提取错误: {e}',
                'text': '',
                'metadata': {}
            }
    
    def _extract_text_from_pdf(self, content_bytes):
        """从PDF提取文本（使用pdfplumber）"""
        try:
            import pdfplumber
            
            text_parts = []
            metadata = {
                'pdf_page_count': 0,
                'pdf_has_text': False,
                'pdf_has_images': False
            }
            
            with pdfplumber.open(io.BytesIO(content_bytes)) as pdf:
                metadata['pdf_page_count'] = len(pdf.pages)
                
                for page_num, page in enumerate(pdf.pages, 1):
                    try:
                        page_text = page.extract_text()
                        if page_text:
                            text_parts.append(page_text)
                            metadata['pdf_has_text'] = True
                    except Exception as e:
                        logger.debug(f"提取PDF第{page_num}页文本失败: {e}")
                
                # 检查是否有图片（简单检查）
                for page in pdf.pages:
                    if page.images:
                        metadata['pdf_has_images'] = True
                        break
            
            extracted_text = '\n\n'.join(text_parts)
            
            # 清理文本：移除多余空格和换行
            extracted_text = re.sub(r'\n{3,}', '\n\n', extracted_text)
            extracted_text = re.sub(r'[ \t]{2,}', ' ', extracted_text)
            
            # 限制长度
            if len(extracted_text) > self.extract_max_length:
                extracted_text = extracted_text[:self.extract_max_length] + '... (内容过长，已截断)'
            
            logger.debug(f"PDF提取完成: {metadata['pdf_page_count']}页, 文本长度: {len(extracted_text)}")
            
            return {
                'success': True,
                'text': extracted_text,
                'metadata': metadata
            }
            
        except Exception as e:
            logger.error(f"PDF解析失败: {e}")
            return {
                'success': False,
                'error': f'PDF解析失败: {e}',
                'text': '',
                'metadata': {}
            }
    
    def _extract_text_from_html(self, content_bytes, url):
        """从HTML提取文本（使用BeautifulSoup）"""
        try:
            # 尝试多种编码
            encodings = ['utf-8', 'gbk', 'gb2312', 'gb18030', 'big5']
            html_content = None
            
            for encoding in encodings:
                try:
                    html_content = content_bytes.decode(encoding)
                    break
                except UnicodeDecodeError:
                    continue
            
            if html_content is None:
                # 如果所有编码都失败，使用utf-8并忽略错误
                html_content = content_bytes.decode('utf-8', errors='ignore')
            
            soup = BeautifulSoup(html_content, 'lxml' if 'lxml' in locals() else 'html.parser')
            
            # 移除脚本和样式标签
            for script in soup(['script', 'style', 'noscript', 'iframe']):
                script.decompose()
            
            # 查找主要内容区域（常见模式）
            main_content = None
            content_selectors = [
                '.content', '.article', '.main', '.news_content',
                '#content', '#article', '#main',
                'div[class*="content"]', 'div[class*="article"]',
                'div[class*="news"]', 'div[class*="detail"]'
            ]
            
            for selector in content_selectors:
                elements = soup.select(selector)
                if elements:
                    main_content = elements[0]
                    break
            
            # 如果没有找到特定区域，使用body或整个文档
            if not main_content:
                main_content = soup.find('body') or soup
            
            # 提取文本
            text = main_content.get_text(separator='\n', strip=True)
            
            # 清理文本
            lines = [line.strip() for line in text.split('\n') if line.strip()]
            cleaned_text = '\n'.join(lines)
            
            # 移除过多空行
            cleaned_text = re.sub(r'\n{3,}', '\n\n', cleaned_text)
            
            # 限制长度
            if len(cleaned_text) > self.extract_max_length:
                cleaned_text = cleaned_text[:self.extract_max_length] + '... (内容过长，已截断)'
            
            # 提取元数据
            metadata = {
                'html_title': soup.title.string if soup.title else '',
                'html_charset': soup.original_encoding or 'unknown',
                'html_has_main_content': main_content is not None,
                'url_domain': urlparse(url).netloc if url else ''
            }
            
            logger.debug(f"HTML提取完成: 标题: {metadata['html_title'][:50]}..., 长度: {len(cleaned_text)}")
            
            return {
                'success': True,
                'text': cleaned_text,
                'metadata': metadata
            }
            
        except Exception as e:
            logger.error(f"HTML解析失败: {e}")
            # 回退到简单提取
            return self._extract_text_from_html_fallback(content_bytes)
    
    def _extract_text_from_html_fallback(self, content_bytes):
        """HTML简单文本提取（无BeautifulSoup）"""
        try:
            # 尝试解码
            try:
                html_content = content_bytes.decode('utf-8', errors='ignore')
            except:
                html_content = content_bytes.decode('gbk', errors='ignore')
            
            # 简单移除HTML标签
            text = re.sub(r'<script.*?</script>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r'<style.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r'<[^>]+>', ' ', text)
            
            # 解码HTML实体
            import html
            text = html.unescape(text)
            
            # 清理空白
            lines = [line.strip() for line in text.split('\n') if line.strip()]
            cleaned_text = '\n'.join(lines)
            cleaned_text = re.sub(r'[ \t]{2,}', ' ', cleaned_text)
            cleaned_text = re.sub(r'\n{3,}', '\n\n', cleaned_text)
            
            # 限制长度
            if len(cleaned_text) > self.extract_max_length:
                cleaned_text = cleaned_text[:self.extract_max_length] + '... (内容过长，已截断)'
            
            return {
                'success': True,
                'text': cleaned_text,
                'metadata': {'extraction_method': 'fallback'}
            }
            
        except Exception as e:
            logger.error(f"HTML简单提取失败: {e}")
            return {
                'success': False,
                'error': f'HTML简单提取失败: {e}',
                'text': '',
                'metadata': {}
            }
    
    def _extract_text_generic(self, content_bytes, content_type):
        """通用文本提取（用于未知类型）"""
        try:
            # 尝试解码为文本
            try:
                text = content_bytes.decode('utf-8', errors='ignore')
            except:
                text = content_bytes.decode('gbk', errors='ignore')
            
            # 如果是二进制数据但非文本，返回空
            if not any(c.isprintable() or c in '\n\r\t' for c in text[:1000]):
                return {
                    'success': False,
                    'error': '内容似乎不是文本格式',
                    'text': '',
                    'metadata': {'content_type': content_type}
                }
            
            # 清理和限制长度
            text = text.strip()
            if len(text) > self.extract_max_length:
                text = text[:self.extract_max_length] + '... (内容过长，已截断)'
            
            return {
                'success': True,
                'text': text,
                'metadata': {'content_type': content_type, 'extraction_method': 'generic'}
            }
            
        except Exception as e:
            logger.error(f"通用文本提取失败: {e}")
            return {
                'success': False,
                'error': f'通用提取失败: {e}',
                'text': '',
                'metadata': {}
            }
    
    def _get_content_cache_key(self, stock_code, url):
        """生成内容缓存键（内部使用）"""
        key_string = f"{stock_code}_{url}"
        return hashlib.md5(key_string.encode('utf-8')).hexdigest()
    
    def clear_cache(self):
        """清理内容缓存（可选）"""
        if self.cache_manager:
            # 注意：CacheManager目前不清理公告缓存
            logger.info("内容缓存清理需要手动实现")
            return False
        return True