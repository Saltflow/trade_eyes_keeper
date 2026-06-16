#!/usr/bin/env python3
"""
HTTP请求处理器 - 处理健康服务器的所有HTTP请求
使用模板系统生成HTML页面，集成OTP认证和会话管理
"""

import http.server
import logging
from pathlib import Path
import html
from urllib.parse import parse_qs, urlparse
import time
from datetime import datetime
import re
import sys
import json

from ..core.global_instances import (
    rate_limiter,
    otp_rate_limiter,
    otp_manager,
    session_manager,
    audit_log,
)

logger = logging.getLogger(__name__)


class HealthHandler(http.server.BaseHTTPRequestHandler):
    """HTTP请求处理器（使用模板）"""

    def __init__(self, *args, health_server=None, **kwargs):
        self.health_server = health_server
        self._template_cache = {}  # 模板缓存，减少文件IO
        logger.info(f"HealthHandler initialized, health_server={health_server}")
        super().__init__(*args, **kwargs)

    def _load_template(self, name):
        """
        加载模板文件，使用缓存提高性能

        Args:
            name: 模板文件名（如 'health.html'）

        Returns:
            str: 模板内容

        Raises:
            FileNotFoundError: 模板文件不存在
            IOError: 模板文件读取失败
        """
        # 检查缓存
        if name in self._template_cache:
            return self._template_cache[name]

        template_path = (
            Path(__file__).parent.parent.parent / "templates" / "health_server" / name
        )
        if not template_path.exists():
            logger.error(f"模板文件不存在: {template_path}")
            raise FileNotFoundError(f"模板文件不存在: {template_path}")

        try:
            with open(template_path, "r", encoding="utf-8") as f:
                content = f.read()
                # 缓存模板，有效期5分钟（300秒）
                self._template_cache[name] = content
                return content
        except Exception as e:
            logger.error(f"加载模板失败 {name}: {e}")
            raise IOError(f"加载模板失败 {name}: {e}")

    def _render_template(self, name, context):
        """
        渲染模板，验证必需变量

        Args:
            name: 模板文件名
            context: 字典，包含模板变量

        Returns:
            str: 渲染后的HTML

        Raises:
            ValueError: 模板渲染失败或必需变量缺失
        """
        try:
            template = self._load_template(name)

            # 验证必需变量（简单检查：模板中的变量是否在context中）
            import re

            required_vars = set(re.findall(r"\{(\w+)\}", template))
            missing_vars = required_vars - set(context.keys())
            if missing_vars:
                logger.warning(f"模板 {name} 缺失变量: {missing_vars}")
                # 不抛出异常，但记录警告，使用空字符串填充

            # 简单替换：{key} -> value
            for key, value in context.items():
                placeholder = "{" + key + "}"
                template = template.replace(placeholder, str(value))
            return template
        except Exception as e:
            logger.error(f"渲染模板失败 {name}: {e}")
            raise ValueError(f"渲染模板失败 {name}: {e}")

    def _send_html_response(
        self, status_code=200, content_type="text/html; charset=utf-8", content=""
    ):
        """发送HTML响应"""
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content.encode("utf-8"))))
        self.end_headers()
        self.wfile.write(content.encode("utf-8"))

    def _send_error_page(self, title, message, back_url="/"):
        """发送错误页面（使用模板），模板失败时回退到内置错误响应"""
        try:
            context = {
                "title": html.escape(title),
                "message": html.escape(message),
                "back_url": html.escape(back_url),
            }
            html_content = self._render_template("error.html", context)
            self._send_html_response(200, "text/html; charset=utf-8", html_content)
        except Exception as e:
            logger.error(f"渲染错误页面失败，回退到内置错误响应: {e}")
            # 回退到内置错误响应（避免硬编码HTML）
            self.send_error(500, f"Error: {title} - {message}")

    def _get_session_token(self):
        """从Cookie中提取会话令牌"""
        cookie_header = self.headers.get("Cookie", "")
        if not cookie_header:
            return None

        # 简单解析Cookie (格式: token=abc123; other=value)
        for cookie in cookie_header.split(";"):
            cookie = cookie.strip()
            if cookie.startswith("token="):
                return cookie[6:]  # 去掉'token='
        return None

    def _validate_session(self):
        """验证当前请求的会话，返回(是否有效, 错误消息, 令牌)"""
        client_ip = self.client_address[0]
        token = self._get_session_token()

        if not token:
            return False, "未登录", None

        is_valid, message = session_manager.validate(token, client_ip)
        return is_valid, message, token

    def _validate_stock_code(self, stock_code):
        """
        验证股票代码格式

        Args:
            stock_code: 股票代码字符串或整数

        Returns:
            tuple: (是否有效, 错误消息)
        """
        code_str = str(stock_code).strip()

        # 基本格式: 6位数字
        if not re.match(r"^\d{6}$", code_str):
            return False, "股票代码必须为6位数字"

        # 市场验证 (首位数)
        first_digit = code_str[0]
        valid_markets = {"6": "上海", "0": "深圳", "3": "创业板"}
        if first_digit not in valid_markets:
            logger.warning(f"非常规股票代码首位: {first_digit} (代码: {code_str})")
            # 不拒绝，只记录警告

        return True, "格式正确"

    def _send_otp_email(self, client_ip, otp_code):
        """发送OTP邮件（使用模板）"""
        try:
            from src.notification.email_notifier import EmailNotifier

            if not self.health_server or not self.health_server.config:
                logger.error("无法获取系统配置发送OTP邮件")
                return False

            config = self.health_server.config
            receiver_email = config.get("email", {}).get("receiver_email")
            if not receiver_email:
                logger.error("未配置接收邮箱，无法发送OTP邮件")
                return False

            # 使用模板
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            context = {
                "otp_code": otp_code,
                "current_time": current_time,
                "client_ip": client_ip,
                "receiver_email": receiver_email,
                "host": self.headers.get("Host", "localhost"),
            }

            body = self._load_template("otp_email.html").format(**context)

            subject = "股票量化系统管理验证码 (10分钟内有效)"
            notifier = EmailNotifier(config)
            notifier._send_email(subject, body)

            logger.info(f"OTP邮件已发送到 {receiver_email}, 验证码: {otp_code}")
            return True

        except Exception as e:
            logger.error(f"发送OTP邮件失败: {e}")
            return False

    def _send_watchlist_change_email(
        self, client_ip, action, stock_code=None, details=None
    ):
        """发送监控列表变更确认邮件（使用模板）"""
        try:
            from src.notification.email_notifier import EmailNotifier

            if not self.health_server or not self.health_server.config:
                logger.error("无法获取系统配置发送确认邮件")
                return False

            config = self.health_server.config
            receiver_email = config.get("email", {}).get("receiver_email")
            if not receiver_email:
                logger.error("未配置接收邮箱，无法发送确认邮件")
                return False

            # 获取当前股票列表
            current_stocks = config.get("stocks", [])
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # 构建邮件内容
            if action == "添加":
                subject = f"股票监控列表已更新 - 添加股票 {stock_code}"
                action_desc = f"添加了股票 {stock_code}"
            elif action == "移除":
                subject = f"股票监控列表已更新 - 移除股票 {stock_code}"
                action_desc = f"移除了股票 {stock_code}"
            elif action == "清空":
                subject = "股票监控列表已清空"
                action_desc = details or "清空了所有股票"
            else:
                subject = "股票监控列表已更新"
                action_desc = action

            # 使用模板
            if current_stocks:
                stocks_html = (
                    "<ul>"
                    + "".join(f"<li>{stock}</li>" for stock in current_stocks)
                    + "</ul>"
                )
            else:
                stocks_html = "<p>无</p>"

            next_run_time = (
                self.health_server._calculate_next_run_time()
                if hasattr(self.health_server, "_calculate_next_run_time")
                else "未知"
            )

            context = {
                "action_desc": action_desc,
                "current_time": current_time,
                "client_ip": client_ip,
                "stock_count": len(current_stocks),
                "stocks_html": stocks_html,
                "next_run_time": next_run_time,
            }

            body = self._load_template("watchlist_email.html").format(**context)

            # 发送邮件
            notifier = EmailNotifier(config)
            notifier._send_email(subject, body)

            logger.info(
                f"监控列表变更确认邮件已发送到 {receiver_email}, 操作: {action_desc}"
            )
            return True

        except Exception as e:
            logger.error(f"发送监控列表变更确认邮件失败: {e}")
            return False

    def _save_config(self, config):
        """保存配置到文件"""
        try:
            import yaml

            config_path = Path(__file__).parent.parent.parent.parent / "config" / "config.yaml"

            # 创建备份
            backup_path = config_path.with_suffix(".yaml.backup")
            import shutil

            shutil.copy2(config_path, backup_path)

            # 写入新配置
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(
                    config,
                    f,
                    allow_unicode=True,
                    default_flow_style=False,
                    sort_keys=False,
                )

            logger.info(f"配置文件已更新: {config_path}")
            return True
        except Exception as e:
            logger.error(f"保存配置文件失败: {e}")
            return False

    def do_GET(self):
        """处理GET请求"""
        logger.info(
            f"do_GET called: command={self.command}, path={self.path}, client={self.client_address}"
        )
        # 检查速率限制
        client_ip = self.client_address[0]
        if not rate_limiter.is_allowed(client_ip):
            self.send_response(429)  # Too Many Requests
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                f"请求频率过高 (限制: 1 QPS). 客户端IP: {client_ip}".encode("utf-8")
            )
            logger.warning(f"请求频率过高被拒绝: {client_ip} - {self.path}")
            return

        logger.info(f"GET request: {self.path}")
        try:
            if self.path == "/":
                self.send_html_response()
            elif self.path == "/status":
                self.send_json_response()
            elif self.path == "/health":
                self.send_health_response()
            elif self.path == "/test-email":
                self.send_test_email_response()
            elif self.path == "/metrics":
                self.send_metrics_response()
            elif self.path == "/request-otp":
                self.handle_otp_request()
            elif self.path.startswith("/verify-otp"):
                self.handle_otp_verification()
            elif self.path == "/manage":
                self.handle_management_page()
            elif self.path == "/logout":
                self.handle_logout()
            elif self.path.startswith("/report/"):
                self.handle_report()
            else:
                self.send_error(404, "Not Found")
        except Exception as e:
            logger.error(f"处理请求 {self.path} 时出错: {e}")
            self.send_error(500, f"Internal Server Error: {e}")

    def do_POST(self):
        """处理POST请求"""
        # 检查速率限制
        client_ip = self.client_address[0]
        if not rate_limiter.is_allowed(client_ip):
            self.send_response(429)  # Too Many Requests
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                f"请求频率过高 (限制: 1 QPS). 客户端IP: {client_ip}".encode("utf-8")
            )
            logger.warning(f"POST请求频率过高被拒绝: {client_ip} - {self.path}")
            return

        try:
            if self.path == "/update-watchlist":
                self.handle_watchlist_update()
            elif self.path == "/feishu/events":
                self._handle_feishu_event()
            else:
                self.send_error(404, "Not Found")
        except Exception as e:
            logger.error(f"处理POST请求 {self.path} 时出错: {e}")
            self.send_error(500, f"Internal Server Error: {e}")

    def handle_otp_request(self):
        """处理OTP请求"""
        client_ip = self.client_address[0]

        # 记录审计日志
        audit_log("OTP_REQUEST", client_ip, "请求验证码")

        # 检查OTP速率限制
        if not otp_rate_limiter.is_allowed(client_ip):
            self._send_otp_request_page(
                error_message="请求过于频繁，请5分钟后再试", remaining_time=300
            )
            return

        # 生成OTP
        otp_code = otp_manager.generate(client_ip)
        if otp_code is None:
            # 已有未过期的OTP，显示验证页面
            self._send_otp_verification_page(
                error_message="您已有一个未过期的验证码，请检查邮箱并输入验证码"
            )
            return

        # 发送OTP邮件
        email_sent = self._send_otp_email(client_ip, otp_code)

        if email_sent:
            otp_manager.mark_email_sent(client_ip)
            audit_log("OTP_REQUEST_SUCCESS", client_ip, "验证码已发送")
            # 重定向到验证页面
            self.send_response(302)
            self.send_header("Location", "/verify-otp")
            self.end_headers()
        else:
            audit_log("OTP_REQUEST_FAILED", client_ip, "邮件发送失败")
            self._send_otp_request_page(
                error_message="邮件发送失败，请检查邮箱配置或稍后重试"
            )

    def handle_otp_verification(self):
        """处理OTP验证"""
        client_ip = self.client_address[0]

        # 解析查询参数
        parsed = urlparse(self.path)
        query_params = parse_qs(parsed.query)
        otp_code = query_params.get("code", [""])[0].strip()

        if not otp_code:
            # 显示验证页面
            self._send_otp_verification_page()
            return

        # 验证OTP
        is_valid, message = otp_manager.validate(client_ip, otp_code)

        if is_valid:
            # 创建会话
            token = session_manager.create(client_ip)

            # 设置HTTP-only Cookie
            cookie = f"token={token}; Path=/; HttpOnly; Max-Age=1800"  # 30分钟

            audit_log("OTP_VERIFY_SUCCESS", client_ip, f"创建会话: {token[:8]}...")

            # 重定向到管理页面
            self.send_response(302)
            self.send_header("Location", "/manage")
            self.send_header("Set-Cookie", cookie)
            self.end_headers()
        else:
            audit_log("OTP_VERIFY_FAILED", client_ip, f"验证失败: {message}")
            self._send_otp_verification_page(error_message=message, code_value=otp_code)

    def handle_management_page(self):
        """处理管理页面请求"""
        # 验证会话
        is_valid, message, token = self._validate_session()
        if not is_valid:
            # 重定向到OTP请求页面
            self.send_response(302)
            self.send_header("Location", "/request-otp")
            self.end_headers()
            return

        self._send_management_page()

    def handle_logout(self):
        """处理退出登录"""
        client_ip = self.client_address[0]
        token = self._get_session_token()

        if token:
            session_manager.invalidate(token)
            audit_log("LOGOUT", client_ip, f"会话失效: {token[:8]}...")

        # 清除Cookie并重定向到首页
        self.send_response(302)
        self.send_header("Location", "/")
        self.send_header(
            "Set-Cookie",
            "token=; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT; HttpOnly",
        )
        self.end_headers()

    def handle_report(self):
        """处理策略报告请求: GET /report/<token>"""
        from ..core.global_instances import get_report_path

        # 从 URL 提取 token
        token = self.path.split("/report/", 1)[-1].strip()
        # 纯十六进制校验: 只允许 [0-9a-f]
        if not token or not all(c in "0123456789abcdef" for c in token):
            self.send_error(400, "Bad Request: invalid token format")
            return

        path = get_report_path(token)
        if not path or not path.exists():
            self.send_error(404, "Not Found or Expired")
            return

        try:
            content = path.read_text(encoding="utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content.encode("utf-8"))))
            # 安全头
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content.encode("utf-8"))
            logger.info("报告已发送: token=%s", token[:6])
        except Exception as e:
            logger.error("发送报告失败: %s", e)
            self.send_error(500, "Internal Server Error")

    def handle_watchlist_update(self):
        """处理监控列表更新"""
        client_ip = self.client_address[0]

        # 验证会话
        is_valid, message, token = self._validate_session()
        if not is_valid:
            self.send_response(302)
            self.send_header("Location", "/request-otp")
            self.end_headers()
            return

        # 读取POST数据
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > 0:
            post_data = self.rfile.read(content_length).decode("utf-8")
            post_params = parse_qs(post_data)
        else:
            post_params = {}

        action = post_params.get("action", [""])[0]
        stock_code = post_params.get("stock_code", [""])[0].strip()

        # 根据action处理
        if action == "add":
            self._handle_add_stock(client_ip, stock_code, token)
        elif action == "remove":
            self._handle_remove_stock(client_ip, stock_code, token)
        elif action == "clear":
            self._handle_clear_stocks(client_ip, token)
        else:
            self._send_management_page(error_message="无效的操作类型")

    def _handle_add_stock(self, client_ip, stock_code, token):
        """处理添加股票"""
        # 验证股票代码
        is_valid, message = self._validate_stock_code(stock_code)
        if not is_valid:
            self._send_management_page(error_message=f"股票代码无效: {message}")
            return

        # 获取当前配置
        if not self.health_server or not self.health_server.config:
            self._send_management_page(error_message="无法读取系统配置")
            return

        config = self.health_server.config
        current_stocks = config.get("stocks", [])

        # 检查是否已存在
        if stock_code in current_stocks:
            self._send_management_page(
                error_message=f"股票 {stock_code} 已在监控列表中"
            )
            return

        # 添加到列表
        current_stocks.append(stock_code)
        config["stocks"] = current_stocks

        # 保存配置
        success = self._save_config(config)
        if success:
            audit_log("WATCHLIST_ADD", client_ip, f"添加股票: {stock_code}")

            # 重新加载健康服务器配置
            self.health_server.config = config

            # 发送确认邮件
            self._send_watchlist_change_email(client_ip, "添加", stock_code)

            self._send_management_page(
                success_message=f"成功添加股票 {stock_code}，已发送确认邮件"
            )
        else:
            self._send_management_page(error_message="保存配置失败，请检查文件权限")

    def _handle_remove_stock(self, client_ip, stock_code, token):
        """处理移除股票"""
        # 获取当前配置
        if not self.health_server or not self.health_server.config:
            self._send_management_page(error_message="无法读取系统配置")
            return

        config = self.health_server.config
        current_stocks = config.get("stocks", [])

        # 检查是否存在
        if stock_code not in current_stocks:
            self._send_management_page(
                error_message=f"股票 {stock_code} 不在监控列表中"
            )
            return

        # 从列表中移除
        current_stocks.remove(stock_code)
        config["stocks"] = current_stocks

        # 保存配置
        success = self._save_config(config)
        if success:
            audit_log("WATCHLIST_REMOVE", client_ip, f"移除股票: {stock_code}")

            # 重新加载健康服务器配置
            self.health_server.config = config

            # 发送确认邮件
            self._send_watchlist_change_email(client_ip, "移除", stock_code)

            self._send_management_page(
                success_message=f"成功移除股票 {stock_code}，已发送确认邮件"
            )
        else:
            self._send_management_page(error_message="保存配置失败，请检查文件权限")

    def _handle_clear_stocks(self, client_ip, token):
        """处理清空所有股票"""
        # 获取当前配置
        if not self.health_server or not self.health_server.config:
            self._send_management_page(error_message="无法读取系统配置")
            return

        config = self.health_server.config
        old_stocks = config.get("stocks", [])

        # 清空列表
        config["stocks"] = []

        # 保存配置
        success = self._save_config(config)
        if success:
            audit_log(
                "WATCHLIST_CLEAR", client_ip, f"清空所有股票，原数量: {len(old_stocks)}"
            )

            # 重新加载健康服务器配置
            self.health_server.config = config

            # 发送确认邮件
            self._send_watchlist_change_email(
                client_ip, "清空", details=f"清空了 {len(old_stocks)} 只股票"
            )

            self._send_management_page(
                success_message=f"成功清空所有股票（共 {len(old_stocks)} 只），已发送确认邮件"
            )
        else:
            self._send_management_page(error_message="保存配置失败，请检查文件权限")

    def send_html_response(self):
        """发送HTML格式的状态页面（使用模板）"""
        status = self.health_server.get_status() if self.health_server else {}

        # 辅助函数：安全获取并转义HTML
        def safe_get(key, default="未知"):
            value = status.get(key, default)
            if value is None:
                value = default
            return html.escape(str(value))

        # 加载并渲染模板
        template_content = self._load_template("health.html")
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        client_ip = html.escape(str(self.client_address[0]))
        rate_limit_stats = self._get_rate_limit_stats()

        html_content = template_content.format(
            current_time=current_time,
            client_ip=client_ip,
            rate_limit_stats=rate_limit_stats,
            hostname=safe_get("hostname", "未知"),
            ip_address=safe_get("ip_address", "未知"),
            kernel_version=safe_get("kernel_version", "未知"),
            system=safe_get("system", "未知"),
            machine=safe_get("machine", "未知"),
            python_version=safe_get("python_version", sys.version.split()[0]),
            start_time=safe_get(
                "start_time", datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ),
            uptime=safe_get("uptime", "0秒"),
            cache_size=safe_get("cache_size", "未知"),
            data_size=safe_get("data_size", "未知"),
            log_size=safe_get("log_size", "未知"),
            disk_usage=safe_get("disk_usage", "未知"),
            memory_usage=safe_get("memory_usage", "未知"),
            stock_count=safe_get("stock_count", "未知"),
            last_run_time=safe_get("last_run_time", "未知"),
            next_run_time=safe_get("next_run_time", "未知"),
        )
        self._send_html_response(200, "text/html; charset=utf-8", html_content)

    def send_json_response(self):
        """发送JSON格式的状态信息"""
        status = self.health_server.get_status() if self.health_server else {}
        json_data = json.dumps(status, ensure_ascii=False, indent=2)

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(json_data.encode("utf-8"))))
        self.end_headers()
        self.wfile.write(json_data.encode("utf-8"))

    def send_health_response(self):
        """健康检查端点 (返回200 OK)"""
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK")

    def send_test_email_response(self):
        """发送测试邮件"""
        try:
            from src.notification.email_notifier import EmailNotifier

            if not self.health_server or not self.health_server.config:
                self._send_error_page("配置错误", "无法获取系统配置", "/")
                return

            config = self.health_server.config
            receiver_email = config.get("email", {}).get("receiver_email")
            if not receiver_email:
                self._send_error_page("配置错误", "未配置接收邮箱", "/")
                return

            # 检查是否强制发送
            parsed = urlparse(self.path)
            query_params = parse_qs(parsed.query)
            force = query_params.get("force", [""])[0].lower() == "true"

            if not force:
                # 显示确认页面
                context = {"receiver_email": html.escape(receiver_email)}
                html_content = self._render_template("test_email_confirm.html", context)
                self._send_html_response(200, "text/html; charset=utf-8", html_content)
                return

            # 发送测试邮件
            notifier = EmailNotifier(config)
            success = notifier.send_test_email()

            if success:
                context = {"receiver_email": html.escape(receiver_email)}
                html_content = self._render_template("test_email_success.html", context)
            else:
                context = {"receiver_email": html.escape(receiver_email)}
                html_content = self._render_template("test_email_fail.html", context)

            self._send_html_response(200, "text/html; charset=utf-8", html_content)

        except Exception as e:
            logger.error(f"发送测试邮件响应失败: {e}")
            self._send_error_page("内部错误", f"发送测试邮件失败: {e}", "/")

    def send_metrics_response(self):
        """发送系统指标 (Prometheus格式)"""
        try:
            import psutil

            metrics = []

            # 系统指标
            metrics.append("# HELP system_uptime_seconds System uptime in seconds")
            metrics.append("# TYPE system_uptime_seconds gauge")
            metrics.append(
                f"system_uptime_seconds {time.time() - self.health_server.start_time}"
            )

            # 内存使用
            memory = psutil.virtual_memory()
            metrics.append(
                "# HELP system_memory_usage_percent System memory usage percentage"
            )
            metrics.append("# TYPE system_memory_usage_percent gauge")
            metrics.append(f"system_memory_usage_percent {memory.percent}")

            # 磁盘使用
            disk = psutil.disk_usage("/")
            metrics.append(
                "# HELP system_disk_usage_percent System disk usage percentage"
            )
            metrics.append("# TYPE system_disk_usage_percent gauge")
            metrics.append(f"system_disk_usage_percent {disk.percent}")

            # 股票数量
            stock_count = (
                len(self.health_server.config.get("stocks", []))
                if self.health_server and self.health_server.config
                else 0
            )
            metrics.append("# HELP system_monitored_stocks Number of monitored stocks")
            metrics.append("# TYPE system_monitored_stocks gauge")
            metrics.append(f"system_monitored_stocks {stock_count}")

            metrics_text = "\n".join(metrics)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(metrics_text.encode("utf-8"))
        except Exception as e:
            logger.error(f"生成指标失败: {e}")
            self.send_response(500)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"Error: {e}".encode("utf-8"))

    def _send_otp_request_page(self, error_message=None, remaining_time=None):
        """发送OTP请求页面（使用模板）"""
        client_ip = self.client_address[0]

        # 检查是否已有未过期的OTP
        remaining = otp_manager.get_remaining_time(client_ip)
        can_request = remaining <= 0  # 没有有效OTP或已过期

        # 检查OTP速率限制
        otp_allowed = otp_rate_limiter.is_allowed(client_ip)

        # 获取配置中的接收邮箱
        receiver_email = "未知"
        try:
            if self.health_server and self.health_server.config:
                receiver_email = self.health_server.config.get("email", {}).get(
                    "receiver_email", "未知"
                )
        except Exception as e:
            logger.debug(f"读取收件邮箱配置失败: {e}")

        # 计算下次可请求时间
        next_request_time = "现在可以请求"
        if not otp_allowed:
            next_request_time = "5分钟后"

        # 准备模板上下文
        context = {
            "receiver_email": html.escape(receiver_email),
            "client_ip": html.escape(client_ip),
            "status_text": "可以请求验证码"
            if can_request and otp_allowed
            else "暂时无法请求",
            "next_request_time": html.escape(next_request_time),
            "button_disabled": "disabled" if not (can_request and otp_allowed) else "",
            "button_text": "发送验证码到邮箱"
            if can_request and otp_allowed
            else "暂时无法发送",
        }

        # 错误消息HTML
        if error_message:
            context["error_html"] = f"""
                <div class="error-box">
                    <h3>⚠️ 错误</h3>
                    <p>{html.escape(error_message)}</p>
                </div>
            """
        else:
            context["error_html"] = ""

        # 剩余时间HTML
        if remaining > 0:
            context["remaining_html"] = f"""
                <div class="info-box">
                    <h3>已有未过期验证码</h3>
                    <p>您已有一个有效验证码，剩余时间: {remaining}秒</p>
                    <p><a href="/verify-otp">点击这里输入验证码</a></p>
                </div>
            """
        else:
            context["remaining_html"] = ""

        # 渲染模板
        try:
            html_content = self._render_template("otp_request.html", context)
            self._send_html_response(200, "text/html; charset=utf-8", html_content)
        except Exception as e:
            logger.error(f"渲染OTP请求页面失败，回退到内置错误响应: {e}")
            self.send_error(500, "无法加载OTP请求页面")

    def _send_otp_verification_page(self, error_message=None, code_value=""):
        """发送OTP验证页面（使用模板）"""
        client_ip = self.client_address[0]
        remaining = otp_manager.get_remaining_time(client_ip)

        context = {
            "client_ip": html.escape(client_ip),
            "remaining": remaining if remaining > 0 else 0,
            "code_value": html.escape(code_value),
        }

        if error_message:
            context["error_html"] = f"""
                <div class="error-box">
                    <h3>⚠️ 验证失败</h3>
                    <p>{html.escape(error_message)}</p>
                </div>
            """
        else:
            context["error_html"] = ""

        try:
            html_content = self._render_template("otp_verification.html", context)
            self._send_html_response(200, "text/html; charset=utf-8", html_content)
        except Exception as e:
            logger.error(f"渲染OTP验证页面失败，回退到内置错误响应: {e}")
            self.send_error(500, "无法加载OTP验证页面")

    def _send_management_page(self, error_message=None, success_message=None):
        """发送监控列表管理页面（使用模板）"""
        # 验证会话
        is_valid, message, token = self._validate_session()
        if not is_valid:
            self._send_error_page("会话无效", message, "/request-otp")
            return

        # 获取当前监控股票列表
        current_stocks = []
        try:
            if self.health_server and self.health_server.config:
                current_stocks = self.health_server.config.get("stocks", [])
        except Exception as e:
            logger.debug(f"读取股票列表配置失败: {e}")

        # 准备模板上下文
        context = {
            "client_ip": html.escape(self.client_address[0]),
            "stock_count": len(current_stocks),
            "stocks_html": "",
            "error_html": "",
            "success_html": "",
        }

        # 生成股票表格行
        if current_stocks:
            context["stocks_html"] = "".join(
                f'<tr><td>{stock}</td><td><form method="POST" action="/update-watchlist" style="display: inline;"><input type="hidden" name="action" value="remove"><input type="hidden" name="stock_code" value="{stock}"><button type="submit" class="button button-remove">移除</button></form></td></tr>'
                for stock in current_stocks
            )
        else:
            context["stocks_html"] = '<tr><td colspan="2">无</td></tr>'

        if error_message:
            context["error_html"] = f"""
                <div class="error-box">
                    <h3>⚠️ 错误</h3>
                    <p>{html.escape(error_message)}</p>
                </div>
            """

        if success_message:
            context["success_html"] = f"""
                <div class="success-box">
                    <h3>✅ 成功</h3>
                    <p>{html.escape(success_message)}</p>
                </div>
            """

        # 渲染模板
        try:
            html_content = self._render_template("management.html", context)
            self._send_html_response(200, "text/html; charset=utf-8", html_content)
        except Exception as e:
            logger.error(f"渲染管理页面失败，回退到内置错误响应: {e}")
            self.send_error(500, "无法加载管理页面")

    def _get_rate_limit_stats(self):
        """获取速率限制统计信息（HTML格式）"""
        stats = rate_limiter.get_stats()
        if not stats:
            return "暂无请求记录"

        lines = []
        for ip, data in stats.items():
            status = "🚫 已限制" if data["blocked"] else "✅ 正常"
            lines.append(f"{ip}: {data['recent_requests']}/60 请求 {status}")

        return "<br>".join(lines)

    def _handle_feishu_event(self):
        """处理飞书事件回调 POST /feishu/events。"""
        import json
        import yaml
        from pathlib import Path
        from src.interactive.feishu_app import FeishuApp
        from src.interactive.feishu_handler import handle_feishu_event

        config_path = Path("config/config.yaml")
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length)
        raw_text = raw_body.decode("utf-8")
        logger.info(f"飞书原始事件({len(raw_text)}B): {raw_text[:500]}")
        body = json.loads(raw_text)

        app = FeishuApp(config)
        status, resp = handle_feishu_event(app, dict(self.headers), body)

        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
