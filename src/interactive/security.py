"""安全层 — 白名单 + 限流。"""

import time
from collections import defaultdict


class SecurityGate:
    """白名单：只允许已配置的 Telegram 用户操作。"""

    def __init__(self, allowed_chat_ids: set[str]):
        self.allowed = {str(cid) for cid in allowed_chat_ids}

    def is_allowed(self, chat_id) -> bool:
        return str(chat_id) in self.allowed


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
