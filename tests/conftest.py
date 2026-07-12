"""
Pytest configuration for stock quantitative system tests.
This file is automatically discovered by pytest and runs before any tests.
"""
import logging
import os
import sys

import pytest

# Add project root to Python path so we can import src modules
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)


@pytest.fixture(autouse=True)
def _detach_stream_log_handlers():
    """防止测试中 StreamHandler 绑定到 pytest 捕获流后，
    在 teardown 关闭该流引发 'I/O operation on closed file'。

    每个测试结束后，移除 root logger 上绑定到已失效 stdout/stderr 的
    StreamHandler（FileHandler 不受影响）。
    """
    yield
    root = logging.getLogger()
    for h in list(root.handlers):
        # 仅处理纯 StreamHandler（FileHandler 是其子类，需排除）
        if isinstance(h, logging.StreamHandler) and not isinstance(
            h, logging.FileHandler
        ):
            stream = getattr(h, "stream", None)
            if stream is not None and getattr(stream, "closed", False):
                root.removeHandler(h)

