"""
速率限制器模块
"""

import threading
import time
import logging

logger = logging.getLogger(__name__)


class RateLimiter:
    """简单的请求速率限制器，支持可配置的时间窗口"""

    def __init__(self, requests_per_minute=60, window_seconds=60):
        """
        初始化速率限制器

        Args:
            requests_per_minute: 每分钟允许的最大请求数 (默认60 = 1 QPS)
            window_seconds: 时间窗口（秒），默认60秒（1分钟）
        """
        self.requests_per_minute = requests_per_minute
        self.window_seconds = window_seconds
        self.request_log = {}  # ip -> [timestamp1, timestamp2, ...]
        self.lock = threading.Lock()

    def is_allowed(self, ip_address):
        """
        检查指定IP的请求是否允许

        Args:
            ip_address: 客户端IP地址

        Returns:
            bool: 是否允许请求
        """
        with self.lock:
            now = time.time()

            # 清理过期记录（超过时间窗口）
            if ip_address in self.request_log:
                self.request_log[ip_address] = [
                    t
                    for t in self.request_log[ip_address]
                    if now - t < self.window_seconds
                ]

            # 检查是否超过限制
            current_count = len(self.request_log.get(ip_address, []))
            if current_count >= self.requests_per_minute:
                return False

            # 记录本次请求
            self.request_log.setdefault(ip_address, []).append(now)
            return True

    def get_stats(self):
        """获取速率限制统计信息"""
        with self.lock:
            now = time.time()
            stats = {}
            for ip, timestamps in self.request_log.items():
                # 只统计最近时间窗口内的请求
                recent_requests = [
                    t for t in timestamps if now - t < self.window_seconds
                ]
                stats[ip] = {
                    "recent_requests": len(recent_requests),
                    "blocked": len(recent_requests) >= self.requests_per_minute,
                }
            return stats
