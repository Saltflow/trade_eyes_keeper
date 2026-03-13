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
from datetime import datetime
from pathlib import Path
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

class HealthHandler(http.server.BaseHTTPRequestHandler):
    """HTTP请求处理器"""
    
    def __init__(self, *args, health_server=None, **kwargs):
        self.health_server = health_server
        super().__init__(*args, **kwargs)
    
    def do_GET(self):
        """处理GET请求"""
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
            else:
                self.send_error(404, "Not Found")
        except Exception as e:
            logger.error(f"处理请求 {self.path} 时出错: {e}")
            self.send_error(500, f"Internal Server Error: {e}")
    
    def send_html_response(self):
        """发送HTML格式的状态页面"""
        status = self.health_server.get_status() if self.health_server else {}
        
        html = f"""
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
            </style>
        </head>
        <body>
            <h1>🚀 股票量化系统健康检查</h1>
            
            <p><strong>状态:</strong> <span class="status">运行正常</span></p>
            <p class="timestamp">最后更新: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
            
            <div class="info-box">
                <h2>🖥️ 服务器信息</h2>
                <table class="info-table">
                    <tr>
                        <th>项目</th>
                        <th>值</th>
                    </tr>
                    <tr>
                        <td>主机名</td>
                        <td>{status.get('hostname', '未知')}</td>
                    </tr>
                    <tr>
                        <td>IP地址</td>
                        <td>{status.get('ip_address', '未知')}</td>
                    </tr>
                    <tr>
                        <td>内核版本</td>
                        <td>{status.get('kernel_version', '未知')}</td>
                    </tr>
                    <tr>
                        <td>系统</td>
                        <td>{status.get('system', '未知')} ({status.get('machine', '未知')})</td>
                    </tr>
                    <tr>
                        <td>Python版本</td>
                        <td>{status.get('python_version', sys.version.split()[0])}</td>
                    </tr>
                    <tr>
                        <td>启动时间</td>
                        <td>{status.get('start_time', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}</td>
                    </tr>
                    <tr>
                        <td>运行时间</td>
                        <td>{status.get('uptime', '0秒')}</td>
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
                        <td>{status.get('cache_size', '未知')}</td>
                    </tr>
                    <tr>
                        <td>数据目录大小</td>
                        <td>{status.get('data_size', '未知')}</td>
                    </tr>
                    <tr>
                        <td>日志目录大小</td>
                        <td>{status.get('log_size', '未知')}</td>
                    </tr>
                    <tr>
                        <td>磁盘使用率</td>
                        <td>{status.get('disk_usage', '未知')}</td>
                    </tr>
                    <tr>
                        <td>内存使用率</td>
                        <td>{status.get('memory_usage', '未知')}</td>
                    </tr>
                    <tr>
                        <td>监控股票数量</td>
                        <td>{status.get('stock_count', '未知')}</td>
                    </tr>
                    <tr>
                        <td>最后运行时间</td>
                        <td>{status.get('last_run_time', '未知')}</td>
                    </tr>
                    <tr>
                        <td>下次运行时间</td>
                        <td>{status.get('next_run_time', '未知')}</td>
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
            </div>
            
            <p style="color: #666; margin-top: 30px; font-size: 0.9em;">
                💡 健康服务器运行在端口1933，用于监控系统状态和发送测试邮件。
            </p>
        </body>
        </html>
        """
        
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(html.encode('utf-8'))))
        self.end_headers()
        self.wfile.write(html.encode('utf-8'))
    
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
        
        # 检查是否强制发送
        force = False
        if '?' in self.path:
            query = self.path.split('?')[1]
            if 'force=true' in query:
                force = True
        
        if not force:
            # 如果是日常运行时间，则拒绝频繁测试
            current_hour = datetime.now().hour
            if 14 <= current_hour <= 16:  # 14:00-16:00之间（接近15:30运行时间）
                response = {
                    'status': 'error',
                    'message': '当前接近日常运行时间(15:30)，为避免干扰，请使用 ?force=true 参数强制发送测试邮件'
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
            
            # 方法3: 获取公网IP（可选）
            try:
                public_ip = urllib.request.urlopen('http://ifconfig.me', timeout=10).read().decode('utf-8').strip()
                if public_ip and public_ip not in ip_list:
                    ip_list.append(f"{public_ip} (公网)")
            except:
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