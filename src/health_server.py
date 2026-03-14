#!/usr/bin/env python3
"""
健康检查服务器 - 在端口1933提供系统状态信息
"""
import http.server
import socketserver
import threading
import json
import os
import sys
import time
import socket
import platform
import subprocess
import urllib.request
import random
import secrets
import re
from datetime import datetime
from pathlib import Path
import logging
from typing import Dict, Any, Optional
import html
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)


class RateLimiter:
    """简单的请求速率限制器，支持可配置的时间窗口"""
    
    def __init__(self, requests_per_minute=60, window_seconds=60):
        """
        初始化速率限制器
        
        Args:
            requests_per_minute: 每分钟允许的最大请求数 (默认60 = 1 QPS)
            window_seconds: 时间窗口（秒），默认60秒（1分钟）
        """
        self.requests_per_minute = requests_per_minute
        self.window_seconds = window_seconds
        self.request_log = {}  # ip -> [timestamp1, timestamp2, ...]
        self.lock = threading.Lock()
        
    def is_allowed(self, ip_address):
        """
        检查指定IP的请求是否允许
        
        Args:
            ip_address: 客户端IP地址
            
        Returns:
            bool: 是否允许请求
        """
        with self.lock:
            now = time.time()
            
            # 清理过期记录（超过时间窗口）
            if ip_address in self.request_log:
                self.request_log[ip_address] = [
                    t for t in self.request_log[ip_address] 
                    if now - t < self.window_seconds
                ]
            
            # 检查是否超过限制
            current_count = len(self.request_log.get(ip_address, []))
            if current_count >= self.requests_per_minute:
                return False
            
            # 记录本次请求
            self.request_log.setdefault(ip_address, []).append(now)
            return True
    
    def get_stats(self):
        """获取速率限制统计信息"""
        with self.lock:
            now = time.time()
            stats = {}
            for ip, timestamps in self.request_log.items():
                # 只统计最近时间窗口内的请求
                recent_requests = [t for t in timestamps if now - t < self.window_seconds]
                stats[ip] = {
                    'recent_requests': len(recent_requests),
                    'blocked': len(recent_requests) >= self.requests_per_minute
                }
            return stats


class OTPManager:
    """OTP管理器 - 生成和验证5位数字验证码，10分钟有效期"""
    
    def __init__(self, expiry_minutes=10):
        """
        初始化OTP管理器
        
        Args:
            expiry_minutes: OTP有效期（分钟），默认10分钟
        """
        self.expiry_minutes = expiry_minutes
        self.otp_store = {}  # ip -> {'code': '12345', 'expiry': timestamp, 'email_sent': bool}
        self.lock = threading.Lock()
        self.cleanup_interval = 300  # 每5分钟清理一次过期OTP
        self.last_cleanup = time.time()
    
    def generate(self, ip_address):
        """
        为指定IP生成新的OTP
        
        Args:
            ip_address: 客户端IP地址
            
        Returns:
            str: 5位数字OTP，或None（如果已有未过期的OTP）
        """
        with self.lock:
            # 清理过期OTP
            self._cleanup_expired()
            
            # 检查是否已有未过期的OTP
            if ip_address in self.otp_store:
                otp_data = self.otp_store[ip_address]
                if time.time() < otp_data['expiry']:
                    return None  # 已有有效OTP
            
            # 生成5位数字OTP
            otp_code = str(random.randint(10000, 99999))
            expiry_time = time.time() + (self.expiry_minutes * 60)
            
            self.otp_store[ip_address] = {
                'code': otp_code,
                'expiry': expiry_time,
                'created': time.time(),
                'email_sent': False
            }
            
            logger.info(f"为IP {ip_address} 生成OTP: {otp_code}, 有效期至 {datetime.fromtimestamp(expiry_time).strftime('%H:%M:%S')}")
            return otp_code
    
    def validate(self, ip_address, code):
        """
        验证OTP代码
        
        Args:
            ip_address: 客户端IP地址
            code: 用户输入的OTP代码
            
        Returns:
            tuple: (是否成功, 错误消息)
        """
        with self.lock:
            # 清理过期OTP
            self._cleanup_expired()
            
            if ip_address not in self.otp_store:
                return False, "验证码不存在或已过期"
            
            otp_data = self.otp_store[ip_address]
            
            # 检查是否过期
            if time.time() > otp_data['expiry']:
                del self.otp_store[ip_address]
                return False, "验证码已过期"
            
            # 验证代码（不区分大小写，去除空格）
            user_code = str(code).strip()
            stored_code = otp_data['code']
            
            if user_code != stored_code:
                return False, "验证码不正确"
            
            # 验证成功，删除OTP防止重复使用
            del self.otp_store[ip_address]
            logger.info(f"IP {ip_address} OTP验证成功")
            return True, "验证成功"
    
    def mark_email_sent(self, ip_address):
        """标记OTP邮件已发送"""
        with self.lock:
            if ip_address in self.otp_store:
                self.otp_store[ip_address]['email_sent'] = True
    
    def get_remaining_time(self, ip_address):
        """
        获取剩余有效期（秒）
        
        Returns:
            int: 剩余秒数，-1表示OTP不存在
        """
        with self.lock:
            if ip_address not in self.otp_store:
                return -1
            
            otp_data = self.otp_store[ip_address]
            remaining = otp_data['expiry'] - time.time()
            return max(0, int(remaining))
    
    def _cleanup_expired(self):
        """清理过期OTP"""
        now = time.time()
        # 每5分钟清理一次
        if now - self.last_cleanup < self.cleanup_interval:
            return
        
        expired_ips = []
        for ip, data in self.otp_store.items():
            if now > data['expiry']:
                expired_ips.append(ip)
        
        for ip in expired_ips:
            del self.otp_store[ip]
        
        if expired_ips:
            logger.debug(f"清理了 {len(expired_ips)} 个过期OTP")
        
        self.last_cleanup = now


