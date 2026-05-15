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
            Path(__file__).parent.parent.parent.parent / "logs" / "management_audit.log"
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
    """注册报告 token（写入文件，跨进程共享），返回 token 字符串"""
    import json as _json
    token = _uuid.uuid4().hex[:12]
    expiry = _time.time() + _report_token_timeout * 60
    _report_tokens[token] = (Path(html_path), expiry)
    # 同步到文件
    _sync_tokens_to_file()
    logger.info("报告 token 已注册: %s", token)
    return token


def _token_file() -> Path:
    return Path("data/optimizer/.report_tokens.json")


def _sync_tokens_to_file():
    """将内存 token 表写入 JSON 文件"""
    import json as _json
    data = {}
    for token, (path, exp) in _report_tokens.items():
        data[token] = [str(path), exp]
    try:
        _token_file().parent.mkdir(parents=True, exist_ok=True)
        _token_file().write_text(_json.dumps(data), encoding="utf-8")
    except Exception as e:
        logger.debug(f"Token 文件写入失败: {e}")


def _load_tokens_from_file():
    """从 JSON 文件加载 token 表到内存"""
    import json as _json
    tf = _token_file()
    if not tf.exists():
        return
    try:
        data = _json.loads(tf.read_text(encoding="utf-8"))
        now = _time.time()
        for token, (path_s, exp) in data.items():
            if now <= exp:  # 只加载未过期的
                _report_tokens[token] = (Path(path_s), exp)
    except Exception as e:
        logger.debug(f"Token 文件读取失败: {e}")


def get_report_path(token):
    """校验 token 返回文件路径, 路径遍历保护, 自动清理过期"""
    # 从文件加载（如果是 health_server 进程，内存表是空的）
    if not _report_tokens:
        _load_tokens_from_file()
    now = _time.time()
    expired = [t for t, (_, exp) in _report_tokens.items() if now > exp]
    for t in expired:
        del _report_tokens[t]
    if expired:
        _sync_tokens_to_file()  # 清理后同步到文件
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
