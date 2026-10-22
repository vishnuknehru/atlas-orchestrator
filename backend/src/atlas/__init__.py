"""ATLAS — Autonomous Multi-Agent Software Engineering Orchestrator."""

from __future__ import annotations

import os

# Enforce ADR-006 §1 zero-egress tracing disablement immediately on import.
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ.pop("LANGSMITH_API_KEY", None)
os.environ.pop("LANGCHAIN_API_KEY", None)

__version__ = "0.1.0"
