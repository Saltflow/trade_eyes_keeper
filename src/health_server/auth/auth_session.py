"""
会话管理器模块 - 管理认证会话，30分钟有效期，内存存储
"""

import threading
import time
import secrets
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class AuthSessionManager:
    """会话管理器 - 管理认证会话，30分钟有效期，内存存储"""

    def __init__(self, expiry_minutes=30):
        """
        初始化会话管理器

        Args:
            expiry_minutes: 会话有效期（分钟），默认30分钟
        """
        self.expiry_minutes = expiry_minutes
        self.sessions = {}  # token -> {'ip': '127.0.0.1', 'expiry': timestamp, 'created': timestamp}
        self.lock = threading.Lock()
        self.cleanup_interval = 300  # 每5分钟清理一次过期会话
        self.last_cleanup = time.time()

    def create(self, ip_address):
        """
        为新认证用户创建会话

        Args:
            ip_address: 客户端IP地址

        Returns:
            str: 会话令牌
        """
        with self.lock:
            # 清理过期会话
            self._cleanup_expired()

            # 生成32字符的随机令牌
            token = secrets.token_hex(16)
            expiry_time = time.time() + (self.expiry_minutes * 60)

            self.sessions[token] = {
                "ip": ip_address,
                "expiry": expiry_time,
                "created": time.time(),
                "last_activity": time.time(),
            }

            logger.info(
                f"为IP {ip_address} 创建会话，令牌: {token[:8]}..., 有效期至 {datetime.fromtimestamp(expiry_time).strftime('%H:%M:%S')}"
            )
            return token

    def validate(self, token, ip_address):
        """
        验证会话令牌

        Args:
            token: 会话令牌
            ip_address: 客户端IP地址（用于IP绑定）

        Returns:
            tuple: (是否有效, 错误消息)
        """
        with self.lock:
            # 清理过期会话
            self._cleanup_expired()

            if token not in self.sessions:
                return False, "会话无效或已过期"

            session_data = self.sessions[token]

            # 检查IP绑定
            if session_data["ip"] != ip_address:
                logger.warning(
                    f"会话令牌IP不匹配: 令牌IP={session_data['ip']}, 请求IP={ip_address}"
                )
                return False, "会话无效"

            # 检查是否过期
            if time.time() > session_data["expiry"]:
                del self.sessions[token]
                return False, "会话已过期"

            # 更新最后活动时间
            session_data["last_activity"] = time.time()

            # 计算剩余时间
            remaining = session_data["expiry"] - time.time()
            logger.debug(f"会话验证成功，剩余时间: {int(remaining)}秒")
            return True, "会话有效"

    def invalidate(self, token):
        """使会话令牌失效"""
        with self.lock:
            if token in self.sessions:
                del self.sessions[token]
                logger.info(f"会话令牌已失效: {token[:8]}...")

    def get_session_info(self, token):
        """获取会话信息"""
        with self.lock:
            if token not in self.sessions:
                return None

            session_data = self.sessions[token]
            return {
                "ip": session_data["ip"],
                "created": datetime.fromtimestamp(session_data["created"]).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "expiry": datetime.fromtimestamp(session_data["expiry"]).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "last_activity": datetime.fromtimestamp(
                    session_data["last_activity"]
                ).strftime("%Y-%m-%d %H:%M:%S"),
                "remaining_seconds": int(max(0, session_data["expiry"] - time.time())),
            }

    def _cleanup_expired(self):
        """清理过期会话"""
        now = time.time()
        # 每5分钟清理一次
        if now - self.last_cleanup < self.cleanup_interval:
            return

        expired_tokens = []
        for token, data in self.sessions.items():
            if now > data["expiry"]:
                expired_tokens.append(token)

        for token in expired_tokens:
            del self.sessions[token]

        if expired_tokens:
            logger.debug(f"清理了 {len(expired_tokens)} 个过期会话")

        self.last_cleanup = now
