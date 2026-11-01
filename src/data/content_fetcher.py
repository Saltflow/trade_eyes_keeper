#!/usr/bin/env python3
"""
公告内容抓取和文本提取模块
支持PDF和HTML格式，共用完整资料缓存，预览截断不改原文。
"""

import hashlib
import logging
import re

import requests

# 预声明以便静态检查通过
BeautifulSoup = None  # type: ignore

# 尝试导入PDF解析库
try:
    import pdfplumber  # noqa: F401

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
    BeautifulSoup = None  # type: ignore
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
        announcement_config = config.get("announcements", {})

        # 配置参数
        self.timeout = 30
        self.retry_times = 3
        self.retry_delay = 2
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

        # 大小限制（字节）
        self.max_pdf_size = announcement_config.get("max_pdf_size_mb", 10) * 1024 * 1024
        self.max_html_size = 5 * 1024 * 1024  # 5MB

        # 内容提取配置
        # 提升到128k，保证三大报表不会被截断
        self.extract_max_length = 128000  # 最大提取字符数
        self.extract_timeout = 60  # 提取超时时间（秒）

        from pathlib import Path

        from src.data_hub import DataHub
        from src.platform_composition import authority_for

        project_root = config.get("project_root") or Path(__file__).resolve().parents[2]
        self.project_root = Path(project_root).resolve()
        self.hub = DataHub(authority_for(project_root, config))
        # Compatibility hook only. New originals and full parses live in DataHub;
        # a legacy preview cache is not authoritative evidence.
        self.cache_manager = None

    def fetch_content(self, url, stock_code, announcement_date):
        try:
            self._import_legacy_pdf(stock_code, url)
        except (OSError, ValueError, KeyError, TypeError) as error:
            return {
                "success": False,
                "error": str(error),
                "content_type": "unknown",
                "extracted_text": "",
                "content_hash": "",
            }
        download = self._download_content(url)
        if not download.get("success"):
            return {
                "success": False,
                "error": download.get("error", "download_failed"),
                "content_type": "unknown",
                "extracted_text": "",
                "content_hash": "",
            }
        raw, kind = download["content"], download["content_type"]
        extracted = self._extract_text(raw, kind, url)
        if not extracted.get("success"):
            return {
                "success": False,
                "error": extracted.get("error", "parse_failed"),
                "content_type": kind,
                "extracted_text": "",
                "content_hash": hashlib.md5(raw).hexdigest(),
            }
        digest = hashlib.sha256(raw).hexdigest()
        pdf_path = download.get("original_path") if kind == "pdf" else None
        metadata = {
            "url": url,
            "stock_code": stock_code,
            "announcement_date": announcement_date,
            "content_type": kind,
            "content_length": len(raw),
            "content_hash": hashlib.md5(raw).hexdigest(),
            "document_sha256": digest,
            "pdf_file_path": pdf_path,
            "authority": self.hub.authority.to_dict(),
            **extracted.get("metadata", {}),
        }
        return {
            "success": True,
            "content_type": kind,
            "extracted_text": extracted["text"],
            "content_hash": metadata["content_hash"],
            "pdf_file_path": pdf_path,
            "metadata": metadata,
            "cached": download.get("cached", False),
        }

    def _import_legacy_pdf(self, stock_code, url):
        """Read old URL/PDF evidence without constructing its cleanup manager."""
        import json
        from pathlib import Path

        if self.hub.lookup(url):
            return
        key = hashlib.md5(f"{stock_code}_{url}".encode()).hexdigest()
        folder = Path(self.config.get("storage", {}).get("cache_dir", "cache"))
        if not folder.is_absolute():
            folder = self.project_root / folder
        original = folder / "pdf_files" / f"{key}.pdf"
        metadata = original.with_name(f"{key}_metadata.json")
        if not original.is_file() or not metadata.is_file():
            return
        declared = json.loads(metadata.read_text(encoding="utf-8"))
        if declared.get("url") != url or str(declared.get("stock_code")) != str(
            stock_code
        ):
            raise ValueError("legacy_pdf_source_identity_mismatch")
        if declared.get("file_size") != original.stat().st_size:
            raise ValueError("legacy_pdf_size_changed")
        with original.open("rb") as stream:
            raw = stream.read()
        if not raw.startswith(b"%PDF-"):
            raise ValueError("legacy_cached_document_is_not_pdf")
        expected = declared.get("metadata", {}).get("content_hash")
        if expected and expected != hashlib.md5(raw).hexdigest():
            raise ValueError("legacy_pdf_hash_changed")
        self.hub.register_original(original, url=url)

    def _download_content(self, url):
        from pathlib import Path

        from src.task_runtime.request_policy import RequestPolicy

        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/pdf,*/*",
        }
        details = {}

        def download(target):
            policy = RequestPolicy(self.hub.authority)
            policy.wait(url, min_interval=5.0 if "cninfo.com.cn" in url else 1.0)
            response = requests.get(
                url,
                headers=headers,
                timeout=self.timeout,
                stream=True,
                allow_redirects=False,
            )
            try:
                if response.status_code in {401, 403, 429}:
                    retry = str(response.headers.get("Retry-After", "60"))
                    policy.block(
                        url,
                        f"source_http_{response.status_code}",
                        retry_after=max(60, int(retry))
                        if response.status_code == 429 and retry.isdigit()
                        else None,
                    )
                response.raise_for_status()
                if response.status_code != 200:
                    raise ValueError("source_redirect_or_incomplete_requires_adapter")
                mime = response.headers.get("Content-Type", "").lower()
                maximum = self.max_pdf_size if "pdf" in mime else self.max_html_size
                count = 0
                with target.open("xb") as stream:
                    for chunk in response.iter_content(chunk_size=8192):
                        count += len(chunk)
                        if count > maximum:
                            raise ValueError("content_size_exceeds_limit")
                        stream.write(chunk)
                details["headers"] = dict(response.headers)
            finally:
                response.close()

        try:
            stored = self.hub.ensure_original(url, download)
            raw = Path(stored["path"]).read_bytes()
            kind = (
                "pdf"
                if raw.lstrip().startswith(b"%PDF-")
                else "html"
                if raw.lstrip().startswith((b"<", b"\xef\xbb\xbf<"))
                else "unknown"
            )
            if len(raw) > (self.max_pdf_size if kind == "pdf" else self.max_html_size):
                raise ValueError("cached_content_size_exceeds_limit")
            return {
                "success": True,
                "content": raw,
                "content_type": kind,
                "original_path": stored["path"],
                "cached": stored.get("cache_hit", False),
                **details,
            }
        except (OSError, ValueError, RuntimeError, TimeoutError) as error:
            return {"success": False, "content_type": "unknown", "error": str(error)}

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
            if content_type == "pdf":
                return self._extract_text_from_pdf(content_bytes)
            elif content_type == "html" and BEAUTIFULSOUP_AVAILABLE:
                return self._extract_text_from_html(content_bytes, url)
            elif content_type == "html":
                # 简单文本提取（无BeautifulSoup）
                return self._extract_text_from_html_fallback(content_bytes)
            else:
                # 未知类型或缺少库，尝试通用文本提取
                return self._extract_text_generic(content_bytes, content_type)

        except (
            OSError,
            ValueError,
            RuntimeError,
            ImportError,
            KeyError,
            TypeError,
        ) as e:
            logger.error(f"提取文本时发生错误: {e}")
            return {
                "success": False,
                "error": f"提取错误: {e}",
                "text": "",
                "metadata": {},
            }

    def _extract_text_from_pdf(self, content_bytes):
        parsed = self.hub.parse_bytes(content_bytes, "application/pdf")
        view = self.hub.text_view(parsed, max_chars=self.extract_max_length)
        return {
            "success": bool(view["text"].strip()),
            "text": view["text"],
            "metadata": {
                "pdf_page_count": parsed["pages"],
                "pdf_has_text": bool(view["text"].strip()),
                "pdf_has_images": any(
                    p.get("type") == "image_url" for p in parsed["content"]
                ),
                "pdf_tables_extracted": False,
                "pdf_extractor": parsed["parser"],
                "preview_truncated": view["truncated"],
                "parse_ref": view["parse_ref"],
                "document_sha256": view["source_sha256"],
                "source_complete": view["source_complete"],
            },
        }

    def _extract_text_from_html(self, content_bytes, url):
        parsed = self.hub.parse_bytes(content_bytes, "text/html")
        view = self.hub.text_view(parsed, max_chars=self.extract_max_length)
        return {
            "success": bool(view["text"].strip()),
            "text": view["text"],
            "metadata": {
                "html_extractor": parsed["parser"],
                "preview_truncated": view["truncated"],
                "parse_ref": view["parse_ref"],
                "document_sha256": view["source_sha256"],
                "source_complete": view["source_complete"],
            },
        }

    def _extract_text_from_html_fallback(self, content_bytes):
        """HTML简单文本提取（无BeautifulSoup）"""
        try:
            # 尝试解码
            try:
                html_content = content_bytes.decode("utf-8", errors="ignore")
            except (AttributeError, UnicodeError, ValueError, TypeError) as e:
                logger.debug(f"UTF-8 解码失败，回退 GBK: {e}")
                html_content = content_bytes.decode("gbk", errors="ignore")

            # 简单移除HTML标签
            text = re.sub(
                r"<script.*?</script>",
                "",
                html_content,
                flags=re.DOTALL | re.IGNORECASE,
            )
            text = re.sub(
                r"<style.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE
            )
            text = re.sub(r"<[^>]+>", " ", text)

            # 解码HTML实体
            import html

            text = html.unescape(text)

            # 清理空白
            lines = [line.strip() for line in text.split("\n") if line.strip()]
            cleaned_text = "\n".join(lines)
            cleaned_text = re.sub(r"[ \t]{2,}", " ", cleaned_text)
            cleaned_text = re.sub(r"\n{3,}", "\n\n", cleaned_text)

            # 限制长度
            if len(cleaned_text) > self.extract_max_length:
                cleaned_text = (
                    cleaned_text[: self.extract_max_length] + "... (内容过长，已截断)"
                )

            return {
                "success": True,
                "text": cleaned_text,
                "metadata": {"extraction_method": "fallback"},
            }

        except (AttributeError, UnicodeError, ValueError, TypeError) as e:
            logger.error(f"HTML简单提取失败: {e}")
            return {
                "success": False,
                "error": f"HTML简单提取失败: {e}",
                "text": "",
                "metadata": {},
            }

    def _try_fetch_embedded_pdf(self, html_bytes, base_url):
        """在HTML中查找PDF链接并下载，若成功返回PDF字节"""
        try:
            html_text = html_bytes.decode("utf-8", errors="ignore")
        except (AttributeError, UnicodeError, ValueError, TypeError) as e:
            try:
                logger.debug(f"UTF-8 解码失败，回退 GBK: {e}")
                html_text = html_bytes.decode("gbk", errors="ignore")
            except (AttributeError, UnicodeError, ValueError, TypeError) as e2:
                logger.debug(f"GBK 解码也失败: {e2}")
                return None

        # 优先匹配直接的pdf链接
        import re

        pdf_candidates = re.findall(
            r"href=[\"']([^\"']+\.pdf)[\"']", html_text, re.IGNORECASE
        )

        # 如果未匹配到直接以.pdf结尾的链接，尝试解析HTML寻找<a>文本包含PDF的链接
        if not pdf_candidates and BEAUTIFULSOUP_AVAILABLE and BeautifulSoup is not None:
            try:
                soup = BeautifulSoup(
                    html_text, "lxml" if "lxml" in locals() else "html.parser"
                )
                anchors = soup.find_all("a")
                for a in anchors:
                    href = a.get("href")
                    text = (a.get_text() or "").lower()
                    if not href:
                        continue
                    if href.lower().endswith(".pdf") or "pdf" in text:
                        pdf_candidates.append(href)
            except (AttributeError, UnicodeError, ValueError, TypeError) as e:
                logger.debug(f"解析HTML寻找PDF链接失败: {e}")

        if not pdf_candidates:
            return None

        pdf_url = pdf_candidates[0]
        # 处理相对链接
        if pdf_url.startswith("//"):
            pdf_url = "https:" + pdf_url
        elif pdf_url.startswith("/") and base_url:
            from urllib.parse import urljoin

            pdf_url = urljoin(base_url, pdf_url)

        downloaded = self._download_content(pdf_url)
        if downloaded.get("success") and downloaded.get("content_type") == "pdf":
            return downloaded["content"]
        return None

    def _extract_text_generic(self, content_bytes, content_type):
        """通用文本提取（用于未知类型）"""
        try:
            # 尝试解码为文本
            try:
                text = content_bytes.decode("utf-8", errors="ignore")
            except (AttributeError, UnicodeError, ValueError, TypeError) as e:
                logger.debug(f"UTF-8 解码失败，回退 GBK: {e}")
                text = content_bytes.decode("gbk", errors="ignore")

            # 如果是二进制数据但非文本，返回空
            if not any(c.isprintable() or c in "\n\r\t" for c in text[:1000]):
                return {
                    "success": False,
                    "error": "内容似乎不是文本格式",
                    "text": "",
                    "metadata": {"content_type": content_type},
                }

            # 清理和限制长度
            text = text.strip()
            if len(text) > self.extract_max_length:
                text = text[: self.extract_max_length] + "... (内容过长，已截断)"

            return {
                "success": True,
                "text": text,
                "metadata": {
                    "content_type": content_type,
                    "extraction_method": "generic",
                },
            }

        except (AttributeError, UnicodeError, ValueError, TypeError) as e:
            logger.error(f"通用文本提取失败: {e}")
            return {
                "success": False,
                "error": f"通用提取失败: {e}",
                "text": "",
                "metadata": {},
            }

    def _get_content_cache_key(self, stock_code, url):
        """生成内容缓存键（内部使用）"""
        key_string = f"{stock_code}_{url}"
        return hashlib.md5(key_string.encode("utf-8")).hexdigest()

    def clear_cache(self):
        """清理内容缓存（可选）"""
        if self.cache_manager:
            # 注意：CacheManager目前不清理公告缓存
            logger.info("内容缓存清理需要手动实现")
            return False
        return True
