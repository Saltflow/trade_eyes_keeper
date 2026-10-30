"""安全层 — 白名单 + 限流。"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, defaultdict


class SecurityGate:
    """白名单：只允许已配置的 Telegram 用户操作。"""

    def __init__(self, allowed_chat_ids: set[str], *, allow_all: bool = False):
        self.allowed = {str(cid) for cid in allowed_chat_ids}
        self.allow_all = allow_all

    def is_allowed(self, chat_id) -> bool:
        if chat_id is None or not str(chat_id).strip():
            return False
        return self.allow_all or str(chat_id) in self.allowed


class RateLimiter:
    """简单滑动窗口限流。"""

    def __init__(self, max_per_minute: int = 10, window_seconds: float = 60.0):
        self.max_per_minute = max_per_minute
        self.window_seconds = window_seconds
        self._buckets: dict[str, list[float]] = defaultdict(list)

    def check(self, user_id: str) -> bool:
        now = time.time()
        cutoff = now - self.window_seconds

        bucket = self._buckets[user_id]
        bucket[:] = [ts for ts in bucket if ts > cutoff]

        if len(bucket) >= self.max_per_minute:
            return False

        bucket.append(now)
        return True


class BotAccess:
    """A bot's allowlist, rate limit and bounded redelivery cache for its lifetime."""

    def __init__(self, config: dict, channel: str, *, allow_wildcard: bool = False):
        self.channel = channel
        settings = (config.get("interactive") or {}).get(channel) or {}
        self.enabled = settings.get("enabled") is True
        allowed = settings.get("allowed_chat_ids", [])
        self.allowed_chat_ids = (
            {
                str(value).strip()
                for value in allowed
                if str(value).strip() not in {"", "None"}
            }
            if isinstance(allowed, (list, tuple, set))
            else set()
        )
        self.allow_all = allow_wildcard and "*" in self.allowed_chat_ids
        if not allow_wildcard:
            self.allowed_chat_ids.discard("*")
        self.gate = SecurityGate(self.allowed_chat_ids, allow_all=self.allow_all)
        rate = settings.get("rate_limit_per_minute", 10)
        if isinstance(rate, bool) or not isinstance(rate, int) or rate < 1:
            raise ValueError(
                f"interactive.{channel}.rate_limit_per_minute must be positive"
            )
        self.rate_limiter = RateLimiter(max_per_minute=rate)
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()

    def validate_config(self) -> None:
        if not self.enabled:
            raise ValueError(f"interactive.{self.channel}.enabled must be true")
        if not self.allowed_chat_ids:
            raise ValueError(
                f"interactive.{self.channel}.allowed_chat_ids must not be empty"
            )

    def accept(self, chat_id: str, message_id: str = "") -> str:
        if not self.enabled:
            return "disabled"
        if not self.gate.is_allowed(chat_id):
            return "unauthorized"
        with self._lock:
            if message_id and message_id in self._seen:
                return "duplicate"
            if message_id:
                self._seen[message_id] = None
                # A bounded cache is enough for immediate transport redelivery.
                while len(self._seen) > 1024:
                    self._seen.popitem(last=False)
            return "accepted" if self.rate_limiter.check(chat_id) else "rate_limited"
