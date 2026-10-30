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
def _isolate_unit_transport_flags(monkeypatch, request):
    """Unit tests choose their own mocked channel state, not the caller's flags."""
    if any(
        request.node.get_closest_marker(mark)
        for mark in ("smoke", "integration", "e2e")
    ):
        return
    for name in (
        "SKIP_NOTIFICATIONS",
        "SKIP_EMAIL",
        "SKIP_FEISHU",
        "SKIP_TELEGRAM",
    ):
        monkeypatch.delenv(name, raising=False)


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


@pytest.fixture
def synthetic_peer_package(tmp_path):
    """Self-contained approved request; no private acceptance archive or finance."""
    from pathlib import Path

    from scripts import run_company_summary as transport

    repo = Path(project_root)
    source = tmp_path / "synthetic_peer_source"
    evidence = source / "source_evidence.txt"
    transport.save(evidence, "SYNTHETIC TEST ONLY\r\nNo financial facts or advice.")
    request = {
        "model": "deepseek-flash",
        "messages": [
            {
                "role": "system",
                "content": "SYNTHETIC OFFLINE TEST. Not financial research.",
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": evidence.read_bytes().decode("utf8")},
                    {"type": "image_url", "image_url": {"url": "synthetic-test-image"}},
                ],
            },
        ],
        "thinking": {"type": "enabled"},
        "max_tokens": 131072,
        "response_format": {"type": "text"},
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    preview = source / "request.preview.json"
    transport.save(preview, request)
    transport.save(
        source / "调用规划.json",
        {"source_hashes": {str(evidence): transport.sha(evidence)}},
    )
    transport.save(
        source / "前置阶段保护.json",
        {str(evidence): transport.sha(evidence)},
    )
    transport.save(
        source / "准备产物哈希.json",
        {preview.name: transport.sha(preview)},
    )
    return repo, source


@pytest.fixture
def synthetic_summary_package(tmp_path):
    """Freeze five recoveries and three summaries using labelled fake sources."""
    from pathlib import Path

    from scripts import run_company_summary as transport

    repo = Path(project_root)
    config = transport.load(repo / "config/company_summary_contract.json")
    source = tmp_path / "synthetic_summary_source"
    evidence = source / "source_evidence.txt"
    transport.save(evidence, "SYNTHETIC TEST ONLY: no company or investment data.")
    jobs = []
    companies = {"TESTA": 2, "TESTB": 2, "TESTC": 1}
    for company, recoveries in companies.items():
        dependencies = [f"{company}_recovery_{i + 1}" for i in range(recoveries)]
        specifications = [(name, "recovery", []) for name in dependencies]
        specifications.append((f"{company}_summary", "summary", dependencies))
        for name, kind, required in specifications:
            text = "SYNTHETIC TEST ONLY\r\n" + name
            for dependency in required:
                text += (
                    "\n{{RECOVERY_CONTENT:" + dependency + "}}"
                    "\n{{RECOVERY_STATUS:" + dependency + "}}"
                )
            request = {
                "model": "deepseek-flash",
                "messages": [
                    {"role": "system", "content": "Offline transport test only."},
                    {"role": "user", "content": [{"type": "text", "text": text}]},
                ],
                "max_tokens": config[kind + "_max_tokens"],
                "response_format": {"type": "text"},
                "stream": True,
            }
            template = source / name / "request.template.json"
            transport.save(template, request)
            jobs.append(
                {
                    "id": name,
                    "kind": kind,
                    "dependencies": required,
                    "sources": [{"fixture": "synthetic", "source": str(evidence)}],
                    "request_sha256": transport.sha(template),
                    "request_bytes": template.stat().st_size,
                    "dependency_token_reserve": len(required)
                    * (config["recovery_max_tokens"] + 512),
                }
            )
    transport.save(source / "调用计划.json", {"jobs": jobs})
    transport.save(
        source / "冻结输入哈希.json", {str(evidence): transport.sha(evidence)}
    )
    relative = "scripts/run_company_summary.py"
    transport.save(
        source / "准备文件哈希.json", {relative: transport.sha(repo / relative)}
    )
    return repo, source
