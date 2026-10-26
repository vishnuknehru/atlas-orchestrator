"""Agent adapters: one place that launches agent CLIs and turns their outcome into a typed result."""

from atlas.agents.base import (
    AgentAdapter,
    AgentRequest,
    AgentResult,
    FailureKind,
    classify_failure,
    redact,
    scrubbed_env,
)
from atlas.agents.cli import CliAdapter, FakeAdapter, adapter_for

__all__ = [
    "AgentAdapter",
    "AgentRequest",
    "AgentResult",
    "CliAdapter",
    "FailureKind",
    "FakeAdapter",
    "adapter_for",
    "classify_failure",
    "redact",
    "scrubbed_env",
]
