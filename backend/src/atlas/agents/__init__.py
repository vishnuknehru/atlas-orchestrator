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
from atlas.agents.boundary import AgentBoundary, SeatbeltBoundary, get_boundary
from atlas.agents.cli import CliAdapter, FakeAdapter, adapter_for

__all__ = [
    "AgentAdapter",
    "AgentBoundary",
    "AgentRequest",
    "AgentResult",
    "CliAdapter",
    "FailureKind",
    "FakeAdapter",
    "SeatbeltBoundary",
    "adapter_for",
    "classify_failure",
    "get_boundary",
    "redact",
    "scrubbed_env",
]
