from pathlib import Path
import subprocess
import sys


def test_importing_llm_client_preserves_standard_json_contract():
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            """
import io
import json
original_dumps, original_dump = json.dumps, json.dump
from src.data.llm.base import BaseLLMClient
assert json.dumps is original_dumps
assert json.dump is original_dump
value = {"label": "\\u4e2d\\u6587"}
assert "\\\\u4e2d" in json.dumps(value, ensure_ascii=True)
assert "\\\\u4e2d" not in json.dumps(value, ensure_ascii=False)
stream = io.StringIO()
json.dump(value, stream, ensure_ascii=True)
assert "\\\\u4e2d" in stream.getvalue()
assert BaseLLMClient._calculate_data_hash({"close": 1.234})
""",
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
