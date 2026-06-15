"""安全层测试 — 白名单 + 限流。"""

import time

from src.interactive.security import RateLimiter, SecurityGate


class TestSecurityGate:
    def test_whitelist_allows_known_chat_id(self):
        gate = SecurityGate(allowed_chat_ids={"123", "456"})
        assert gate.is_allowed("123") is True

    def test_whitelist_rejects_unknown_chat_id(self):
        gate = SecurityGate(allowed_chat_ids={"123"})
        assert gate.is_allowed("999") is False

    def test_whitelist_empty_allows_none(self):
        gate = SecurityGate(allowed_chat_ids=set())
        assert gate.is_allowed("123") is False

    def test_whitelist_str_int_conversion(self):
        gate = SecurityGate(allowed_chat_ids={"123"})
        assert gate.is_allowed(123) is True


class TestRateLimiter:
    def test_allows_within_limit(self):
        rl = RateLimiter(max_per_minute=5)
        for _ in range(5):
            assert rl.check("user1") is True

    def test_blocks_over_limit(self):
        rl = RateLimiter(max_per_minute=5)
        for _ in range(5):
            rl.check("user1")
        assert rl.check("user1") is False

    def test_separate_users_independent_limits(self):
        rl = RateLimiter(max_per_minute=3)
        for _ in range(3):
            assert rl.check("user1") is True
        assert rl.check("user1") is False
        assert rl.check("user2") is True  # different user

    def test_reset_after_window(self):
        rl = RateLimiter(max_per_minute=2, window_seconds=0.1)
        for _ in range(2):
            rl.check("user1")
        assert rl.check("user1") is False
        time.sleep(0.15)
        assert rl.check("user1") is True  # window expired
