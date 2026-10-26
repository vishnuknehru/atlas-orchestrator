"""CLI-backed adapters for Claude Code and Codex, run inside the OS AgentBoundary, plus a replay adapter."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path

from atlas.agents.base import (
    AgentAdapter,
    AgentRequest,
    AgentResult,
    classify_failure,
    redact,
    scrubbed_env,
)
from atlas.agents.boundary import AgentBoundary, get_boundary
from atlas.config.schemas import AgentConfigEntry


class CliAdapter:
    """Runs `claude -p` or `codex exec` once. Read-only unless the request is writable (then cwd is a worktree)."""

    def __init__(
        self,
        agent: AgentConfigEntry,
        *,
        secrets: Sequence[str] = (),
        boundary: AgentBoundary | None = None,
    ) -> None:
        self._agent = agent
        self._secrets = list(secrets)
        self._boundary = boundary if boundary is not None else get_boundary()

    def _command(self, request: AgentRequest, out_file: str) -> list[str] | None:
        if self._agent.adapter == "claude_cli":
            cmd = ["claude", "-p", "--output-format", "text", "--no-session-persistence"]
            if self._agent.models:
                cmd += ["--model", self._agent.models[0]]
            if request.writable:
                # No Bash until Claude's own OS sandbox is verified (security-model.md section 5.2).
                cmd += ["--permission-mode", "acceptEdits", "--allowedTools", "Read,Edit,Write,Grep,Glob"]
            return cmd
        if self._agent.adapter == "codex_cli":
            # Model is deliberately not passed: the CLI default is the one that works on the user's login.
            return [
                "codex", "exec", "-s", "read-only", "--skip-git-repo-check", "--ephemeral",
                "--ignore-rules", "-C", str(request.cwd), "-o", out_file, "-",
            ]
        return None

    def run(self, request: AgentRequest) -> AgentResult:
        with tempfile.TemporaryDirectory(prefix="atlas-agent-out-") as out_dir:
            # Outside the worktree, so an agent's answer never lands in the candidate diff.
            return self._run(request, Path(out_dir) / "agent-output.txt")

    def _run(self, request: AgentRequest, out_file: Path) -> AgentResult:
        cmd = self._command(request, str(out_file))
        if cmd is None:
            return AgentResult.failed(
                "unsupported_adapter", f"{self._agent.adapter} cannot be run automatically by ATLAS"
            )
        env = scrubbed_env()
        if shutil.which(cmd[0], path=env.get("PATH")) is None:
            return AgentResult.failed("not_installed", f"The {cmd[0]!r} command is not installed or not on PATH.")
        if request.writable and self._agent.adapter == "codex_cli":
            # Verified 2026-10-26: Codex's own sandbox cannot start inside the AgentBoundary, and running it
            # without its sandbox would trade its no-network rule for ours. Blocked until that is decided
            # (security-model.md section 5.1.9). docs/evidence/agent-boundary-spike-2026-10-26.md
            return AgentResult.failed(
                "no_agent_boundary", "Codex cannot edit files under ATLAS's sandbox yet. Choose Claude Code as the developer."
            )
        if request.writable and self._boundary is None:
            return AgentResult.failed(
                "no_agent_boundary", "No OS sandbox is available, so agents that edit files are not allowed to run."
            )
        if self._boundary is not None:
            cmd = self._boundary.wrap(cmd, worktree=request.cwd if request.writable else None)
        try:
            proc = subprocess.run(
                cmd,
                input=request.prompt,
                capture_output=True,
                text=True,
                cwd=request.cwd,
                env=env,
                timeout=request.timeout_seconds,
                check=False,
            )
        except FileNotFoundError:
            return AgentResult.failed("not_installed", f"The {cmd[0]!r} command is not installed or not on PATH.")
        except subprocess.TimeoutExpired:
            return AgentResult.failed("timeout", f"No answer within {request.timeout_seconds}s.")
        if proc.returncode != 0:
            result = classify_failure(proc.returncode, proc.stdout or "", proc.stderr or "")
            return AgentResult.failed(
                result.failure or "nonzero_exit", redact(result.detail, self._secrets), proc.returncode
            )
        output = out_file.read_text() if out_file.exists() else proc.stdout
        return AgentResult.success(redact(output, self._secrets))


class FakeAdapter:
    """Replays canned results in order; records the requests it received."""

    def __init__(self, results: Sequence[AgentResult]) -> None:
        self._results = list(results)
        self.requests: list[AgentRequest] = []

    def run(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        if not self._results:
            raise AssertionError("FakeAdapter has no more canned results")
        return self._results.pop(0)


def adapter_for(agent: AgentConfigEntry, *, secrets: Sequence[str] = ()) -> AgentAdapter:
    return CliAdapter(agent, secrets=secrets)
