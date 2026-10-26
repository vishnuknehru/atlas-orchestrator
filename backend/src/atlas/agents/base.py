"""Adapter contract, result types, secret redaction and environment scrubbing."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

FailureKind = Literal[
    "not_installed",
    "timeout",
    "auth_expired",
    "rate_limit",
    "nonzero_exit",
    "unsupported_adapter",
    "no_agent_boundary",
]

# Only these variables reach an agent process. Credentials such as GH_TOKEN or *_API_KEY are dropped.
_ENV_ALLOWLIST = frozenset({"PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TMPDIR", "LANG", "TZ"})
_ENV_ALLOW_PREFIXES = ("LC_", "XDG_")

_RATE_LIMIT = re.compile(r"rate.?limit|usage limit|too many requests|\b429\b|quota", re.IGNORECASE)
_AUTH = re.compile(
    r"not logged in|log ?in again|please (run )?.*login|unauthori[sz]ed|authentication|invalid api key|\b401\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AgentRequest:
    """One read-only agent invocation. `cwd` must be a scratch directory, never the target repository."""

    agent_id: str
    role: str
    prompt: str
    cwd: Path
    timeout_seconds: int = 300
    # Writer roles: the agent may edit files, but only inside `cwd`, which must be its own worktree.
    writable: bool = False


@dataclass(frozen=True)
class AgentResult:
    ok: bool
    output: str = ""
    failure: FailureKind | None = None
    detail: str = ""
    exit_code: int | None = None

    @staticmethod
    def success(output: str, exit_code: int | None = 0) -> AgentResult:
        return AgentResult(ok=True, output=output, exit_code=exit_code)

    @staticmethod
    def failed(kind: FailureKind, detail: str, exit_code: int | None = None) -> AgentResult:
        return AgentResult(ok=False, failure=kind, detail=detail, exit_code=exit_code)


class AgentAdapter(Protocol):
    def run(self, request: AgentRequest) -> AgentResult: ...


def scrubbed_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    env = os.environ if source is None else source
    return {k: v for k, v in env.items() if k in _ENV_ALLOWLIST or k.startswith(_ENV_ALLOW_PREFIXES)}


def redact(text: str, secrets: list[str]) -> str:
    """Replace exact secret values (empty and very short ones are ignored to avoid mangling output)."""
    for secret in secrets:
        if len(secret) >= 8:
            text = text.replace(secret, "[REDACTED]")
    return text


def classify_failure(exit_code: int, stdout: str, stderr: str) -> AgentResult:
    """Map a non-zero exit to a typed failure so callers can pause (limits) or ask for login (auth)."""
    combined = f"{stderr}\n{stdout}"
    lines = [ln for ln in combined.strip().splitlines() if ln.strip()]
    detail = lines[-1][:300] if lines else "no output"
    if _RATE_LIMIT.search(combined):
        return AgentResult.failed("rate_limit", detail, exit_code)
    if _AUTH.search(combined):
        return AgentResult.failed("auth_expired", detail, exit_code)
    return AgentResult.failed("nonzero_exit", detail, exit_code)
