"""
OTP管理器模块 - 生成和验证5位数字验证码，10分钟有效期
"""

import threading
import time
import random
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class OTPManager:
    """OTP管理器 - 生成和验证5位数字验证码，10分钟有效期"""

    def __init__(self, expiry_minutes=10):
        """
        初始化OTP管理器

        Args:
            expiry_minutes: OTP有效期（分钟），默认10分钟
        """
        self.expiry_minutes = expiry_minutes
        self.otp_store = {}  # ip -> {'code': '12345', 'expiry': timestamp, 'email_sent': bool}
        self.lock = threading.Lock()
        self.cleanup_interval = 300  # 每5分钟清理一次过期OTP
        self.last_cleanup = time.time()

    def generate(self, ip_address):
        """
        为指定IP生成新的OTP

        Args:
            ip_address: 客户端IP地址

        Returns:
            str: 5位数字OTP，或None（如果已有未过期的OTP）
        """
        with self.lock:
            # 清理过期OTP
            self._cleanup_expired()

            # 检查是否已有未过期的OTP
            if ip_address in self.otp_store:
                otp_data = self.otp_store[ip_address]
                if time.time() < otp_data["expiry"]:
                    return None  # 已有有效OTP

            # 生成5位数字OTP
            otp_code = str(random.randint(10000, 99999))
            expiry_time = time.time() + (self.expiry_minutes * 60)

            self.otp_store[ip_address] = {
                "code": otp_code,
                "expiry": expiry_time,
                "created": time.time(),
                "email_sent": False,
            }

            logger.info(
                f"为IP {ip_address} 生成OTP: {otp_code}, 有效期至 {datetime.fromtimestamp(expiry_time).strftime('%H:%M:%S')}"
            )
            return otp_code

    def validate(self, ip_address, code):
        """
        验证OTP代码

        Args:
            ip_address: 客户端IP地址
            code: 用户输入的OTP代码

        Returns:
            tuple: (是否成功, 错误消息)
        """
        with self.lock:
            # 清理过期OTP
            self._cleanup_expired()

            if ip_address not in self.otp_store:
                return False, "验证码不存在或已过期"

            otp_data = self.otp_store[ip_address]

            # 检查是否过期
            if time.time() > otp_data["expiry"]:
                del self.otp_store[ip_address]
                return False, "验证码已过期"

            # 验证代码（不区分大小写，去除空格）
            user_code = str(code).strip()
            stored_code = otp_data["code"]

            if user_code != stored_code:
                return False, "验证码不正确"

            # 验证成功，删除OTP防止重复使用
            del self.otp_store[ip_address]
            logger.info(f"IP {ip_address} OTP验证成功")
            return True, "验证成功"

    def mark_email_sent(self, ip_address):
        """标记OTP邮件已发送"""
        with self.lock:
            if ip_address in self.otp_store:
                self.otp_store[ip_address]["email_sent"] = True

    def get_remaining_time(self, ip_address):
        """
        获取剩余有效期（秒）

        Returns:
            int: 剩余秒数，-1表示OTP不存在
        """
        with self.lock:
            if ip_address not in self.otp_store:
                return -1

            otp_data = self.otp_store[ip_address]
            remaining = otp_data["expiry"] - time.time()
            return max(0, int(remaining))

    def _cleanup_expired(self):
        """清理过期OTP"""
        now = time.time()
        # 每5分钟清理一次
        if now - self.last_cleanup < self.cleanup_interval:
            return

        expired_ips = []
        for ip, data in self.otp_store.items():
            if now > data["expiry"]:
                expired_ips.append(ip)

        for ip in expired_ips:
            del self.otp_store[ip]

        if expired_ips:
            logger.debug(f"清理了 {len(expired_ips)} 个过期OTP")

        self.last_cleanup = now
