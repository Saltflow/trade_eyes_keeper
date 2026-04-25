#!/usr/bin/env python3
"""
健康服务器启动入口
"""

import os
import time
import logging
from pathlib import Path
import yaml
from dotenv import load_dotenv

from .health_server import HealthServer

logger = logging.getLogger(__name__)


def start_health_server(config_path=None):
    """启动健康服务器（独立运行）"""

    # 定位项目根目录（从 src/health_server/core/start_server.py 向上4级到达项目根）
    _script_dir = Path(__file__).resolve().parent
    _project_root = (
        _script_dir.parent.parent.parent
    )  # core/ -> health_server/ -> src/ -> project root

    # 加载环境变量
    env_path = _project_root / "config" / ".env"
    load_dotenv(dotenv_path=env_path)

    # 加载配置
    if config_path is None:
        config_path = _project_root / "config" / "config.yaml"

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # 用环境变量覆盖配置（与main.py保持一致）
    if os.getenv("EMAIL_SENDER"):
        config.setdefault("email", {})["sender_email"] = os.getenv("EMAIL_SENDER")
    if os.getenv("EMAIL_PASSWORD"):
        config.setdefault("email", {})["sender_password"] = os.getenv("EMAIL_PASSWORD")
    if os.getenv("EMAIL_RECEIVER"):
        config.setdefault("email", {})["receiver_email"] = os.getenv("EMAIL_RECEIVER")
    deepseek_key = os.getenv("DEEPSEEK_API_KEY")
    if deepseek_key and deepseek_key.strip():
        config.setdefault("llm", {})["api_key"] = deepseek_key.strip()
    if os.getenv("TUSHARE_TOKEN"):
        config.setdefault("data_source", {})["tushare_token"] = os.getenv(
            "TUSHARE_TOKEN"
        )

    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # 启动健康服务器
    health_server = HealthServer(config)

    try:
        health_server.start(daemon=False)
        print(f"健康服务器运行在 http://{health_server.host}:{health_server.port}")
        print("按 Ctrl+C 停止服务器")

        # 保持主线程运行
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        print("\n正在停止健康服务器...")
        health_server.stop()
        print("健康服务器已停止")


if __name__ == "__main__":
    start_health_server()
