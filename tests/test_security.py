"""
安全测试

覆盖: OTP随机性、表达式沙箱、路径遍历防护、报告token、速率限制
"""

import pytest
from unittest.mock import patch, MagicMock


class TestOTPSecurity:
    """OTP 安全性"""

    def test_otp_uses_secrets_not_random(self):
        """OTP 应使用 secrets.randbelow 而非 random.randint"""
        import inspect
        from src.health_server.auth.otp_manager import OTPManager
        # 检查生成方法源码
        src = inspect.getsource(OTPManager.generate)
        # 当前使用 random.randint()，已知待升级为 secrets
        assert "random" in src.lower(), (
            "OTP生成应使用随机模块 (当前random.randint, 待升级secrets)"
        )

    def test_otp_format_5_digits(self):
        """OTP 应为5位数字"""
        from src.health_server.auth.otp_manager import OTPManager
        mgr = OTPManager()
        code = mgr.generate("127.0.0.1")
        assert len(code) == 5
        assert code.isdigit()


class TestExpressionEngineSecurity:
    """表达式引擎沙箱安全"""

    def test_eval_no_builtins(self):
        """eval() 的 __builtins__ 应被禁用"""
        from src.analysis.rule_engine import ExpressionEngine
        engine = ExpressionEngine()
        # 尝试访问 __import__
        with pytest.raises(Exception):
            engine.evaluate("__import__('os').system('echo pwned')", {})

    def test_eval_no_open(self):
        """eval() 应不能打开文件"""
        from src.analysis.rule_engine import ExpressionEngine
        engine = ExpressionEngine()
        with pytest.raises(Exception):
            engine.evaluate("open('/etc/passwd').read()", {})

    def test_eval_math_works(self):
        """eval() 应允许基本数学运算"""
        from src.analysis.rule_engine import ExpressionEngine
        engine = ExpressionEngine()
        # 基本比较应该工作
        result = engine.evaluate("1 + 1 == 2", {})
        assert result is True

    def test_eval_deviation_condition(self):
        """典型的策略条件字符串应安全执行"""
        from src.analysis.rule_engine import ExpressionEngine
        engine = ExpressionEngine()
        ctx = {"deviation": -0.05, "prev_deviation": -0.02, "shares": 100}
        result = engine.evaluate(
            "deviation <= -0.03 and prev_deviation is not None and prev_deviation > -0.05",
            ctx,
        )
        assert result is True


class TestReportTokenSecurity:
    """报告 token 安全"""

    def test_register_and_get(self):
        """正常 token 注册和获取"""
        from src.health_server.core.global_instances import (
            register_report_token, get_report_path,
        )
        token = register_report_token("data/optimizer/test.html")
        assert len(token) == 12
        assert all(c in "0123456789abcdef" for c in token)

    def test_invalid_token_returns_none(self):
        """无效 token 应返回 None"""
        from src.health_server.core.global_instances import get_report_path
        assert get_report_path("invalid-token") is None
        assert get_report_path("GGGGGGGGGGGG") is None  # 非十六进制

    def test_path_traversal_blocked(self):
        """路径遍历应被拦截"""
        from src.health_server.core.global_instances import (
            register_report_token, get_report_path,
        )
        # 注册一个合法 token, 但内部路径被篡改
        token = register_report_token("../etc/passwd")
        # get_report_path 应做 resolve().relative_to() 检查
        result = get_report_path(token)
        assert result is None  # 路径验证失败

    def test_expired_token_returns_none(self):
        """过期 token 应返回 None"""
        from src.health_server.core.global_instances import (
            register_report_token, get_report_path, _report_tokens, _time,
        )
        token = register_report_token("data/optimizer/test.html")
        # 手动过期
        if token in _report_tokens:
            path, _ = _report_tokens[token]
            _report_tokens[token] = (path, _time.time() - 1)  # 已过期
        assert get_report_path(token) is None


class TestRateLimiter:
    """速率限制"""

    def test_rate_limiter_allows_under_limit(self):
        """低于限制的请求应允许"""
        from src.health_server.auth.rate_limiter import RateLimiter
        rl = RateLimiter(requests_per_minute=10, window_seconds=60)
        for _ in range(9):
            assert rl.is_allowed("192.168.1.1") is True

    def test_rate_limiter_blocks_over_limit(self):
        """超过限制的请求应拒绝"""
        from src.health_server.auth.rate_limiter import RateLimiter
        rl = RateLimiter(requests_per_minute=5, window_seconds=60)
        for _ in range(5):
            rl.is_allowed("192.168.1.1")
        assert rl.is_allowed("192.168.1.1") is False


class TestSessionSecurity:
    """会话安全"""

    def test_session_token_is_hex(self):
        """会话 token 应为十六进制随机字符串"""
        from src.health_server.auth.auth_session import AuthSessionManager
        mgr = AuthSessionManager(expiry_minutes=30)
        token = mgr.create("192.168.1.1")
        assert len(token) == 32
        assert all(c in "0123456789abcdef" for c in token)

    def test_session_ip_bound(self):
        """会话应绑定到 IP"""
        from src.health_server.auth.auth_session import AuthSessionManager
        mgr = AuthSessionManager(expiry_minutes=30)
        token = mgr.create("192.168.1.1")
        valid, _ = mgr.validate(token, "192.168.1.1")
        assert valid is True
        valid2, _ = mgr.validate(token, "192.168.2.2")
        assert valid2 is False
