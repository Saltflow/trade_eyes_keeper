#!/usr/bin/env python3
"""
健康检查服务器核心类
"""

import threading
import socketserver
import logging
import os
import time
import sys
from datetime import datetime
from pathlib import Path
import subprocess
import urllib.request
import platform
import socket
import re

from ..handlers.health_handler import HealthHandler

logger = logging.getLogger(__name__)


class HealthServer:
    """健康检查服务器"""

    def __init__(self, config, host=None, port=None):
        self.config = config
        self.start_time = time.time()

        self.health_config = config.get("health_server", {})
        self.host = host or self.health_config.get("host", "0.0.0.0")
        self.port = port or self.health_config.get("port", 1933)
        self.server = None
        self.thread = None
        self._watchdog_thread = None
        self._watchdog_failures = 0

    def start(self, daemon=True):
        """启动健康服务器"""
        try:
            def handler_factory(*args, **kwargs):
                return HealthHandler(*args, health_server=self, **kwargs)

            # ThreadingTCPServer: 每个请求独立线程，坏连接不阻塞其他请求
            self.server = socketserver.ThreadingTCPServer(
                (self.host, self.port), handler_factory
            )
            self.server.timeout = 30  # 30s 无数据则断开卡住的连接

            # SSL/TLS: 如果存在证书文件则启用 HTTPS (路径从 config.yaml 读取)
            cert_path = self.health_config.get("cert_file", "cert.pem")
            key_path = self.health_config.get("key_file", "key.pem")
            cert_file = Path(cert_path)
            key_file = Path(key_path)
            if cert_file.exists() and key_file.exists():
                import ssl
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.load_cert_chain(str(cert_file), str(key_file))
                self.server.socket = ctx.wrap_socket(
                    self.server.socket, server_side=True,
                )
                logger.info("SSL 已启用 (自签名证书)")
            else:
                logger.info("HTTP 模式 (无 cert.pem/key.pem)")

            # 启动服务器线程
            self.thread = threading.Thread(target=self.server.serve_forever)
            self.thread.daemon = daemon

            logger.info(f"健康服务器启动在 {self.host}:{self.port}")
            logger.info(f"  访问 http://{self.host}:{self.port} 查看系统状态")
            logger.info(f"  测试邮件端点: http://{self.host}:{self.port}/test-email")

            self.thread.start()

            # Watchdog: 每 60s 自检 /health，连续 3 次失败则 exit
            self._watchdog_thread = threading.Thread(
                target=self._watchdog_loop, daemon=True
            )
            self._watchdog_thread.start()

            # 启动内嵌调度器（替代外部 crontab）
            try:
                from src.core.schedule_manager import ScheduleManager
                from .global_instances import set_schedule_manager

                config_path = Path("config/config.yaml")
                mgr = ScheduleManager(self.config, config_path=config_path)
                mgr.start()
                set_schedule_manager(mgr)
                logger.info("内嵌调度器已启动（替代 crontab）")
            except Exception as e:
                logger.error(f"内嵌调度器启动失败: {e}")

            return True

        except Exception as e:
            logger.error(f"启动健康服务器失败: {e}")
            return False

    def stop(self):
        """停止健康服务器"""
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            logger.info("健康服务器已停止")

    def _watchdog_loop(self):
        """自检循环：每 60s 检查 /health，连续 3 次失败则 exit。"""
        interval = 60
        max_failures = 3
        while True:
            time.sleep(interval)
            self._watchdog_step(max_failures)

    def _watchdog_step(self, max_failures=3):
        try:
            import urllib.request as ur
            url = f"http://127.0.0.1:{self.port}/health"
            req = ur.Request(url, headers={"User-Agent": "Watchdog/1.0"})
            resp = ur.urlopen(req, timeout=5)
            if resp.status == 200:
                self._watchdog_failures = 0
                return
        except Exception:
            pass
        self._watchdog_failures += 1
        logger.warning(
            f"Watchdog: /health 自检失败 ({self._watchdog_failures}/{max_failures})"
        )
        if self._watchdog_failures >= max_failures:
            logger.critical(
                f"Watchdog: 连续 {max_failures} 次自检失败，强制退出"
            )
            os._exit(1)

    def get_status(self):
        """获取系统状态信息"""
        try:
            # 获取服务器信息
            server_info = self._get_server_info()
            hostname = server_info["hostname"]
            ip_address = server_info["ip_address"]
            kernel_version = server_info["kernel_version"]
            system = server_info["system"]
            machine = server_info["machine"]

            # 计算运行时间
            uptime_seconds = time.time() - self.start_time
            uptime_str = self._format_uptime(uptime_seconds)

            # 获取目录大小
            project_root = Path(__file__).parent.parent.parent
            cache_dir = project_root / "cache"
            data_dir = project_root / "data"
            log_dir = project_root / "logs"

            cache_size = self._get_directory_size(cache_dir)
            data_size = self._get_directory_size(data_dir)
            log_size = self._get_directory_size(log_dir)

            # 获取磁盘使用率
            disk_usage = self._get_disk_usage()

            # 获取内存使用率
            memory_usage = self._get_memory_usage()

            # 从配置获取股票信息
            stock_count = len(self.config.get("stocks", []))
            monitored_stocks = self.config.get("stocks", [])

            # 计算下次运行时间
            next_run_time = self._calculate_next_run_time()

            return {
                "hostname": hostname,
                "ip_address": ip_address,
                "kernel_version": kernel_version,
                "system": system,
                "machine": machine,
                "python_version": sys.version.split()[0],
                "start_time": datetime.fromtimestamp(self.start_time).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "uptime": uptime_str,
                "uptime_seconds": int(uptime_seconds),
                "cache_size": cache_size,
                "cache_size_bytes": self._get_directory_size_bytes(cache_dir),
                "data_size": data_size,
                "data_size_bytes": self._get_directory_size_bytes(data_dir),
                "log_size": log_size,
                "log_size_bytes": self._get_directory_size_bytes(log_dir),
                "disk_usage": disk_usage,
                "memory_usage": memory_usage,
                "stock_count": stock_count,
                "monitored_stocks": monitored_stocks,
                "last_run_time": self._get_last_run_time(),
                "last_run_timestamp": self._get_last_run_timestamp(),
                "next_run_time": next_run_time,
                "server_url": f"http://{self.host}:{self.port}",
            }

        except Exception as e:
            logger.error(f"获取系统状态失败: {e}")
            return {
                "hostname": "未知",
                "ip_address": "未知",
                "kernel_version": "未知",
                "system": "未知",
                "machine": "未知",
                "python_version": sys.version.split()[0],
                "start_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "uptime": "未知",
                "uptime_seconds": 0,
                "cache_size": "未知",
                "cache_size_bytes": 0,
                "data_size": "未知",
                "data_size_bytes": 0,
                "log_size": "未知",
                "log_size_bytes": 0,
                "disk_usage": "未知",
                "memory_usage": "未知",
                "stock_count": 0,
                "monitored_stocks": [],
                "last_run_time": "未知",
                "last_run_timestamp": 0,
                "next_run_time": "未知",
                "server_url": f"http://{self.host}:{self.port}",
            }

    def _get_server_info(self):
        """
        获取服务器信息（IP地址和内核版本）

        Returns:
            dict: 包含服务器信息的字典
        """
        try:
            # 获取主机名和IP地址
            hostname = socket.gethostname()
            ip_list = []

            # 方法1: 通过socket.gethostbyname_ex获取所有IP
            try:
                _, _, ip_addresses = socket.gethostbyname_ex(hostname)
                ip_list.extend(ip_addresses)
            except Exception as e:
                logger.debug(f"gethostbyname_ex 取IP失败: {e}")

            # 方法2: 通过hostname -I命令获取所有IP（Linux）
            try:
                result = subprocess.run(
                    ["hostname", "-I"], capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    ips = result.stdout.strip().split()
                    ip_list.extend(ips)
            except Exception as e:
                logger.debug(f"hostname -I 取IP失败: {e}")

            # 方法3: 获取公网IP（可选）- 使用HTTPS防止中间人攻击
            ip_detect_url = self.health_config.get(
                "ip_detect_url", "https://ifconfig.me"
            )
            try:
                public_ip = (
                    urllib.request.urlopen(ip_detect_url, timeout=10)
                    .read()
                    .decode("utf-8")
                    .strip()
                )
                # 简单验证IP格式（基本防护）
                ip_pattern = r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$"
                if (
                    public_ip
                    and re.match(ip_pattern, public_ip)
                    and public_ip not in ip_list
                ):
                    ip_list.append(f"{public_ip} (公网)")
                elif public_ip:
                    logger.warning(
                        f"IP检测服务返回非标准响应: {public_ip[:50]}..."
                    )
            except Exception as e:
                logger.debug(f"获取公网IP失败: {e}")
                pass

            # 去重并过滤回环地址
            ip_list = list(set(ip_list))
            ip_list = [ip for ip in ip_list if not ip.startswith("127.")]

            if ip_list:
                ip_address = ", ".join(ip_list)
            else:
                ip_address = "无法获取"

            # 获取内核版本（Linux系统）
            kernel_version = "未知"
            try:
                # 尝试通过platform模块获取
                kernel_version = platform.release()
                if not kernel_version or kernel_version == "":
                    # 尝试通过uname命令获取
                    result = subprocess.run(
                        ["uname", "-r"], capture_output=True, text=True, timeout=5
                    )
                    if result.returncode == 0:
                        kernel_version = result.stdout.strip()
            except Exception as e:
                logger.debug(f"uname -r 取内核版本失败: {e}")
                kernel_version = platform.uname().release

            return {
                "hostname": hostname,
                "ip_address": ip_address,
                "kernel_version": kernel_version,
                "system": platform.system(),
                "machine": platform.machine(),
            }
        except Exception as e:
            logger.warning(f"获取服务器信息失败: {e}")
            return {
                "hostname": "未知",
                "ip_address": "无法获取",
                "kernel_version": "未知",
                "system": "未知",
                "machine": "未知",
            }

    def _format_uptime(self, seconds):
        """格式化运行时间"""
        days = int(seconds // (24 * 3600))
        seconds %= 24 * 3600
        hours = int(seconds // 3600)
        seconds %= 3600
        minutes = int(seconds // 60)
        seconds = int(seconds % 60)

        if days > 0:
            return f"{days}天{hours}小时{minutes}分钟"
        elif hours > 0:
            return f"{hours}小时{minutes}分钟{seconds}秒"
        else:
            return f"{minutes}分钟{seconds}秒"

    def _get_directory_size(self, directory):
        """获取目录大小（人类可读格式）"""
        try:
            if not directory.exists():
                return "0 B"

            total_size = 0
            for path in directory.rglob("*"):
                if path.is_file():
                    total_size += path.stat().st_size

            # 格式化大小
            for unit in ["B", "KB", "MB", "GB"]:
                if total_size < 1024.0:
                    return f"{total_size:.1f} {unit}"
                total_size /= 1024.0
            return f"{total_size:.1f} TB"
        except Exception as e:
            logger.debug(f"获取目录大小失败 {directory}: {e}")
            return "未知"

    def _get_directory_size_bytes(self, directory):
        """获取目录大小（字节）"""
        try:
            if not directory.exists():
                return 0

            total_size = 0
            for path in directory.rglob("*"):
                if path.is_file():
                    total_size += path.stat().st_size
            return total_size
        except Exception as e:
            logger.debug(f"获取目录大小（字节）失败 {directory}: {e}")
            return 0

    def _get_disk_usage(self):
        """获取磁盘使用率"""
        try:
            import shutil

            usage = shutil.disk_usage(Path(__file__).parent.parent)
            percent = (usage.used / usage.total) * 100
            return f"{percent:.1f}% ({self._format_bytes(usage.used)} / {self._format_bytes(usage.total)})"
        except Exception as e:
            logger.debug(f"获取磁盘使用率失败: {e}")
            return "未知"

    def _get_memory_usage(self):
        """获取内存使用率"""
        try:
            if platform.system() == "Linux":
                with open("/proc/meminfo", "r") as f:
                    lines = f.readlines()
                meminfo = {}
                for line in lines:
                    parts = line.split(":")
                    if len(parts) == 2:
                        meminfo[parts[0].strip()] = int(parts[1].strip().split()[0])

                total = meminfo.get("MemTotal", 0)
                free = meminfo.get("MemFree", 0)
                buffers = meminfo.get("Buffers", 0)
                cached = meminfo.get("Cached", 0)

                if total > 0:
                    used = total - free - buffers - cached
                    percent = (used / total) * 100
                    return f"{percent:.1f}%"

            return "未知"
        except Exception as e:
            logger.debug(f"获取内存使用率失败: {e}")
            return "未知"

    def _get_last_run_time(self):
        """获取上次运行时间"""
        try:
            log_file = Path(__file__).parent.parent / "logs" / "quant_system.log"
            if log_file.exists():
                with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()
                    for line in reversed(lines[-100:]):  # 检查最后100行
                        if "开始执行每日任务" in line:
                            # 提取时间戳
                            parts = line.split(" - ")
                            if len(parts) >= 1:
                                return parts[0]
            return "未知"
        except Exception as e:
            logger.debug(f"获取上次运行时间失败: {e}")
            return "未知"

    def _get_last_run_timestamp(self):
        """获取上次运行时间戳（秒）"""
        last_run = self._get_last_run_time()
        if last_run == "未知":
            return 0

        try:
            # 尝试解析时间戳格式 "2026-03-14 15:30:01"
            dt = datetime.strptime(last_run, "%Y-%m-%d %H:%M:%S")
            return int(dt.timestamp())
        except ValueError:
            return 0

    def _calculate_next_run_time(self):
        """计算下次运行时间"""
        try:
            # 从配置获取调度时间
            scheduler_config = self.config.get("scheduler", {})
            run_time = scheduler_config.get("run_time", "15:30")
            timezone = scheduler_config.get("timezone", "Asia/Shanghai")

            # 解析时间
            hour, minute = map(int, run_time.split(":"))

            # 获取当前时间（上海时区）
            import pytz
            from datetime import timedelta

            tz = pytz.timezone(timezone)
            now = datetime.now(tz)

            # 计算今天的运行时间
            today_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

            # 如果今天已经过了运行时间，计算明天的
            if now > today_run:
                next_run = today_run + timedelta(days=1)
            else:
                next_run = today_run

            return next_run.strftime("%Y-%m-%d %H:%M:%S")
        except Exception as e:
            logger.debug(f"计算下次运行时间失败: {e}")
            return "未知"

    def _format_bytes(self, bytes_num):
        """格式化字节数为人类可读格式"""
        for unit in ["B", "KB", "MB", "GB"]:
            if bytes_num < 1024.0:
                return f"{bytes_num:.1f} {unit}"
            bytes_num /= 1024.0
        return f"{bytes_num:.1f} TB"