class SessionManager:
    """会话管理器 - 管理认证会话，30分钟有效期，内存存储"""
    
    def __init__(self, expiry_minutes=30):
        """
        初始化会话管理器
        
        Args:
            expiry_minutes: 会话有效期（分钟），默认30分钟
        """
        self.expiry_minutes = expiry_minutes
        self.sessions = {}  # token -> {'ip': '127.0.0.1', 'expiry': timestamp, 'created': timestamp}
        self.lock = threading.Lock()
        self.cleanup_interval = 300  # 每5分钟清理一次过期会话
        self.last_cleanup = time.time()
    
    def create(self, ip_address):
        """
        为新认证用户创建会话
        
        Args:
            ip_address: 客户端IP地址
            
        Returns:
            str: 会话令牌
        """
        with self.lock:
            # 清理过期会话
            self._cleanup_expired()
            
            # 生成32字符的随机令牌
            token = secrets.token_hex(16)
            expiry_time = time.time() + (self.expiry_minutes * 60)
            
            self.sessions[token] = {
                'ip': ip_address,
                'expiry': expiry_time,
                'created': time.time(),
                'last_activity': time.time()
            }
            
            logger.info(f"为IP {ip_address} 创建会话，令牌: {token[:8]}..., 有效期至 {datetime.fromtimestamp(expiry_time).strftime('%H:%M:%S')}")
            return token
    
    def validate(self, token, ip_address):
        """
        验证会话令牌
        
        Args:
            token: 会话令牌
            ip_address: 客户端IP地址（用于IP绑定）
            
        Returns:
            tuple: (是否有效, 错误消息)
        """
        with self.lock:
            # 清理过期会话
            self._cleanup_expired()
            
            if token not in self.sessions:
                return False, "会话无效或已过期"
            
            session_data = self.sessions[token]
            
            # 检查IP绑定
            if session_data['ip'] != ip_address:
                logger.warning(f"会话令牌IP不匹配: 令牌IP={session_data['ip']}, 请求IP={ip_address}")
                return False, "会话无效"
            
            # 检查是否过期
            if time.time() > session_data['expiry']:
                del self.sessions[token]
                return False, "会话已过期"
            
            # 更新最后活动时间
            session_data['last_activity'] = time.time()
            
            # 计算剩余时间
            remaining = session_data['expiry'] - time.time()
            logger.debug(f"会话验证成功，剩余时间: {int(remaining)}秒")
            return True, "会话有效"
    
    def invalidate(self, token):
        """使会话令牌失效"""
        with self.lock:
            if token in self.sessions:
                del self.sessions[token]
                logger.info(f"会话令牌已失效: {token[:8]}...")
    
    def get_session_info(self, token):
        """获取会话信息"""
        with self.lock:
            if token not in self.sessions:
                return None
            
            session_data = self.sessions[token]
            return {
                'ip': session_data['ip'],
                'created': datetime.fromtimestamp(session_data['created']).strftime('%Y-%m-%d %H:%M:%S'),
                'expiry': datetime.fromtimestamp(session_data['expiry']).strftime('%Y-%m-%d %H:%M:%S'),
                'last_activity': datetime.fromtimestamp(session_data['last_activity']).strftime('%Y-%m-%d %H:%M:%S'),
                'remaining_seconds': int(max(0, session_data['expiry'] - time.time()))
            }
    
    def _cleanup_expired(self):
        """清理过期会话"""
        now = time.time()
        # 每5分钟清理一次
        if now - self.last_cleanup < self.cleanup_interval:
            return
        
        expired_tokens = []
        for token, data in self.sessions.items():
            if now > data['expiry']:
                expired_tokens.append(token)
        
        for token in expired_tokens:
            del self.sessions[token]
        
        if expired_tokens:
            logger.debug(f"清理了 {len(expired_tokens)} 个过期会话")
        
        self.last_cleanup = now


# 全局速率限制器实例 (1 QPS = 60请求/分钟，60秒窗口)
rate_limiter = RateLimiter(requests_per_minute=60, window_seconds=60)  # 1 QPS

# OTP专用速率限制器 (1请求/5分钟，300秒窗口)
otp_rate_limiter = RateLimiter(requests_per_minute=1, window_seconds=300)  # 1请求每5分钟

# OTP管理器实例 (10分钟有效期)
otp_manager = OTPManager(expiry_minutes=10)

# 会话管理器实例 (30分钟有效期)
session_manager = SessionManager(expiry_minutes=30)

# 审计日志函数
def audit_log(action, ip_address, details=""):
    """
    记录管理操作审计日志
    
    Args:
        action: 操作类型 (OTP_REQUEST, OTP_VERIFY, WATCHLIST_UPDATE等)
        ip_address: 客户端IP地址
        details: 操作详情
    """
    try:
        audit_log_file = Path(__file__).parent.parent / 'logs' / 'management_audit.log'
        audit_log_file.parent.mkdir(parents=True, exist_ok=True)
        
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        log_entry = f"{timestamp} | {ip_address} | {action} | {details}\n"
        
        with open(audit_log_file, 'a', encoding='utf-8') as f:
            f.write(log_entry)
        
        logger.debug(f"审计日志: {action} - {ip_address} - {details}")
    except Exception as e:
        logger.error(f"写入审计日志失败: {e}")


