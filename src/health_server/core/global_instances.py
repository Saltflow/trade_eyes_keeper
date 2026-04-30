"""
全局实例模块 - 提供共享的速率限制器、OTP管理器、会话管理器实例
"""

import logging
from datetime import datetime
from pathlib import Path

from ..auth.rate_limiter import RateLimiter
from ..auth.otp_manager import OTPManager
from ..auth.auth_session import AuthSessionManager

logger = logging.getLogger(__name__)

# 全局速率限制器实例 (1 QPS = 60请求/分钟，60秒窗口)
rate_limiter = RateLimiter(requests_per_minute=60, window_seconds=60)  # 1 QPS

# OTP专用速率限制器 (1请求/5分钟，300秒窗口)
otp_rate_limiter = RateLimiter(
    requests_per_minute=1, window_seconds=300
)  # 1请求每5分钟

# OTP管理器实例 (10分钟有效期)
otp_manager = OTPManager(expiry_minutes=10)

# 会话管理器实例 (30分钟有效期)
session_manager = AuthSessionManager(expiry_minutes=30)


def audit_log(action, ip_address, details=""):
    """
    记录管理操作审计日志

    Args:
        action: 操作类型 (OTP_REQUEST, OTP_VERIFY, WATCHLIST_UPDATE等)
        ip_address: 客户端IP地址
        details: 操作详情
    """
    try:
        audit_log_file = (
            Path(__file__).parent.parent.parent / "logs" / "management_audit.log"
        )
        audit_log_file.parent.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"{timestamp} | {ip_address} | {action} | {details}\n"

        with open(audit_log_file, "a", encoding="utf-8") as f:
            f.write(log_entry)

        logger.debug(f"审计日志: {action} - {ip_address} - {details}")
    except Exception as e:
        logger.error(f"写入审计日志失败: {e}")


# ── 报告 Token 系统 ──
# 用于在邮件中嵌入有时效的策略搜索报告链接
import time as _time  # noqa: E402
import uuid as _uuid  # noqa: E402

_report_tokens: dict[str, tuple] = {}  # token → (Path, expiry_ts)
_report_token_timeout = 30  # 默认 30 分钟


def set_report_token_timeout(minutes: int):
    global _report_token_timeout
    if minutes > 0:
        _report_token_timeout = minutes


def register_report_token(html_path):
    """注册报告 token，返回 token 字符串"""
    token = _uuid.uuid4().hex[:12]
    _report_tokens[token] = (Path(html_path), _time.time() + _report_token_timeout * 60)
    logger.info("报告 token 已注册: %s", token)
    return token


def get_report_path(token):
    """校验 token 返回文件路径, 路径遍历保护, 自动清理过期"""
    now = _time.time()
    expired = [t for t, (_, exp) in _report_tokens.items() if now > exp]
    for t in expired:
        del _report_tokens[t]
    entry = _report_tokens.get(token)
    if not entry:
        return None
    path, expiry = entry
    if now > expiry:
        del _report_tokens[token]
        return None
    # 路径遍历保护
    try:
        resolved = Path(path).resolve()
        allowed = Path("data/optimizer").resolve()
        resolved.relative_to(allowed)
    except (ValueError, OSError):
        logger.warning("报告 token 路径逃逸: %s", token)
        return None
    return Path(path)
