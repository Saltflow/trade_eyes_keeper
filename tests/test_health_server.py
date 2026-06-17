"""健康服务器并发 & 韧性测试。"""

import socket
import threading
import time
import urllib.request

import pytest

from src.health_server.core.health_server import HealthServer


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _get(url, timeout=5):
    resp = urllib.request.urlopen(url, timeout=timeout)
    return resp.status, resp.read().decode()


class TestConcurrency:
    def test_concurrent_requests_do_not_block(self):
        """两个并发请求都能收到响应。"""
        port = _free_port()
        server = HealthServer({}, port=port)
        server.start(daemon=True)
        time.sleep(0.5)
        url = f"http://127.0.0.1:{port}/health"

        results = []

        def worker():
            try:
                s, body = _get(url)
                results.append(s)
            except Exception as e:
                results.append(str(e))

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        server.stop()
        assert all(r == 200 for r in results), f"results={results}"
        assert len(results) == 2

    def test_bad_connection_does_not_block_other_requests(self):
        """一个不发数据的坏连接不阻塞 /health。"""
        port = _free_port()
        server = HealthServer({}, port=port)
        server.start(daemon=True)
        time.sleep(0.5)

        # 打开一个 TCP 连接但不发任何数据
        bad = socket.create_connection(("127.0.0.1", port), timeout=3)
        time.sleep(0.5)

        # 正常请求必须在 2s 内返回
        status, body = _get(f"http://127.0.0.1:{port}/health", timeout=2)
        bad.close()
        server.stop()

        assert status == 200
        assert "OK" in body.upper()

    def test_server_still_accepts_after_bad_disconnect(self):
        """坏连接断开后服务器正常接受新请求。"""
        port = _free_port()
        server = HealthServer({}, port=port)
        server.start(daemon=True)
        time.sleep(0.5)

        # 打开并立即关闭（模拟 RST）
        for _ in range(3):
            s = socket.create_connection(("127.0.0.1", port), timeout=3)
            s.close()

        time.sleep(0.3)
        status, _ = _get(f"http://127.0.0.1:{port}/health", timeout=3)
        server.stop()
        assert status == 200


class TestWatchdog:
    def test_watchdog_not_triggered_when_healthy(self):
        """正常运行时 watchdog 不触发。"""
        port = _free_port()
        server = HealthServer({}, port=port)
        server.start(daemon=True)
        time.sleep(0.5)

        # watchdog 应该没失败
        assert server._watchdog_failures == 0
        server.stop()

    def test_watchdog_failure_counter_increments(self):
        """模拟失败计数递增。"""
        port = _free_port()
        server = HealthServer({}, port=port)
        server._watchdog_failures = 0
        server._watchdog_step(max_failures=3)
        assert server._watchdog_failures == 1
        server._watchdog_step(max_failures=3)
        assert server._watchdog_failures == 2
