"""Provider-neutral errors and stable protocol serialization."""

import json


class AgentRuntimeError(RuntimeError):
    """A bounded agent turn failed without granting additional capabilities."""


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
