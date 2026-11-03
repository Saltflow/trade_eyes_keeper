"""Explicitly synthetic agent and container IO; never financial acceptance."""

import json

from src.research_pipeline.common import atomic_write, file_sha

MODEL_SOURCE = """import json
from pathlib import Path
x = json.loads(Path("/input/inputs.json").read_text())
profit = x["units"] * (x["price"] - x["unit_cost"])
result = {"profit": profit, "eps": profit / x["shares"],
          "checks": [{"name": "positive_denominator", "passed": x["shares"] > 0}]}
Path("results.json").write_text(json.dumps(result))
"""


def sandbox_result(self, inputs, output, *, cancel, deadline):
    """Simulates container IO only; business-value acceptance is separate."""
    values = json.loads((inputs / "inputs.json").read_text("utf-8"))
    profit = values["units"] * (values["price"] - values["unit_cost"])
    path = output / "files/results.json"
    atomic_write(
        path,
        {
            "profit": profit,
            "eps": profit / values["shares"],
            "checks": [
                {"name": "positive_denominator", "passed": values["shares"] > 0}
            ],
            "simulated_fixture": True,
        },
    )
    return {
        "status": "computed",
        "checks_status": "passed",
        "files": {"results.json": file_sha(path)},
        "simulated": True,
        "financial_validation": "not_independently_verified",
    }


def agent_response(request):
    if not request["messages"][0]["content"].startswith("投研原生工具调用："):
        return None
    receipts = []
    original = None
    for message in request["messages"]:
        text = message.get("content", "")
        if not isinstance(text, str):
            continue
        if message["role"] == "tool":
            receipts.append(json.loads(text))
        elif message["role"] == "user" and text.startswith('{"companies"'):
            original = json.loads(text)
    if not receipts:
        name, args = (
            "read_source",
            {
                "source_id": original["sources"]["results"][0]["source_id"],
                "start": 1,
                "count": 3,
            },
        )
    elif len(receipts) == 1:
        name, args = (
            "save_model",
            {
                "python": MODEL_SOURCE,
                "inputs": {"units": 4, "price": 7, "unit_cost": 3, "shares": 2},
                "explanation": "Synthetic IO fixture; not a real issuer model.",
                "evidence": [
                    {"source_id": receipts[0]["source_id"], "start": 1, "end": 1}
                ],
                "assumptions": {"all_values": "fictional execution fixture only"},
            },
        )
    elif len(receipts) == 2:
        name, args = "run_model", {"model_id": receipts[1]["model_id"]}
    elif len(receipts) == 3:
        name, args = "select_model", {"model_id": receipts[1]["model_id"]}
    else:
        return {
            "content": "Offline synthetic research handoff, not financial acceptance.",
            "tool_calls": [],
        }
    if name == "save_model" and original.get("revision") == "review_rework":
        args["inputs"]["shares"] = 3
    calls = [
        {
            "id": f"tool_{len(receipts)}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }
    ]
    if name == "select_model":
        calls.insert(
            0,
            {
                "id": "read_calculated_result",
                "type": "function",
                "function": {"name": "read_result", "arguments": json.dumps(args)},
            },
        )
    return {
        "content": "Offline investigation tool IO fixture.",
        "tool_calls": calls,
        "reasoning_content": "Synthetic provider continuation field.",
    }
