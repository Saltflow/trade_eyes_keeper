"""Project visibility from registered tools and existing authoritative records."""

import time

from ...core.runtime_service import read_service_status

REPOSITORY_TOOLS = frozenset(
    {
        "repository_map",
        "list_repository_files",
        "search_repository",
        "read_repository_file",
        "repository_symbols",
        "list_project_docs",
        "search_project_docs",
        "read_project_doc",
    }
)
PROJECT_TOOLS = REPOSITORY_TOOLS | {
    "project_capabilities",
    "project_status",
    "query_tasks",
    "read_configuration",
    "propose_configuration",
    "bot_command",
}
MATERIAL_TOOLS = frozenset(
    {
        "data_find",
        "data_read",
        "data_search",
        "list_datasets",
        "get_dataset_details",
        "list_dataset_files",
        "query_dataset_documents",
        "read_dataset_text",
        "fetch_a_share_report",
        "search_report_text",
        "search_web",
        "fetch_web_source",
    }
)
GROUPS = (
    "报告与邮件",
    "触发器",
    "持仓与账户",
    "财务与投研",
    "资料与联网取证",
    "项目管理",
)


def tool_group(name):
    if name.startswith("report_"):
        return GROUPS[0]
    if name == "trigger_manage":
        return GROUPS[1]
    if name.startswith("personal_monitor_"):
        return GROUPS[2]
    if name in MATERIAL_TOOLS:
        return GROUPS[4]
    if name in PROJECT_TOOLS:
        return GROUPS[5]
    return GROUPS[3]


def tool_allowed(name, capabilities):
    if capabilities is None:
        return True
    if name in {
        "project_capabilities",
        "query_tasks",
        "data_find",
        "data_read",
        "data_search",
        "report_find",
        "report_get",
    }:
        return True
    if name == "trigger_manage":
        return bool({"personal", "triggers"} & capabilities)
    if name.startswith("personal_monitor_"):
        return "personal" in capabilities
    if name in REPOSITORY_TOOLS | {"project_status"}:
        return bool({"repository", "research"} & capabilities)
    # Preserve the established research profile's business permissions.
    return "research" in capabilities


def capabilities_result(schemas, available):
    enabled = {item["function"]["name"] for item in available}
    groups = {group: [] for group in GROUPS}
    for item in schemas:
        fn = item["function"]
        groups[tool_group(fn["name"])].append(
            {
                "name": fn["name"],
                "description": fn["description"],
                "available": fn["name"] in enabled,
                "reason": None
                if fn["name"] in enabled
                else "not_enabled_in_this_profile",
            }
        )
    return {
        "groups": groups,
        "counts": {
            "total": sum(len(items) for items in groups.values()),
            "available": len(enabled),
            "by_group": {
                group: {
                    "total": len(items),
                    "available": sum(item["available"] for item in items),
                }
                for group, items in groups.items()
            },
        },
        "scope": "current_authenticated_profile",
        "notice": "工具开放不等于数据齐全或服务健康；需继续查询实际记录。",
    }


def project_status(config):
    from .data_tools import _safe

    status = read_service_status(config)
    fields = (
        "state",
        "ready",
        "fresh",
        "updated_at",
        "started_at",
        "build_info",
        "execution",
        "module_health",
        "bot_threads_alive",
        "phase",
        "failure",
    )
    return {
        "source": "service_status.json",
        **_safe({key: status[key] for key in fields if key in status}),
        "coverage_gaps": []
        if status.get("fresh")
        else ["service_heartbeat_unavailable_or_stale"],
    }


def query_tasks(store, chat, sender, *, readers, source="all", limit=20, offset=0):
    """Page each authoritative source separately; no synthetic task database."""
    from .data_tools import _safe

    if (
        type(limit) is not int
        or not 1 <= limit <= 100
        or type(offset) is not int
        or offset < 0
    ):
        raise ValueError("invalid task page")
    if source not in {"all", "proposals", "reports", "triggers", "research"}:
        raise ValueError("unknown task source")
    if source not in {"all", "proposals", *readers}:
        return {
            "sources": {
                source: {
                    "status": "unavailable",
                    "reason": "not_enabled_in_this_profile",
                }
            },
            "scope": "authenticated_owner_chat",
        }

    def proposals():
        rows = store.recent(chat, sender, limit=limit + 1, offset=offset)
        return {
            "items": [proposal_view(row) for row in rows[:limit]],
            "next_offset": offset + limit if len(rows) > limit else None,
            "coverage_gaps": [],
        }

    sources = {"proposals": proposals, **readers}
    result = {}
    for name, read in sources.items():
        if source not in {"all", name}:
            continue
        try:
            result[name] = {"status": "ok", **_safe(read())}
        except Exception as exc:  # noqa: BLE001 - preserve partial source failures
            result[name] = {
                "status": "failed",
                "reason": type(exc).__name__,
                "coverage_gaps": ["source_query_failed"],
            }
    return {
        "sources": result,
        "scope": "authenticated_owner_chat",
        "notice": "按来源分页；提案与执行是关联记录，不可将各来源数量相加为执行总数。",
    }


def proposal_view(item):
    """Show expired previews without changing durable confirmation state."""
    from .data_tools import _safe

    result = {
        key: item.get(key)
        for key in (
            "id",
            "kind",
            "status",
            "note",
            "created",
            "updated",
            "expires",
            "result",
        )
    }
    if (
        item["status"] in {"pending", "previewing", "preview_failed"}
        and item.get("expires", float("inf")) < time.time()
    ):
        result.update(status="expired", stored_status=item["status"])
    return _safe(result)
