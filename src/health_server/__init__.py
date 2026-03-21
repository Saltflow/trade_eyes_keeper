"""
健康服务器模块 - 提供系统状态监控、OTP认证、会话管理等功能
"""

# 导出核心类
from .core.health_server import HealthServer
from .core.start_server import start_health_server

# 导出工具类（可选）
from .auth.rate_limiter import RateLimiter
from .auth.otp_manager import OTPManager
from .auth.session_manager import SessionManager

# 全局实例（保持向后兼容）
from .core.global_instances import (
    rate_limiter,
    otp_rate_limiter,
    otp_manager,
    session_manager,
    audit_log,
)

__all__ = [
    "HealthServer",
    "start_health_server",
    "RateLimiter",
    "OTPManager",
    "SessionManager",
    "rate_limiter",
    "otp_rate_limiter",
    "otp_manager",
    "session_manager",
    "audit_log",
]
