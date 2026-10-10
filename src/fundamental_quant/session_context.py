"""Loss-aware context capacity planning for persistent PDF research sessions.

This module neither calls a model nor mutates storage. Persist full history outside
the active context and commit a checkpoint only after ``compose`` succeeds. Token
counts are estimates unless a provider tokenizer is supplied; byte counting is a
deliberately conservative fallback, never a character clipping mechanism.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping


class ContextCapacityError(ValueError):
    """The request cannot fit without violating retention or evidence contracts."""


def load_token_counter(
    path, expected_sha256: str | None = None
) -> Callable[[str], int]:
    """Load a caller-provisioned official model tokenizer, without network access.

    The caller must match tokenizer and model versions. An absent tokenizer is a
    preflight error, not an excuse to silently shrink the retained-record floor.
    Optional SHA-256 pins the configured resource for reproducible campaigns.
    """
    tokenizer_path = Path(path)
    if not tokenizer_path.is_file():
        raise ContextCapacityError(f"tokenizer_missing:{tokenizer_path}")
    if expected_sha256:
        digest = hashlib.sha256(tokenizer_path.read_bytes()).hexdigest()
        if digest != expected_sha256.lower():
            raise ContextCapacityError("tokenizer_hash_mismatch")
    try:
        from tokenizers import Tokenizer

        tokenizer = Tokenizer.from_file(str(tokenizer_path))
    except Exception as exc:
        raise ContextCapacityError(
            f"tokenizer_unavailable:{type(exc).__name__}"
        ) from exc

    def count(text: str) -> int:
        return len(tokenizer.encode(text, add_special_tokens=False).ids)

    return count


@dataclass(frozen=True)
class ContextLimits:
    context_tokens: int = 1_000_000
    output_tokens: int = 32_768
    retain_record_tokens: int = 8192
    max_images: int = 600
    max_image_bytes: int = 200 * 1024 * 1024
    max_request_bytes: int = 48 * 1024 * 1024
    image_tokens_per_page: int = 1024
    input_margin_tokens: int = 0
    # Account for the compaction task instruction, separately from output reserve.
    compaction_instruction_tokens: int = 1024
    # The checkpoint model may have a different output cap from research/review.
    # None preserves legacy callers that use one output cap for both requests.
    compact_output_tokens: int | None = None

    def __post_init__(self):
        for name, value in vars(self).items():
            if name == "compact_output_tokens" and value is None:
                continue
            if name == "input_margin_tokens" and type(value) is int and value >= 0:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.output_tokens >= self.context_tokens:
            raise ValueError("output_tokens must leave room for input")
        if (
            self.compact_output_tokens is not None
            and self.compact_output_tokens >= self.context_tokens
        ):
            raise ValueError("compact_output_tokens must leave room for input")


@dataclass(frozen=True)
class ContextMeasure:
    estimated_input_tokens: int
    text_tokens: int
    image_tokens: int
    image_count: int
    image_bytes: int
    request_bytes: int


@dataclass(frozen=True)
class CompactionPlan:
    needed: bool
    reasons: tuple
    summary_input: list
    retained_messages: list
    pending_messages: list
    system_messages: list
    original_fingerprint: str
    measurement: ContextMeasure


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


CHECKPOINT_FIELDS = (
    "facts",
    "estimates",
    "citations",
    "conflicts",
    "read_coverage",
    "next_actions",
)


class ContextManager:
    """Plan and validate compaction, keeping messages whole and tool pairs intact.

    ``images`` maps Files API IDs to ``bytes`` (or ``byte_size``), width and height.
    Image content accepts ``image_url: {url: 'file://ID'}`` or
    ``image_file: {file_id: 'ID'}``. Unknown image references fail visibly.
    ``token_counter`` can provide the configured model's exact text tokenizer.
    """

    def __init__(
        self,
        limits: ContextLimits | None = None,
        token_counter: Callable[[str], int] | None = None,
    ):
        self.limits = limits or ContextLimits()
        self.token_counter = token_counter

    def _tokens(self, value: str) -> int:
        result = (
            self.token_counter(value)
            if self.token_counter
            else len(value.encode("utf-8"))
        )
        if isinstance(result, bool) or not isinstance(result, int) or result < 0:
            raise ContextCapacityError("invalid_token_count")
        return result

    def measure(self, messages: list, images: Mapping | None = None) -> ContextMeasure:
        images = images or {}
        image_bytes = image_count = image_tokens = 0
        text_messages = copy.deepcopy(messages)
        for message in text_messages:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for index, part in enumerate(content):
                if not isinstance(part, dict) or part.get("type") not in {
                    "image_url",
                    "image_file",
                    "input_image",
                    "file",
                }:
                    continue
                ref = part.get("file_id")
                if not ref:
                    ref = part.get("image_file", {}).get("file_id")
                if not ref:
                    url = part.get("image_url", {})
                    url = url.get("url") if isinstance(url, dict) else url
                    if isinstance(url, str) and url.startswith("file://"):
                        ref = url[7:]
                metadata = images.get(ref)
                if not metadata:
                    raise ContextCapacityError(f"missing_image_metadata:{ref}")
                size = metadata.get("bytes", metadata.get("byte_size"))
                if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
                    raise ContextCapacityError(f"invalid_image_bytes:{ref}")
                for dimension in ("width", "height"):
                    if (
                        not isinstance(metadata.get(dimension), int)
                        or metadata[dimension] <= 0
                    ):
                        raise ContextCapacityError(f"invalid_image_{dimension}:{ref}")
                image_count += 1
                image_bytes += size
                # Provider-specific high-resolution estimates may exceed default.
                per_image = metadata.get(
                    "estimated_tokens", self.limits.image_tokens_per_page
                )
                if (
                    isinstance(per_image, bool)
                    or not isinstance(per_image, int)
                    or per_image <= 0
                ):
                    raise ContextCapacityError(f"invalid_image_tokens:{ref}")
                image_tokens += max(per_image, self.limits.image_tokens_per_page)
                content[index] = {"type": "text", "text": f"[image:{ref}]"}
        text_tokens = self._tokens(_json(text_messages)) + 8 * len(messages)
        return ContextMeasure(
            text_tokens + image_tokens + self.limits.input_margin_tokens,
            text_tokens,
            image_tokens,
            image_count,
            image_bytes,
            len(_json({"messages": messages}).encode("utf-8")),
        )

    def _reasons(
        self,
        measured: ContextMeasure,
        extra_tokens: int = 0,
        *,
        output_tokens: int | None = None,
    ) -> tuple:
        checks = (
            (
                "context_tokens",
                measured.estimated_input_tokens
                + (
                    self.limits.output_tokens
                    if output_tokens is None
                    else output_tokens
                )
                + extra_tokens,
                self.limits.context_tokens,
            ),
            ("image_count", measured.image_count, self.limits.max_images),
            ("image_bytes", measured.image_bytes, self.limits.max_image_bytes),
            ("request_bytes", measured.request_bytes, self.limits.max_request_bytes),
        )
        return tuple(name for name, used, limit in checks if used > limit)

    @property
    def compact_output_tokens(self) -> int:
        return (
            self.limits.output_tokens
            if self.limits.compact_output_tokens is None
            else self.limits.compact_output_tokens
        )

    def validate_compaction_request(
        self, messages: list, images: Mapping | None = None
    ) -> ContextMeasure:
        """Check the actual wrapped checkpoint request before any paid call.

        ``plan`` only has the history, not the caller's compaction instruction or
        required-reference registry. Those wrappers can exceed its provisional
        allowance in either tokens or serialized bytes. Measure them here with
        the checkpoint output cap, without adding the provisional allowance a
        second time. This method never changes messages or the active history.
        """
        measured = self.measure(messages, images)
        reasons = self._reasons(measured, output_tokens=self.compact_output_tokens)
        if reasons:
            raise ContextCapacityError(
                "compaction_request_cannot_fit:" + ",".join(reasons)
            )
        self._groups(messages)
        return measured

    @staticmethod
    def _groups(messages: list) -> list:
        groups = []
        index = 0
        while index < len(messages):
            message = messages[index]
            if message.get("role") == "tool":
                raise ContextCapacityError("orphan_tool_result")
            group = [message]
            calls = message.get("tool_calls") or []
            if calls:
                expected = {call["id"] for call in calls}
                if len(expected) != len(calls):
                    raise ContextCapacityError("duplicate_tool_call_id")
                found = set()
                while (
                    index + 1 < len(messages)
                    and messages[index + 1].get("role") == "tool"
                ):
                    index += 1
                    tool = messages[index]
                    call_id = tool.get("tool_call_id")
                    if call_id not in expected or call_id in found:
                        raise ContextCapacityError("mismatched_tool_result")
                    found.add(call_id)
                    group.append(tool)
                if found != expected:
                    raise ContextCapacityError("pending_tool_results_must_be_resolved")
            groups.append(group)
            index += 1
        return groups

    def plan(
        self, messages: list, pending: list, images: Mapping | None = None
    ) -> CompactionPlan:
        """Include next batch and output headroom before deciding to compact.

        Latest substantive assistant records are retained as complete messages.
        A supplied tokenizer controls both sizing and the retained token floor.
        Without one the retention floor is estimated; all record messages are
        retained, avoiding any assertion that byte estimates prove 8192 tokens.
        Old image-only turns can still be compacted before those records.
        """
        messages, pending = copy.deepcopy(messages), copy.deepcopy(pending)
        measurement = self.measure(messages + pending, images)
        reasons = self._reasons(measurement)
        fingerprint = hashlib.sha256(_json(messages).encode("utf-8")).hexdigest()
        systems = []
        while messages and messages[0].get("role") in {"system", "developer"}:
            systems.append(messages.pop(0))
        if any(m.get("role") in {"system", "developer"} for m in messages + pending):
            raise ContextCapacityError("nonleading_system_message")
        # Validate combined history so pending may contain the last tool result.
        self._groups(messages + pending)
        if not reasons:
            return CompactionPlan(
                False, (), [], messages, pending, systems, fingerprint, measurement
            )
        groups = self._groups(messages)
        split = len(groups)
        retained_tokens = 0
        for index in range(len(groups) - 1, -1, -1):
            group = groups[index]
            records = [
                m
                for m in group
                if m.get("role") == "assistant"
                and m.get("content")
                and not m.get("tool_calls")
            ]
            if records:
                retained_tokens += sum(
                    self._tokens(
                        m["content"]
                        if isinstance(m["content"], str)
                        else "".join(
                            part.get("text", "")
                            for part in m["content"]
                            if isinstance(part, dict)
                        )
                    )
                    for m in records
                )
                split = index
                if (
                    self.token_counter
                    and retained_tokens >= self.limits.retain_record_tokens
                ):
                    break
        # With no substantive records retain the latest group, never a partial turn.
        if split == len(groups) and groups:
            split = len(groups) - 1
        if self.token_counter is None:
            # Without an actual tokenizer, a UTF-8 upper bound cannot establish
            # a lower bound of 8192 retained tokens. Preserve every substantive
            # record, but permit old image-only turns to leave active context.
            keep = {
                index
                for index, group in enumerate(groups)
                if any(m.get("role") == "assistant" and m.get("content") for m in group)
            }
            if groups:
                keep.add(len(groups) - 1)
            old = [m for i, group in enumerate(groups) if i not in keep for m in group]
            tail = [m for i, group in enumerate(groups) if i in keep for m in group]
        else:
            old = [message for group in groups[:split] for message in group]
            tail = [message for group in groups[split:] for message in group]
        if not old:
            raise ContextCapacityError("no_compactable_history:" + ",".join(reasons))
        tail_reasons = self._reasons(self.measure(systems + tail + pending, images))
        if tail_reasons:
            raise ContextCapacityError(
                "retained_context_cannot_fit:" + ",".join(tail_reasons)
            )
        compact_reasons = self._reasons(
            self.measure(systems + old, images),
            self.limits.compaction_instruction_tokens,
            output_tokens=self.compact_output_tokens,
        )
        if compact_reasons:
            raise ContextCapacityError(
                "compaction_request_cannot_fit:" + ",".join(compact_reasons)
            )
        return CompactionPlan(
            True,
            reasons,
            systems + old,
            tail,
            pending,
            systems,
            fingerprint,
            measurement,
        )

    def compose(
        self,
        plan: CompactionPlan,
        checkpoint: dict | None = None,
        images: Mapping | None = None,
        allowed_refs=None,
        required_refs=None,
    ) -> list:
        """Validate a model checkpoint and return a NEW active context.

        References and required dependencies must come from the durable evidence
        registry. Schema validation is not a claim of semantic summary fidelity.
        Full history and previous checkpoint must remain until atomic commit.
        """
        messages = copy.deepcopy(plan.system_messages)
        if plan.needed:
            if not isinstance(checkpoint, dict):
                raise ContextCapacityError("missing_checkpoint")
            for field in CHECKPOINT_FIELDS:
                if not isinstance(checkpoint.get(field), list):
                    raise ContextCapacityError(f"checkpoint_missing_field:{field}")
            citations = checkpoint["citations"]
            if not all(isinstance(ref, str) and ref for ref in citations):
                raise ContextCapacityError("invalid_checkpoint_citations")
            if allowed_refs is None:
                raise ContextCapacityError("checkpoint_requires_reference_registry")
            if not set(citations).issubset(set(allowed_refs)):
                raise ContextCapacityError("checkpoint_unknown_reference")
            if not set(required_refs or ()).issubset(set(citations)):
                raise ContextCapacityError("checkpoint_dropped_required_reference")
            messages.append(
                {
                    "role": "user",
                    "content": _json(
                        {
                            "type": "research_checkpoint",
                            "version": 1,
                            "history_fingerprint": plan.original_fingerprint,
                            "checkpoint": checkpoint,
                        }
                    ),
                }
            )
        messages.extend(copy.deepcopy(plan.retained_messages))
        messages.extend(copy.deepcopy(plan.pending_messages))
        reasons = self._reasons(self.measure(messages, images))
        if reasons:
            raise ContextCapacityError(
                "composed_context_cannot_fit:" + ",".join(reasons)
            )
        self._groups(messages)
        return messages