class HealthHandler(http.server.BaseHTTPRequestHandler):
    """HTTP请求处理器"""
    
    def __init__(self, *args, health_server=None, **kwargs):
        self.health_server = health_server
        super().__init__(*args, **kwargs)
    
    def do_GET(self):
        """处理GET请求"""
        # 检查速率限制
        client_ip = self.client_address[0]
        if not rate_limiter.is_allowed(client_ip):
            self.send_response(429)  # Too Many Requests
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(f"请求频率过高 (限制: 1 QPS). 客户端IP: {client_ip}".encode('utf-8'))
            logger.warning(f"请求频率过高被拒绝: {client_ip} - {self.path}")
            return
        
        try:
            if self.path == '/':
                self.send_html_response()
            elif self.path == '/status':
                self.send_json_response()
            elif self.path == '/health':
                self.send_health_response()
            elif self.path == '/test-email':
                self.send_test_email_response()
            elif self.path == '/metrics':
                self.send_metrics_response()
            elif self.path == '/request-otp':
                self.handle_otp_request()
            elif self.path.startswith('/verify-otp'):
                self.handle_otp_verification()
            elif self.path == '/manage':
                self.handle_management_page()
            elif self.path == '/logout':
                self.handle_logout()
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
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(f"请求频率过高 (限制: 1 QPS). 客户端IP: {client_ip}".encode('utf-8'))
            logger.warning(f"POST请求频率过高被拒绝: {client_ip} - {self.path}")
            return
        
        try:
            if self.path == '/update-watchlist':
                self.handle_watchlist_update()
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
                error_message="请求过于频繁，请5分钟后再试",
                remaining_time=300
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
            audit_log("OTP_REQUEST_SUCCESS", client_ip, f"验证码已发送: {otp_code}")
            # 重定向到验证页面
            self.send_response(302)
            self.send_header('Location', '/verify-otp')
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
        otp_code = query_params.get('code', [''])[0].strip()
        
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
            self.send_header('Location', '/manage')
            self.send_header('Set-Cookie', cookie)
            self.end_headers()
        else:
            audit_log("OTP_VERIFY_FAILED", client_ip, f"验证失败: {message}")
            self._send_otp_verification_page(
                error_message=message,
                code_value=otp_code
            )
    
    def handle_management_page(self):
        """处理管理页面请求"""
        # 验证会话
        is_valid, message, token = self._validate_session()
        if not is_valid:
            # 重定向到OTP请求页面
            self.send_response(302)
            self.send_header('Location', '/request-otp')
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
        self.send_header('Location', '/')
        self.send_header('Set-Cookie', 'token=; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT; HttpOnly')
        self.end_headers()
    
    def handle_watchlist_update(self):
        """处理监控列表更新"""
        client_ip = self.client_address[0]
        
        # 验证会话
        is_valid, message, token = self._validate_session()
        if not is_valid:
            self.send_response(302)
            self.send_header('Location', '/request-otp')
            self.end_headers()
            return
        
        # 读取POST数据
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length > 0:
            post_data = self.rfile.read(content_length).decode('utf-8')
            post_params = parse_qs(post_data)
        else:
            post_params = {}
        
        action = post_params.get('action', [''])[0]
        stock_code = post_params.get('stock_code', [''])[0].strip()
        
        # 根据action处理
        if action == 'add':
            self._handle_add_stock(client_ip, stock_code, token)
        elif action == 'remove':
            self._handle_remove_stock(client_ip, stock_code, token)
        elif action == 'clear':
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
        current_stocks = config.get('stocks', [])
        
        # 检查是否已存在
        if stock_code in current_stocks:
            self._send_management_page(error_message=f"股票 {stock_code} 已在监控列表中")
            return
        
        # 添加到列表
        current_stocks.append(stock_code)
        config['stocks'] = current_stocks
        
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
        current_stocks = config.get('stocks', [])
        
        # 检查是否存在
        if stock_code not in current_stocks:
            self._send_management_page(error_message=f"股票 {stock_code} 不在监控列表中")
            return
        
        # 从列表中移除
        current_stocks.remove(stock_code)
        config['stocks'] = current_stocks
        
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
        old_stocks = config.get('stocks', [])
        
        # 清空列表
        config['stocks'] = []
        
        # 保存配置
        success = self._save_config(config)
        if success:
            audit_log("WATCHLIST_CLEAR", client_ip, f"清空所有股票，原数量: {len(old_stocks)}")
            
            # 重新加载健康服务器配置
            self.health_server.config = config
            
            # 发送确认邮件
            self._send_watchlist_change_email(client_ip, "清空", details=f"清空了 {len(old_stocks)} 只股票")
            
            self._send_management_page(
                success_message=f"成功清空所有股票（共 {len(old_stocks)} 只），已发送确认邮件"
            )
        else:
            self._send_management_page(error_message="保存配置失败，请检查文件权限")
    
    def _save_config(self, config):
        """保存配置到文件"""
        try:
            import yaml
            config_path = Path(__file__).parent.parent / 'config' / 'config.yaml'
            
            # 创建备份
            backup_path = config_path.with_suffix('.yaml.backup')
            import shutil
            shutil.copy2(config_path, backup_path)
            
            # 写入新配置
            with open(config_path, 'w', encoding='utf-8') as f:
                yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
            
            logger.info(f"配置文件已更新: {config_path}")
            return True
        except Exception as e:
            logger.error(f"保存配置文件失败: {e}")
            return False
    
    def _send_otp_email(self, client_ip, otp_code):
        """发送OTP邮件"""
        try:
            from src.email_notifier import EmailNotifier
            
            # 获取接收邮箱
            if not self.health_server or not self.health_server.config:
                logger.error("无法获取系统配置发送OTP邮件")
                return False
            
            config = self.health_server.config
            receiver_email = config.get('email', {}).get('receiver_email')
            if not receiver_email:
                logger.error("未配置接收邮箱，无法发送OTP邮件")
                return False
            
            # 创建邮件内容
            subject = f"股票量化系统管理验证码 (10分钟内有效)"
            current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            body = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="utf-8">
                <style>
                    body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
                    .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                    .header {{ background-color: #4CAF50; color: white; padding: 20px; text-align: center; }}
                    .content {{ padding: 30px; background-color: #f9f9f9; }}
                    .otp-code {{ font-size: 32px; font-weight: bold; text-align: center; color: #4CAF50; margin: 20px 0; padding: 15px; background-color: white; border: 2px dashed #4CAF50; }}
                    .info-box {{ background-color: #e3f2fd; padding: 15px; margin: 20px 0; border-left: 4px solid #2196F3; }}
                    .footer {{ margin-top: 30px; color: #666; font-size: 0.9em; }}
                </style>
            </head>
            <body>
                <div class="container">
                    <div class="header">
                        <h1>股票量化系统管理验证码</h1>
                    </div>
                    <div class="content">
                        <p>您好，</p>
                        <p>您请求的管理验证码是:</p>
                        
                        <div class="otp-code">{otp_code}</div>
                        
                        <p>此验证码将在 <strong>10分钟</strong> 后失效。</p>
                        
                        <div class="info-box">
                            <p><strong>请求信息:</strong></p>
                            <ul>
                                <li>时间: {current_time}</li>
                                <li>IP地址: {client_ip}</li>
                                <li>接收邮箱: {receiver_email}</li>
                            </ul>
                        </div>
                        
                        <p>如果不是您本人操作，请忽略此邮件。</p>
                        
                        <p>系统管理地址: http://{self.headers.get('Host', 'localhost:1933')}/manage</p>
                    </div>
                    <div class="footer">
                        <p>此邮件由股票量化系统自动发送，请勿回复。</p>
                    </div>
                </div>
            </body>
            </html>
            """
            
            # 使用现有的邮件发送逻辑
            notifier = EmailNotifier(config)
            notifier._send_email(subject, body)
            
            logger.info(f"OTP邮件已发送到 {receiver_email}, 验证码: {otp_code}")
            return True
            
        except Exception as e:
            logger.error(f"发送OTP邮件失败: {e}")
            return False
    
    def _send_watchlist_change_email(self, client_ip, action, stock_code=None, details=None):
        """发送监控列表变更确认邮件"""
        try:
            from src.email_notifier import EmailNotifier
            
            if not self.health_server or not self.health_server.config:
                logger.error("无法获取系统配置发送确认邮件")
                return False
            
            config = self.health_server.config
            receiver_email = config.get('email', {}).get('receiver_email')
            if not receiver_email:
                logger.error("未配置接收邮箱，无法发送确认邮件")
                return False
            
            # 获取当前股票列表
            current_stocks = config.get('stocks', [])
            current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            # 构建邮件内容
            if action == "添加":
                subject = f"股票监控列表已更新 - 添加股票 {stock_code}"
                action_desc = f"添加了股票 {stock_code}"
            elif action == "移除":
                subject = f"股票监控列表已更新 - 移除股票 {stock_code}"
                action_desc = f"移除了股票 {stock_code}"
            elif action == "清空":
                subject = f"股票监控列表已清空"
                action_desc = details or "清空了所有股票"
            else:
                subject = f"股票监控列表已更新"
                action_desc = action
            
            body = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="utf-8">
                <style>
                    body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
                    .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                    .header {{ background-color: #2196F3; color: white; padding: 20px; text-align: center; }}
                    .content {{ padding: 30px; background-color: #f9f9f9; }}
                    .info-box {{ background-color: #e3f2fd; padding: 15px; margin: 20px 0; border-left: 4px solid #2196F3; }}
                    .stock-list {{ background-color: white; padding: 15px; border: 1px solid #ddd; }}
                    .footer {{ margin-top: 30px; color: #666; font-size: 0.9em; }}
                </style>
            </head>
            <body>
                <div class="container">
                    <div class="header">
                        <h1>股票监控列表更新确认</h1>
                    </div>
                    <div class="content">
                        <p>您好，</p>
                        <p>您的股票监控列表已成功更新。</p>
                        
                        <div class="info-box">
                            <p><strong>操作详情:</strong></p>
                            <ul>
                                <li>操作类型: {action_desc}</li>
                                <li>操作时间: {current_time}</li>
                                <li>操作IP: {client_ip}</li>
                                <li>当前监控股票数量: {len(current_stocks)} 只</li>
                            </ul>
                        </div>
            """
            
            if current_stocks:
                body += f"""
                        <div class="stock-list">
                            <p><strong>当前监控股票列表:</strong></p>
                            <ul>
                """
                for stock in current_stocks:
                    body += f"<li>{stock}</li>\n"
                body += """
                            </ul>
                        </div>
                """
            else:
                body += """
                        <div class="stock-list">
                            <p><strong>当前监控股票列表:</strong> 无</p>
                        </div>
                """
            
            body += f"""
                        <p>下次系统运行时间: {self.health_server._calculate_next_run_time() if hasattr(self.health_server, '_calculate_next_run_time') else '未知'}</p>
                        
                        <p>如果不是您本人操作，请立即登录系统检查。</p>
                    </div>
                    <div class="footer">
                        <p>此邮件由股票量化系统自动发送，请勿回复。</p>
                    </div>
                </div>
            </body>
            </html>
            """
            
            # 发送邮件
            notifier = EmailNotifier(config)
            notifier._send_email(subject, body)
            
            logger.info(f"监控列表变更确认邮件已发送到 {receiver_email}, 操作: {action_desc}")
            return True
            
        except Exception as e:
            logger.error(f"发送监控列表变更确认邮件失败: {e}")
            return False
    
    def _get_rate_limit_stats(self):
        """获取速率限制统计信息（HTML格式）"""
        stats = rate_limiter.get_stats()
        if not stats:
            return "暂无请求记录"
        
        lines = []
        for ip, data in stats.items():
            status = "🚫 已限制" if data['blocked'] else "✅ 正常"
            lines.append(f"{ip}: {data['recent_requests']}/60 请求 {status}")
        
        return "<br>".join(lines)
    
    def _get_session_token(self):
        """从Cookie中提取会话令牌"""
        cookie_header = self.headers.get('Cookie', '')
        if not cookie_header:
            return None
        
        # 简单解析Cookie (格式: token=abc123; other=value)
        for cookie in cookie_header.split(';'):
            cookie = cookie.strip()
            if cookie.startswith('token='):
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
        if not re.match(r'^\d{6}$', code_str):
            return False, "股票代码必须为6位数字"
        
        # 市场验证 (首位数)
        first_digit = code_str[0]
        valid_markets = {'6': '上海', '0': '深圳', '3': '创业板'}
        if first_digit not in valid_markets:
            logger.warning(f"非常规股票代码首位: {first_digit} (代码: {code_str})")
            # 不拒绝，只记录警告
        
        return True, "格式正确"
    
    def _send_error_page(self, title, message, back_url="/"):
        """发送错误页面"""
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <title>{html.escape(title)}</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 40px; line-height: 1.6; }}
                .error {{ color: #d32f2f; background-color: #ffebee; padding: 20px; border-radius: 4px; border-left: 4px solid #d32f2f; }}
                .back-link {{ margin-top: 20px; display: inline-block; }}
            </style>
        </head>
        <body>
            <h1>❌ {html.escape(title)}</h1>
            <div class="error">
                {html.escape(message)}
            </div>
            <a href="{html.escape(back_url)}" class="back-link">← 返回</a>
        </body>
        </html>
        """
        
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(html_content.encode('utf-8'))))
        self.end_headers()
        self.wfile.write(html_content.encode('utf-8'))
    
    def _send_otp_request_page(self, error_message=None, remaining_time=None):
        """发送OTP请求页面"""
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
                receiver_email = self.health_server.config.get('email', {}).get('receiver_email', '未知')
        except:
            pass
        
        # 计算下次可请求时间
        next_request_time = "现在可以请求"
        if not otp_allowed:
            # 查找最近的请求时间
            # 简单实现: 显示5分钟后
            next_request_time = "5分钟后"
        
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <title>请求管理验证码</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 40px; line-height: 1.6; }}
                .container {{ max-width: 600px; margin: 0 auto; }}
                .info-box {{ background-color: #e3f2fd; border-left: 4px solid #2196F3; padding: 20px; margin: 20px 0; }}
                .error-box {{ background-color: #ffebee; border-left: 4px solid #f44336; padding: 20px; margin: 20px 0; }}
                .success-box {{ background-color: #e8f5e9; border-left: 4px solid #4caf50; padding: 20px; margin: 20px 0; }}
                .form-group {{ margin: 20px 0; }}
                .button {{ background-color: #4caf50; color: white; border: none; padding: 12px 24px; border-radius: 4px; cursor: pointer; font-size: 16px; }}
                .button:disabled {{ background-color: #cccccc; cursor: not-allowed; }}
                .button:hover:not(:disabled) {{ background-color: #45a049; }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>🔐 请求管理验证码</h1>
                
                <div class="info-box">
                    <h3>验证码将发送到您的订阅邮箱</h3>
                    <p><strong>邮箱地址:</strong> {html.escape(receiver_email)}</p>
                    <p><strong>验证码类型:</strong> 5位数字，10分钟内有效</p>
                    <p><strong>您的IP地址:</strong> {html.escape(client_ip)}</p>
                </div>
        """
        
        if error_message:
            html_content += f"""
                <div class="error-box">
                    <h3>⚠️ 错误</h3>
                    <p>{html.escape(error_message)}</p>
                </div>
            """
        
        if remaining > 0:
            html_content += f"""
                <div class="info-box">
                    <h3>已有未过期验证码</h3>
                    <p>您已有一个有效验证码，剩余时间: {remaining}秒</p>
                    <p><a href="/verify-otp">点击这里输入验证码</a></p>
                </div>
            """
        
        html_content += f"""
                <div class="form-group">
                    <p><strong>状态:</strong> { "可以请求验证码" if can_request and otp_allowed else "暂时无法请求" }</p>
                    <p><strong>下次可请求时间:</strong> {html.escape(next_request_time)}</p>
                </div>
                
                <form method="GET" action="/request-otp">
                    <button type="submit" class="button" {'disabled' if not (can_request and otp_allowed) else ''}>
                        {'发送验证码到邮箱' if can_request and otp_allowed else '暂时无法发送'}
                    </button>
                </form>
                
                <p style="margin-top: 30px;">
                    <a href="/">← 返回健康检查页面</a>
                </p>
            </div>
        </body>
        </html>
        """
        
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(html_content.encode('utf-8'))))
        self.end_headers()
        self.wfile.write(html_content.encode('utf-8'))
    
    def _send_otp_verification_page(self, error_message=None, code_value=""):
        """发送OTP验证页面"""
        client_ip = self.client_address[0]
        remaining = otp_manager.get_remaining_time(client_ip)
        
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <title>验证管理验证码</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 40px; line-height: 1.6; }}
                .container {{ max-width: 500px; margin: 0 auto; }}
                .info-box {{ background-color: #e3f2fd; border-left: 4px solid #2196F3; padding: 20px; margin: 20px 0; }}
                .error-box {{ background-color: #ffebee; border-left: 4px solid #f44336; padding: 20px; margin: 20px 0; }}
                .form-group {{ margin: 20px 0; }}
                .input-field {{ width: 100%; padding: 12px; font-size: 18px; border: 1px solid #ddd; border-radius: 4px; box-sizing: border-box; }}
                .button {{ background-color: #4caf50; color: white; border: none; padding: 12px 24px; border-radius: 4px; cursor: pointer; font-size: 16px; width: 100%; }}
                .button:hover {{ background-color: #45a049; }}
                .countdown {{ color: #666; font-size: 0.9em; margin-top: 5px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>🔐 验证管理验证码</h1>
                
                <div class="info-box">
                    <h3>请输入5位数字验证码</h3>
                    <p>验证码已发送到您的订阅邮箱，10分钟内有效</p>
                    <p><strong>剩余时间:</strong> {remaining if remaining > 0 else 0}秒</p>
                    <p><strong>您的IP地址:</strong> {html.escape(client_ip)}</p>
                </div>
        """
        
        if error_message:
            html_content += f"""
                <div class="error-box">
                    <h3>⚠️ 验证失败</h3>
                    <p>{html.escape(error_message)}</p>
                </div>
            """
        
        html_content += f"""
                <form method="GET" action="/verify-otp">
                    <div class="form-group">
                        <label for="code"><strong>验证码:</strong></label>
                        <input type="text" id="code" name="code" class="input-field" 
                               placeholder="输入5位数字" maxlength="5" pattern="\\d{{5}}" 
                               value="{html.escape(code_value)}" required>
                        <div class="countdown">请输入收到的5位数字验证码</div>
                    </div>
                    
                    <button type="submit" class="button">验证并登录</button>
                </form>
                
                <p style="margin-top: 30px;">
                    <a href="/request-otp">← 重新请求验证码</a> | 
                    <a href="/">返回健康检查页面</a>
                </p>
            </div>
        </body>
        </html>
        """
        
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(html_content.encode('utf-8'))))
        self.end_headers()
        self.wfile.write(html_content.encode('utf-8'))
    
    def _send_management_page(self, error_message=None, success_message=None):
        """发送监控列表管理页面"""
        # 验证会话
        is_valid, message, token = self._validate_session()
        if not is_valid:
            self._send_error_page("会话无效", message, "/request-otp")
            return
        
        # 获取当前监控股票列表
        current_stocks = []
        try:
            if self.health_server and self.health_server.config:
                current_stocks = self.health_server.config.get('stocks', [])
        except:
            current_stocks = []
        
        # 构建股票列表HTML
        stocks_html = ""
        if current_stocks:
            for stock in current_stocks:
                stock_str = str(stock)
                stocks_html += f"""
                <tr>
                    <td>{html.escape(stock_str)}</td>
                    <td>
                        <form method="POST" action="/update-watchlist" style="display: inline;">
                            <input type="hidden" name="action" value="remove">
                            <input type="hidden" name="stock_code" value="{html.escape(stock_str)}">
                            <button type="submit" class="button-remove">移除</button>
                        </form>
                    </td>
                </tr>
                """
        else:
            stocks_html = """
                <tr>
                    <td colspan="2" style="text-align: center; color: #666;">暂无监控股票</td>
                </tr>
            """
        
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <title>监控股票列表管理</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 40px; line-height: 1.6; }}
                .container {{ max-width: 800px; margin: 0 auto; }}
                .info-box {{ background-color: #e3f2fd; border-left: 4px solid #2196F3; padding: 20px; margin: 20px 0; }}
                .error-box {{ background-color: #ffebee; border-left: 4px solid #f44336; padding: 20px; margin: 20px 0; }}
                .success-box {{ background-color: #e8f5e9; border-left: 4px solid #4caf50; padding: 20px; margin: 20px 0; }}
                .form-group {{ margin: 20px 0; }}
                .input-field {{ padding: 12px; font-size: 16px; border: 1px solid #ddd; border-radius: 4px; width: 200px; }}
                .button {{ padding: 10px 20px; border-radius: 4px; cursor: pointer; font-size: 14px; border: none; }}
                .button-add {{ background-color: #4caf50; color: white; }}
                .button-add:hover {{ background-color: #45a049; }}
                .button-remove {{ background-color: #f44336; color: white; }}
                .button-remove:hover {{ background-color: #d32f2f; }}
                .button-logout {{ background-color: #ff9800; color: white; }}
                .stock-table {{ width: 100%; border-collapse: collapse; margin: 20px 0; }}
                .stock-table th, .stock-table td {{ border: 1px solid #ddd; padding: 12px; text-align: left; }}
                .stock-table th {{ background-color: #f2f2f2; }}
                .session-info {{ color: #666; font-size: 0.9em; margin-bottom: 20px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>📈 监控股票列表管理</h1>
                
                <div class="session-info">
                    <strong>登录状态:</strong> 已登录 | 
                    <strong>IP地址:</strong> {html.escape(self.client_address[0])} | 
                    <a href="/logout">退出登录</a>
                </div>
        """
        
        if error_message:
            html_content += f"""
                <div class="error-box">
                    <h3>⚠️ 操作失败</h3>
                    <p>{html.escape(error_message)}</p>
                </div>
            """
        
        if success_message:
            html_content += f"""
                <div class="success-box">
                    <h3>✅ 操作成功</h3>
                    <p>{html.escape(success_message)}</p>
                </div>
            """
        
        html_content += f"""
                <div class="info-box">
                    <h3>添加新股票</h3>
                    <p>请输入6位数字股票代码（上海6开头，深圳0或3开头）</p>
                    <form method="POST" action="/update-watchlist">
                        <input type="hidden" name="action" value="add">
                        <input type="text" name="stock_code" class="input-field" 
                               placeholder="例如: 000001" maxlength="6" pattern="\\d{{6}}" required>
                        <button type="submit" class="button button-add">添加股票</button>
                    </form>
                </div>
                
                <h2>当前监控股票 ({len(current_stocks)} 只)</h2>
                <table class="stock-table">
                    <thead>
                        <tr>
                            <th>股票代码</th>
                            <th>操作</th>
                        </tr>
                    </thead>
                    <tbody>
                        {stocks_html}
                    </tbody>
                </table>
                
                <div class="info-box">
                    <h3>批量操作</h3>
                    <form method="POST" action="/update-watchlist" onsubmit="return confirm('确定要清空所有股票吗？此操作不可撤销。');">
                        <input type="hidden" name="action" value="clear">
                        <button type="submit" class="button button-remove">清空所有股票</button>
                    </form>
                </div>
                
                <p style="margin-top: 30px;">
                    <a href="/">← 返回健康检查页面</a>
                </p>
            </div>
        </body>
        </html>
        """
        
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(html_content.encode('utf-8'))))
        self.end_headers()
        self.wfile.write(html_content.encode('utf-8'))
    
    def send_html_response(self):
        """发送HTML格式的状态页面"""
        status = self.health_server.get_status() if self.health_server else {}
        
        # 辅助函数：安全获取并转义HTML
        def safe_get(key, default='未知'):
            value = status.get(key, default)
            if value is None:
                value = default
            return html.escape(str(value))
        
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <title>股票量化系统健康检查</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 40px; line-height: 1.6; }}
                h1 {{ color: #333; border-bottom: 2px solid #4CAF50; padding-bottom: 10px; }}
                .status {{ padding: 8px 15px; border-radius: 4px; color: white; font-weight: bold;
                         background-color: #4CAF50; display: inline-block; }}
                .info-box {{ background-color: #f9f9f9; border-left: 4px solid #2196F3; 
                          padding: 15px; margin: 15px 0; }}
                .info-table {{ border-collapse: collapse; width: 100%; margin: 20px 0; }}
                .info-table th, .info-table td {{ border: 1px solid #ddd; padding: 12px; text-align: left; }}
                .info-table th {{ background-color: #f2f2f2; font-weight: bold; }}
                .endpoints {{ margin-top: 30px; padding: 20px; background-color: #f5f5f5; }}
                .endpoint {{ margin: 5px 0; font-family: monospace; }}
                .timestamp {{ color: #666; font-size: 0.9em; }}
                .security-info {{ background-color: #fff8e1; border-left: 4px solid #ffc107; 
                               padding: 15px; margin: 20px 0; }}
                .rate-limit-stats {{ font-family: monospace; font-size: 0.9em; color: #666; }}
            </style>
        </head>
        <body>
            <h1>🚀 股票量化系统健康检查</h1>
            
            <p><strong>状态:</strong> <span class="status">运行正常</span></p>
            <p class="timestamp">最后更新: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
            
            <div class="security-info">
                <h3>🔒 安全状态</h3>
                <p><strong>速率限制:</strong> 已启用 (1 QPS / 60 请求每分钟)</p>
                <p><strong>HTML注入防护:</strong> 已启用 (所有动态内容转义)</p>
                <p><strong>客户端IP:</strong> {html.escape(str(self.client_address[0]))}</p>
                <p class="rate-limit-stats">
                    <strong>速率限制统计:</strong><br>
                    {self._get_rate_limit_stats()}
                </p>
            </div>
            
            <div class="info-box">
                <h2>🖥️ 服务器信息</h2>
                <table class="info-table">
                    <tr>
                        <th>项目</th>
                        <th>值</th>
                    </tr>
                    <tr>
                        <td>主机名</td>
                        <td>{safe_get('hostname', '未知')}</td>
                    </tr>
                    <tr>
                        <td>IP地址</td>
                        <td>{safe_get('ip_address', '未知')}</td>
                    </tr>
                    <tr>
                        <td>内核版本</td>
                        <td>{safe_get('kernel_version', '未知')}</td>
                    </tr>
                    <tr>
                        <td>系统</td>
                        <td>{safe_get('system', '未知')} ({safe_get('machine', '未知')})</td>
                    </tr>
                    <tr>
                        <td>Python版本</td>
                        <td>{safe_get('python_version', sys.version.split()[0])}</td>
                    </tr>
                    <tr>
                        <td>启动时间</td>
                        <td>{safe_get('start_time', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}</td>
                    </tr>
                    <tr>
                        <td>运行时间</td>
                        <td>{safe_get('uptime', '0秒')}</td>
                    </tr>
                </table>
            </div>
            
            <div class="info-box">
                <h2>📊 系统状态</h2>
                <table class="info-table">
                    <tr>
                        <th>项目</th>
                        <th>值</th>
                    </tr>
                    <tr>
                        <td>缓存目录大小</td>
                        <td>{safe_get('cache_size', '未知')}</td>
                    </tr>
                    <tr>
                        <td>数据目录大小</td>
                        <td>{safe_get('data_size', '未知')}</td>
                    </tr>
                    <tr>
                        <td>日志目录大小</td>
                        <td>{safe_get('log_size', '未知')}</td>
                    </tr>
                    <tr>
                        <td>磁盘使用率</td>
                        <td>{safe_get('disk_usage', '未知')}</td>
                    </tr>
                    <tr>
                        <td>内存使用率</td>
                        <td>{safe_get('memory_usage', '未知')}</td>
                    </tr>
                    <tr>
                        <td>监控股票数量</td>
                        <td>{safe_get('stock_count', '未知')}</td>
                    </tr>
                    <tr>
                        <td>最后运行时间</td>
                        <td>{safe_get('last_run_time', '未知')}</td>
                    </tr>
                    <tr>
                        <td>下次运行时间</td>
                        <td>{safe_get('next_run_time', '未知')}</td>
                    </tr>
                </table>
            </div>
            
            <div class="endpoints">
                <h2>🔗 API端点</h2>
                <div class="endpoint"><a href="/">/</a> - HTML状态页面 (当前页面)</div>
                <div class="endpoint"><a href="/status">/status</a> - JSON格式状态信息</div>
                <div class="endpoint"><a href="/health">/health</a> - 健康检查端点 (返回200 OK)</div>
                <div class="endpoint"><a href="/test-email">/test-email</a> - 发送测试邮件 (需要GET参数?force=true)</div>
                <div class="endpoint"><a href="/metrics">/metrics</a> - 系统指标 (Prometheus格式)</div>
                <div class="endpoint"><a href="/request-otp">/request-otp</a> - 管理验证码请求</div>
                <div class="endpoint"><a href="/manage">/manage</a> - 监控股票列表管理 (需要验证)</div>
            </div>
            
            <p style="color: #666; margin-top: 30px; font-size: 0.9em;">
                💡 健康服务器运行在端口1933，用于监控系统状态和发送测试邮件。
            </p>
        </body>
        </html>
        """
        
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(html_content.encode('utf-8'))))
        self.end_headers()
        self.wfile.write(html_content.encode('utf-8'))
    
    def send_json_response(self):
        """发送JSON格式的状态信息"""
        status = self.health_server.get_status() if self.health_server else {}
        
        response = {
            'status': 'healthy',
            'timestamp': datetime.now().isoformat(),
            'server': {
                'hostname': status.get('hostname'),
                'ip_address': status.get('ip_address'),
                'kernel_version': status.get('kernel_version'),
                'system': status.get('system'),
                'machine': status.get('machine'),
                'python_version': status.get('python_version', sys.version.split()[0])
            },
            'system': {
                'cache_size': status.get('cache_size'),
                'data_size': status.get('data_size'),
                'log_size': status.get('log_size'),
                'disk_usage': status.get('disk_usage'),
                'memory_usage': status.get('memory_usage'),
                'uptime': status.get('uptime'),
                'start_time': status.get('start_time')
            },
            'application': {
                'stock_count': status.get('stock_count'),
                'last_run_time': status.get('last_run_time'),
                'next_run_time': status.get('next_run_time'),
                'monitored_stocks': status.get('monitored_stocks', [])
            },
            'endpoints': {
                '/': 'HTML status page',
                '/status': 'JSON status',
                '/health': 'Health check',
                '/test-email': 'Send test email',
                '/metrics': 'System metrics'
            }
        }
        
        json_response = json.dumps(response, ensure_ascii=False, indent=2)
        
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(json_response.encode('utf-8'))))
        self.end_headers()
        self.wfile.write(json_response.encode('utf-8'))
    
    def send_health_response(self):
        """发送健康检查响应"""
        response = {
            'status': 'healthy',
            'timestamp': datetime.now().isoformat()
        }
        
        json_response = json.dumps(response, ensure_ascii=False)
        
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(json_response.encode('utf-8'))))
        self.end_headers()
        self.wfile.write(json_response.encode('utf-8'))
    
    def send_test_email_response(self):
        """处理测试邮件请求"""
        from src.email_notifier import EmailNotifier
        import yaml
        
        # 检查是否强制发送 - 使用安全的查询参数解析
        force = False
        if '?' in self.path:
            try:
                from urllib.parse import parse_qs, urlparse
                parsed = urlparse(self.path)
                query_params = parse_qs(parsed.query)
                # 检查force参数，不区分大小写
                force_param = query_params.get('force', [''])[0].lower()
                force = force_param == 'true'
                logger.debug(f"解析测试邮件请求参数: path={self.path}, force={force}")
            except Exception as e:
                logger.warning(f"解析查询参数失败: {e}, 使用默认值 force=False")
                force = False
        
        if not force:
            # 如果是日常运行时间，则拒绝频繁测试
            current_hour = datetime.now().hour
            current_minute = datetime.now().minute
            # 检查是否接近运行时间 (15:30-16:30 之间，考虑16:00的运行时间)
            if (current_hour == 15 and current_minute >= 30) or (current_hour == 16 and current_minute <= 30):
                response = {
                    'status': 'error',
                    'message': '当前接近日常运行时间(16:00)，为避免干扰，请使用 ?force=true 参数强制发送测试邮件'
                }
                json_response = json.dumps(response, ensure_ascii=False, indent=2)
                
                self.send_response(429)  # Too Many Requests
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(json_response.encode('utf-8'))))
                self.end_headers()
                self.wfile.write(json_response.encode('utf-8'))
                return
        
        try:
            # 加载配置并发送测试邮件
            config_path = Path(__file__).parent.parent / 'config' / 'config.yaml'
            with open(config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            
            notifier = EmailNotifier(config)
            
            # 创建测试数据
            import pandas as pd
            test_data = pd.DataFrame([{
                'stock_code': 'TEST',
                'date': datetime.now().strftime('%Y-%m-%d'),
                'close': 100.0,
                'high': 105.0,
                'low': 95.0,
                'open': 98.0,
                'volume': 1000000,
                'ma60': 102.0,
                'pe': 15.0,
                'pb': 2.0,
                'roe': 18.0,
                'debt_ratio': 40.0
            }])
            
            # 发送测试邮件
            notifier.send_alert([
                {
                    'stock_code': 'TEST',
                    'condition': 'test',
                    'price_difference': 2.0,
                    'percentage_difference': 1.96
                }
            ], test_data)
            
            response = {
                'status': 'success',
                'message': '测试邮件已发送',
                'timestamp': datetime.now().isoformat(),
                'receiver': config.get('email', {}).get('receiver_email', '未知')
            }
            
            json_response = json.dumps(response, ensure_ascii=False, indent=2)
            
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(json_response.encode('utf-8'))))
            self.end_headers()
            self.wfile.write(json_response.encode('utf-8'))
            
        except Exception as e:
            logger.error(f"发送测试邮件失败: {e}")
            
            response = {
                'status': 'error',
                'message': f'发送测试邮件失败: {str(e)}',
                'timestamp': datetime.now().isoformat()
            }
            
            json_response = json.dumps(response, ensure_ascii=False, indent=2)
            
            self.send_response(500)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(json_response.encode('utf-8'))))
            self.end_headers()
            self.wfile.write(json_response.encode('utf-8'))
    
    def send_metrics_response(self):
        """发送Prometheus格式的指标"""
        status = self.health_server.get_status() if self.health_server else {}
        
        metrics = [
            '# HELP stock_system_uptime_seconds 系统运行时间（秒）',
            '# TYPE stock_system_uptime_seconds gauge',
            f'stock_system_uptime_seconds {status.get("uptime_seconds", 0)}',
            '',
            '# HELP stock_system_stock_count 监控股票数量',
            '# TYPE stock_system_stock_count gauge',
            f'stock_system_stock_count {status.get("stock_count", 0)}',
            '',
            '# HELP stock_system_cache_size_bytes 缓存目录大小（字节）',
            '# TYPE stock_system_cache_size_bytes gauge',
            f'stock_system_cache_size_bytes {status.get("cache_size_bytes", 0)}',
            '',
            '# HELP stock_system_last_run_timestamp_seconds 最后运行时间戳',
            '# TYPE stock_system_last_run_timestamp_seconds gauge',
            f'stock_system_last_run_timestamp_seconds {status.get("last_run_timestamp", 0)}'
        ]
        
        metrics_text = '\n'.join(metrics)
        
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(metrics_text.encode('utf-8'))))
        self.end_headers()
        self.wfile.write(metrics_text.encode('utf-8'))
    
    def log_message(self, format, *args):
        """重写日志方法，使用我们的logger"""
        logger.debug(f"{self.address_string()} - {format % args}")


class HealthServer:
    """健康检查服务器"""
    
    def __init__(self, config, host='0.0.0.0', port=1933):
        """
        初始化健康服务器
        
        Args:
            config: 系统配置
            host: 监听主机
            port: 监听端口
        """
        self.config = config
        self.host = host
        self.port = port
        self.server = None
        self.thread = None
        self.start_time = time.time()
        
        # 从配置中获取设置
        self.health_config = config.get('health_server', {})
        if 'host' in self.health_config:
            self.host = self.health_config['host']
        if 'port' in self.health_config:
            self.port = self.health_config['port']
    
    def start(self, daemon=True):
        """启动健康服务器"""
        try:
            # 创建自定义Handler工厂
            def handler_factory(*args, **kwargs):
                return HealthHandler(*args, health_server=self, **kwargs)
            
            # 创建HTTP服务器
            self.server = socketserver.TCPServer((self.host, self.port), handler_factory)
            
            # 启动服务器线程
            self.thread = threading.Thread(target=self.server.serve_forever)
            self.thread.daemon = daemon
            
            logger.info(f"健康服务器启动在 {self.host}:{self.port}")
            logger.info(f"  访问 http://{self.host}:{self.port} 查看系统状态")
            logger.info(f"  测试邮件端点: http://{self.host}:{self.port}/test-email")
            
            self.thread.start()
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
    
    def get_status(self):
        """获取系统状态信息"""
        try:
            # 获取服务器信息
            server_info = self._get_server_info()
            hostname = server_info['hostname']
            ip_address = server_info['ip_address']
            kernel_version = server_info['kernel_version']
            system = server_info['system']
            machine = server_info['machine']
            
            # 计算运行时间
            uptime_seconds = time.time() - self.start_time
            uptime_str = self._format_uptime(uptime_seconds)
            
            # 获取目录大小
            project_root = Path(__file__).parent.parent
            cache_dir = project_root / 'cache'
            data_dir = project_root / 'data'
            log_dir = project_root / 'logs'
            
            cache_size = self._get_directory_size(cache_dir)
            data_size = self._get_directory_size(data_dir)
            log_size = self._get_directory_size(log_dir)
            
            # 获取磁盘使用率
            disk_usage = self._get_disk_usage()
            
            # 获取内存使用率
            memory_usage = self._get_memory_usage()
            
            # 从配置获取股票信息
            stock_count = len(self.config.get('stocks', []))
            monitored_stocks = self.config.get('stocks', [])
            
            # 计算下次运行时间
            next_run_time = self._calculate_next_run_time()
            
            return {
                'hostname': hostname,
                'ip_address': ip_address,
                'kernel_version': kernel_version,
                'system': system,
                'machine': machine,
                'python_version': sys.version.split()[0],
                'start_time': datetime.fromtimestamp(self.start_time).strftime('%Y-%m-%d %H:%M:%S'),
                'uptime': uptime_str,
                'uptime_seconds': int(uptime_seconds),
                'cache_size': cache_size,
                'cache_size_bytes': self._get_directory_size_bytes(cache_dir),
                'data_size': data_size,
                'data_size_bytes': self._get_directory_size_bytes(data_dir),
                'log_size': log_size,
                'log_size_bytes': self._get_directory_size_bytes(log_dir),
                'disk_usage': disk_usage,
                'memory_usage': memory_usage,
                'stock_count': stock_count,
                'monitored_stocks': monitored_stocks,
                'last_run_time': self._get_last_run_time(),
                'last_run_timestamp': self._get_last_run_timestamp(),
                'next_run_time': next_run_time,
                'server_url': f"http://{self.host}:{self.port}"
            }
            
        except Exception as e:
            logger.error(f"获取系统状态失败: {e}")
            return {
                'hostname': '未知',
                'ip_address': '未知',
                'kernel_version': '未知',
                'system': '未知',
                'machine': '未知',
                'python_version': sys.version.split()[0],
                'start_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'uptime': '未知',
                'uptime_seconds': 0,
                'cache_size': '未知',
                'cache_size_bytes': 0,
                'data_size': '未知',
                'data_size_bytes': 0,
                'log_size': '未知',
                'log_size_bytes': 0,
                'disk_usage': '未知',
                'memory_usage': '未知',
                'stock_count': 0,
                'monitored_stocks': [],
                'last_run_time': '未知',
                'last_run_timestamp': 0,
                'next_run_time': '未知',
                'server_url': f"http://{self.host}:{self.port}"
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
            except:
                pass
            
            # 方法2: 通过hostname -I命令获取所有IP（Linux）
            try:
                result = subprocess.run(['hostname', '-I'], capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    ips = result.stdout.strip().split()
                    ip_list.extend(ips)
            except:
                pass
            
            # 方法3: 获取公网IP（可选）- 使用HTTPS防止中间人攻击
            try:
                public_ip = urllib.request.urlopen('https://ifconfig.me', timeout=10).read().decode('utf-8').strip()
                # 简单验证IP格式（基本防护）
                import re
                ip_pattern = r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$'
                if public_ip and re.match(ip_pattern, public_ip) and public_ip not in ip_list:
                    ip_list.append(f"{public_ip} (公网)")
                elif public_ip:
                    logger.warning(f"从ifconfig.me获取到非标准IP响应: {public_ip[:50]}...")
            except Exception as e:
                logger.debug(f"获取公网IP失败: {e}")
                pass
            
            # 去重并过滤回环地址
            ip_list = list(set(ip_list))
            ip_list = [ip for ip in ip_list if not ip.startswith('127.')]
            
            if ip_list:
                ip_address = ', '.join(ip_list)
            else:
                ip_address = "无法获取"
            
            # 获取内核版本（Linux系统）
            kernel_version = "未知"
            try:
                # 尝试通过platform模块获取
                kernel_version = platform.release()
                if not kernel_version or kernel_version == "":
                    # 尝试通过uname命令获取
                    result = subprocess.run(['uname', '-r'], capture_output=True, text=True, timeout=5)
                    if result.returncode == 0:
                        kernel_version = result.stdout.strip()
            except:
                # 最后回退到platform.uname
                kernel_version = platform.uname().release
            
            return {
                'hostname': hostname,
                'ip_address': ip_address,
                'kernel_version': kernel_version,
                'system': platform.system(),
                'machine': platform.machine()
            }
        except Exception as e:
            logger.warning(f"获取服务器信息失败: {e}")
            return {
                'hostname': '未知',
                'ip_address': '无法获取',
                'kernel_version': '未知',
                'system': '未知',
                'machine': '未知'
            }
    
    def _format_uptime(self, seconds):
        """格式化运行时间"""
        days = int(seconds // 86400)
        hours = int((seconds % 86400) // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        
        if days > 0:
            return f"{days}天 {hours}小时 {minutes}分钟"
        elif hours > 0:
            return f"{hours}小时 {minutes}分钟 {secs}秒"
        elif minutes > 0:
            return f"{minutes}分钟 {secs}秒"
        else:
            return f"{secs}秒"
    
    def _get_directory_size(self, path):
        """获取目录大小（人类可读格式）"""
        try:
            if not path.exists():
                return "0 B"
            
            total_size = self._get_directory_size_bytes(path)
            
            # 转换为人类可读格式
            for unit in ['B', 'KB', 'MB', 'GB']:
                if total_size < 1024.0:
                    return f"{total_size:.1f} {unit}"
                total_size /= 1024.0
            return f"{total_size:.1f} TB"
        except:
            return "未知"
    
    def _get_directory_size_bytes(self, path):
        """获取目录大小（字节）"""
        try:
            if not path.exists():
                return 0
            
            total_size = 0
            for dirpath, dirnames, filenames in os.walk(path):
                for filename in filenames:
                    filepath = os.path.join(dirpath, filename)
                    if os.path.isfile(filepath):
                        total_size += os.path.getsize(filepath)
            return total_size
        except:
            return 0
    
    def _get_disk_usage(self):
        """获取磁盘使用率"""
        try:
            import shutil
            total, used, free = shutil.disk_usage('/')
            usage_percent = (used / total) * 100
            return f"{usage_percent:.1f}% (已用{used//(1024**3)}GB/总共{total//(1024**3)}GB)"
        except:
            return "未知"
    
    def _get_memory_usage(self):
        """获取内存使用率"""
        try:
            if platform.system() == 'Linux':
                with open('/proc/meminfo', 'r') as f:
                    meminfo = {}
                    for line in f:
                        parts = line.split(':')
                        if len(parts) == 2:
                            meminfo[parts[0].strip()] = parts[1].strip()
                    
                    total = int(meminfo['MemTotal'].split()[0])
                    free = int(meminfo['MemFree'].split()[0])
                    buffers = int(meminfo.get('Buffers', '0 kB').split()[0])
                    cached = int(meminfo.get('Cached', '0 kB').split()[0])
                    
                    used = total - free - buffers - cached
                    usage_percent = (used / total) * 100
                    return f"{usage_percent:.1f}% (已用{used//1024}MB/总共{total//1024}MB)"
            return "未知"
        except:
            return "未知"
    
    def _get_last_run_time(self):
        """获取最后运行时间"""
        try:
            log_file = Path(__file__).parent.parent / 'logs' / 'quant_system.log'
            if log_file.exists():
                with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
                    lines = f.readlines()
                    for line in reversed(lines[-100:]):  # 检查最后100行
                        if '每日股票数据获取和分析任务 开始执行' in line or 'run_daily_task 开始执行' in line:
                            # 提取时间戳
                            import re
                            timestamp_match = re.search(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', line)
                            if timestamp_match:
                                return timestamp_match.group(0)
            return "从未运行"
        except:
            return "未知"
    
    def _get_last_run_timestamp(self):
        """获取最后运行时间戳"""
        last_run_str = self._get_last_run_time()
        if last_run_str == "从未运行" or last_run_str == "未知":
            return 0
        
        try:
            from datetime import datetime
            dt = datetime.strptime(last_run_str, '%Y-%m-%d %H:%M:%S')
            return int(dt.timestamp())
        except:
            return 0
    
    def _calculate_next_run_time(self):
        """计算下次运行时间"""
        try:
            scheduler_config = self.config.get('scheduler', {})
            run_time = scheduler_config.get('run_time', '15:30')
            
            if ':' in run_time:
                hour_str, minute_str = run_time.split(':')
                hour = int(hour_str)
                minute = int(minute_str)
            else:
                hour, minute = 15, 30
            
            now = datetime.now()
            today_run = datetime(now.year, now.month, now.day, hour, minute, 0)
            
            if now < today_run:
                next_run = today_run
            else:
                next_run = today_run.replace(day=today_run.day + 1)
            
            return next_run.strftime('%Y-%m-%d %H:%M:%S')
        except:
            return "未知"


def start_health_server(config_path=None):
    """启动健康服务器（独立运行）"""
    import yaml
    
    # 加载配置
    if config_path is None:
        config_path = Path(__file__).parent.parent / 'config' / 'config.yaml'
    
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    
    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
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