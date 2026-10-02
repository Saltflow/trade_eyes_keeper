"""Assistant-only settings, separate from dividend extraction budgets."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, StrictBool, constr


class AssistantSettings(BaseModel):
    enabled: StrictBool = False
    model: constr(strip_whitespace=True, min_length=1, max_length=128) = (
        "deepseek-flash"
    )
    reasoning_effort: Literal["low", "high", "max"] = "high"
    request_timeout_seconds: int = Field(60, ge=5, le=120)
    request_retries: int = Field(1, ge=0, le=2)
    max_tokens: int = Field(16384, ge=512, le=32768)
    max_tool_rounds: int = Field(6, ge=1, le=16)
    max_history_messages: int = Field(20, ge=2, le=80)
    session_ttl_seconds: int = Field(1800, ge=60, le=86400)
    conversation_retention_days: int = Field(30, ge=1, le=365)
    harness_backend: Literal["native", "pi"] = "native"
    max_tool_calls: int = Field(48, ge=1, le=64)
    max_context_chars: int = Field(96000, ge=16000, le=200000)
    conversation_timeout_seconds: int = Field(300, ge=30, le=600)
    stock_data_auto_backfill: StrictBool = True
    stock_data_backfill_cooldown_seconds: int = Field(1800, ge=60, le=86400)
    stock_data_backfills_per_hour: int = Field(6, ge=1, le=60)
    proposal_ttl_seconds: int = Field(900, ge=60, le=3600)
    workspace_root: str = "data/feishu_research"
    docker_image: str = "trade-eyes-research:local"
    cpus: float = Field(2, gt=0, le=32)
    memory_mb: int = Field(4096, ge=512, le=65536)
    prepare_memory_mb: int | None = Field(None, ge=512, le=65536)
    prepare_min_available_mb: int = Field(128, ge=64, le=65536)
    timeout_seconds: int = Field(3600, ge=10, le=14400)
    prepare_timeout_seconds: int = Field(3600, ge=10, le=14400)
    max_output_mb: int = Field(100, ge=1, le=1024)

    class Config:
        extra = "forbid"
        validate_assignment = True

    @classmethod
    def from_config(cls, config: dict):
        value = (config.get("interactive", {}).get("feishu", {}) or {}).get(
            "assistant", {}
        )
        return cls.parse_obj(value or {})


PROJECT_ROOT = Path(__file__).resolve().parents[3]
