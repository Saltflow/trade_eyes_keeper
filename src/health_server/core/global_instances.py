"""
全局实例模块 - 提供共享的速率限制器、OTP管理器、会话管理器实例
"""
import logging
from datetime import datetime
from pathlib import Path

from ..auth.rate_limiter import RateLimiter
from ..auth.otp_manager import OTPManager
from ..auth.session_manager import SessionManager

logger = logging.getLogger(__name__)

# 全局速率限制器实例 (1 QPS = 60请求/分钟，60秒窗口)
rate_limiter = RateLimiter(requests_per_minute=60, window_seconds=60)  # 1 QPS

# OTP专用速率限制器 (1请求/5分钟，300秒窗口)
otp_rate_limiter = RateLimiter(requests_per_minute=1, window_seconds=300)  # 1请求每5分钟

# OTP管理器实例 (10分钟有效期)
otp_manager = OTPManager(expiry_minutes=10)

# 会话管理器实例 (30分钟有效期)
session_manager = SessionManager(expiry_minutes=30)


def audit_log(action, ip_address, details=""):
    """
    记录管理操作审计日志
    
    Args:
        action: 操作类型 (OTP_REQUEST, OTP_VERIFY, WATCHLIST_UPDATE等)
        ip_address: 客户端IP地址
        details: 操作详情
    """
    try:
        audit_log_file = Path(__file__).parent.parent.parent / 'logs' / 'management_audit.log'
        audit_log_file.parent.mkdir(parents=True, exist_ok=True)
        
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        log_entry = f"{timestamp} | {ip_address} | {action} | {details}\n"
        
        with open(audit_log_file, 'a', encoding='utf-8') as f:
            f.write(log_entry)
        
        logger.debug(f"审计日志: {action} - {ip_address} - {details}")
    except Exception as e:
        logger.error(f"写入审计日志失败: {e}")