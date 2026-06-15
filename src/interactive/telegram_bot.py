"""Telegram 交互机器人 — 轮询 + 命令分发。"""

import logging
import os
import time
from typing import Optional

import requests

from .command_parser import (
    AddCommand,
    BacktestCommand,
    CommandType,
    ErrorCommand,
    HelpCommand,
    ListCommand,
    RemoveCommand,
    parse_command,
)
from .commands.handlers import (
    handle_add,
    handle_backtest,
    handle_help,
    handle_list,
    handle_remove,
)
from .security import RateLimiter, SecurityGate

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


class TelegramBot:
    """Telegram 轮询 Bot。"""

    def __init__(self, config: dict):
        ic = config.get("interactive", {}).get("telegram", {})
        self.bot_token = ic.get("bot_token") or os.getenv("TELEGRAM_BOT_TOKEN", "")
        allowed = ic.get("allowed_chat_ids", [])
        if not allowed or allowed == [""]:
            env_chat = os.getenv("TELEGRAM_CHAT_ID", "")
            allowed = [env_chat] if env_chat else []
        self.allowed_chat_ids = set(str(cid) for cid in allowed if cid)
        self.polling_interval = ic.get("polling_interval", 2)
        self._running = False

        # 代理配置（中国大陆服务器访问 Telegram API 需要）
        proxy_url = (
            ic.get("proxy")
            or os.getenv("TELEGRAM_PROXY")
            or os.getenv("HTTPS_PROXY")
            or os.getenv("https_proxy")
            or ""
        )
        self._proxies = {"https": proxy_url} if proxy_url else None

        self.gate = SecurityGate(self.allowed_chat_ids)
        self.rate_limiter = RateLimiter(
            max_per_minute=ic.get("rate_limit_per_minute", 10)
        )

    def _api(self, method: str, **params) -> Optional[dict]:
        """调用 Telegram Bot API。"""
        url = f"{TELEGRAM_API}/bot{self.bot_token}/{method}"
        try:
            resp = requests.post(
                url, data=params,
                timeout=30 if method == "getUpdates" else 15,
                proxies=self._proxies,
            )
            if resp.status_code != 200:
                logger.error(f"Telegram API {method} HTTP {resp.status_code}")
                return None
            result = resp.json()
            if not result.get("ok"):
                logger.error(
                    f"Telegram API {method} error: {result.get('description')}"
                )
                return None
            return result.get("result")
        except Exception as e:
            logger.error(f"Telegram API {method} 请求失败: {e}")
            return None

    def _send_message(self, chat_id, text: str) -> bool:
        return self._api(
            "sendMessage",
            chat_id=str(chat_id),
            text=text,
            parse_mode="HTML",
            disable_web_page_preview="true",
        ) is not None

    def _get_updates(self, offset: int) -> list[dict]:
        result = self._api("getUpdates", offset=offset, timeout=10)
        return result if isinstance(result, list) else []

    def _process_update(self, update: dict) -> None:
        message = update.get("message")
        if not message:
            return

        chat = message.get("chat", {})
        chat_id = str(chat.get("id", ""))

        if not self.gate.is_allowed(chat_id):
            logger.warning(f"未授权的 chat_id: {chat_id}")
            return

        if not self.rate_limiter.check(chat_id):
            self._send_message(chat_id, "操作过于频繁，请稍后再试。")
            return

        text = message.get("text", "")
        cmd = parse_command(text)

        if isinstance(cmd, HelpCommand):
            response = handle_help()
        elif isinstance(cmd, ListCommand):
            response = handle_list()
        elif isinstance(cmd, AddCommand):
            response = handle_add(cmd.stock_code)
        elif isinstance(cmd, RemoveCommand):
            response = handle_remove(cmd.stock_code)
        elif isinstance(cmd, BacktestCommand):
            self._send_message(
                chat_id,
                f"⏳ 正在回测 <code>{cmd.stock_code}</code>…",
            )
            response = handle_backtest(cmd.stock_code, cmd.start_date, cmd.end_date)
        elif isinstance(cmd, ErrorCommand):
            response = f"❌ {cmd.message}"
        else:
            response = "❌ 未知错误"

        self._send_message(chat_id, response)

    def run(self) -> None:
        """启动轮询循环（阻塞）。"""
        if not self.bot_token:
            logger.error("Telegram bot_token 未配置，无法启动交互模式")
            return
        if not self.allowed_chat_ids:
            logger.warning("未配置 allowed_chat_ids，Bot 将不响应任何消息")

        logger.info("Telegram 交互 Bot 启动")
        self._running = True
        offset = 0

        while self._running:
            try:
                updates = self._get_updates(offset)
                for update in updates:
                    self._process_update(update)
                    offset = max(offset, update.get("update_id", 0) + 1)
            except Exception as e:
                logger.error(f"轮询异常: {e}")
            time.sleep(self.polling_interval)

    def stop(self) -> None:
        self._running = False
        logger.info("Telegram 交互 Bot 已停止")
