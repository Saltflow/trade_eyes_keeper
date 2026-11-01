"""Feishu host policy for the shared provider-neutral agent loop."""

# Shared standard modules retained for compatible instrumentation/patch points.
import shutil  # noqa: F401
import subprocess  # noqa: F401
import time  # noqa: F401
from pathlib import Path

from ...agent_runtime.harness import (
    ConversationHarness as _PortableHarness,
)
from ...agent_runtime.harness import (
    compact_messages as _compact_messages,
)
from .client import AssistantAPIError

RETRIEVAL_TOOLS = frozenset(
    {
        "get_dataset_details",
        "query_dataset_documents",
        "fetch_a_share_report",
        "search_report_text",
        "read_dataset_text",
        "search_repository",
        "read_repository_file",
        "search_project_docs",
        "read_project_doc",
        "read_configuration",
        "query_research_data",
        "query_stock_fundamentals",
        "search_web",
        "calculate_gordon_ke_sensitivity",
        "query_tasks",
        "research_pipeline_status",
        "research_pipeline_tasks",
        "research_pipeline_logs",
        "research_pipeline_artifacts",
    }
)


def compact_messages(messages, limit):
    return _compact_messages(messages, limit, error_type=AssistantAPIError)


class ConversationHarness(_PortableHarness):
    def __init__(self, client, settings, root, stop_event):
        root = Path(root)
        super().__init__(
            client,
            settings,
            root,
            stop_event,
            bridge_path=root / "tools/feishu-pi/bridge.mjs",
            retrieval_tools=RETRIEVAL_TOOLS,
            repeatable_tools={"research_pipeline_status", "research_pipeline_logs"},
            repeatable_prefixes=("personal_monitor_",),
            require_pi_for_retrieval=True,
            error_type=AssistantAPIError,
        )
