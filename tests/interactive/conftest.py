"""Isolated host services use the repository's real Pi subprocess bridge."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from src.interactive.assistant.harness import ConversationHarness


@pytest.fixture(autouse=True)
def isolated_shared_authority(tmp_path, monkeypatch):
    """HTTP fixtures must never poison another test's durable source/account state."""
    monkeypatch.setenv("TRADE_EYES_AUTHORITY_ROOT", str(tmp_path / "authority"))


@pytest.fixture
def pi_node():
    executable = os.environ.get("FEISHU_PI_TEST_NODE") or shutil.which("node")
    assert executable, "Pi tests require Node >=22.19; set FEISHU_PI_TEST_NODE"
    result = subprocess.run(
        [executable, "--version"], capture_output=True, text=True, check=True, timeout=5
    )
    version = tuple(int(part) for part in result.stdout.strip().lstrip("v").split("."))
    assert version >= (22, 19), "Set FEISHU_PI_TEST_NODE to Node >=22.19"
    return executable


@pytest.fixture
def real_pi_bridge(monkeypatch, pi_node):
    """Keep temporary business roots while loading the actual installed Pi bridge."""
    original_init = ConversationHarness.__init__
    bridge = Path(__file__).resolve().parents[2] / "tools/feishu-pi/bridge.mjs"

    def initialize(harness, *args, **kwargs):
        original_init(harness, *args, **kwargs)
        harness.bridge_path = bridge
        harness.node_executable = pi_node

    monkeypatch.setattr(ConversationHarness, "__init__", initialize)


@pytest.fixture
def text_pdf_bytes():
    """Small valid PDF with selectable text; parsed by the real native backend."""
    stream = b"BT /F1 12 Tf 40 100 Td (Archived report evidence 123.45) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length "
        + str(len(stream)).encode()
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(pdf)
    pdf.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode())
    pdf.extend(f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
    return bytes(pdf)
