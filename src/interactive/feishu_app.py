"""飞书开放平台应用客户端 — token / 事件 / 消息。"""

import hashlib
import json
import logging
import os
import time

import requests

from .security import RateLimiter, SecurityGate

logger = logging.getLogger(__name__)

FEISHU_API = "https://open.feishu.cn/open-apis"


class FeishuApp:
    """飞书自建应用 Bot：接收事件、发送卡片消息。"""

    def __init__(self, config: dict):
        ic = config.get("interactive", {}).get("feishu", {})
        self.app_id = ic.get("app_id") or os.getenv("FEISHU_APP_ID", "")
        self.app_secret = ic.get("app_secret") or os.getenv("FEISHU_APP_SECRET", "")
        self.verification_token = ic.get("verification_token") or os.getenv(
            "FEISHU_VERIFICATION_TOKEN", ""
        )
        self.encrypt_key = ic.get("encrypt_key") or os.getenv(
            "FEISHU_ENCRYPT_KEY", ""
        )

        allowed = ic.get("allowed_chat_ids", [])
        has_wildcard = any(str(cid).strip() == "*" for cid in allowed)
        self.allowed_chat_ids = set(str(cid) for cid in allowed if cid and str(cid).strip())
        self._allow_all = has_wildcard or not bool(self.allowed_chat_ids)
        self.gate = SecurityGate(self.allowed_chat_ids) if self.allowed_chat_ids else None
        self.rate_limiter = RateLimiter(
            max_per_minute=ic.get("rate_limit_per_minute", 10)
        )

        self._token: str = ""
        self._token_expires_at: float = 0

        self._enabled = bool(self.app_id and self.app_secret)

    # ── Token ──────────────────────────────────────

    def get_tenant_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_expires_at - 60:
            return self._token

        url = f"{FEISHU_API}/auth/v3/tenant_access_token/internal"
        payload = {"app_id": self.app_id, "app_secret": self.app_secret}
        try:
            resp = requests.post(url, json=payload, timeout=10)
            data = resp.json()
            if data.get("code") == 0:
                self._token = data["tenant_access_token"]
                self._token_expires_at = now + data.get("expire", 7200)
                return self._token
            logger.error(f"飞书 token 获取失败: {data}")
        except Exception as e:
            logger.error(f"飞书 token 请求异常: {e}")
        return ""

    # ── 事件验证 ──────────────────────────────────

    def verify_event(self, body: dict) -> dict | bool:
        """验证事件：challenge → 返回 {"challenge": ...}；普通事件 → True。"""
        if body.get("type") == "url_verification":
            challenge = body.get("challenge", "")
            if challenge and body.get("token") == self.verification_token:
                return {"challenge": challenge}
        return True

    def verify_signature(self, headers: dict, body: dict) -> bool:
        """校验 X-Lark-Signature（需要 encrypt_key 已配置）。"""
        if not self.encrypt_key:
            return True  # 未配置加密时不强制校验
        sig = headers.get("X-Lark-Signature", "")
        if not sig:
            return False
        expect = hashlib.sha256(
            f"{int(time.time())}{json.dumps(body, sort_keys=True)}{self.encrypt_key}".encode()
        ).hexdigest()
        return sig == expect

    # ── 发送消息 ─────────────────────────────────

    def send_message(self, chat_id: str, text: str) -> tuple:
        if not self._enabled:
            return False, "App 未配置（缺少 app_id/app_secret）"

        token = self.get_tenant_token()
        if not token:
            return False, "无法获取 tenant token"

        from ..notification.feishu_notifier import _build_interactive_card

        card = _build_interactive_card("股票量化助手", text)
        url = f"{FEISHU_API}/im/v1/messages?receive_id_type=chat_id"
        payload = {
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        }

        try:
            resp = requests.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                timeout=10,
            )
            data = resp.json()
            if data.get("code") == 0:
                return True, "ok"
            return False, f"飞书 code={data.get('code')} {data.get('msg', '')}"
        except Exception as e:
            logger.error(f"飞书消息发送失败: {e}")
            return False, str(e)
