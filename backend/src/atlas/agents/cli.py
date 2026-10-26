"""CLI-backed adapters for Claude Code and Codex (read-only use), plus a replay adapter for tests."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence

from atlas.agents.base import (
    AgentAdapter,
    AgentRequest,
    AgentResult,
    classify_failure,
    redact,
    scrubbed_env,
)
from atlas.config.schemas import AgentConfigEntry


class CliAdapter:
    """Runs `claude -p` or `codex exec` once, read-only, in the request's scratch directory."""

    def __init__(self, agent: AgentConfigEntry, *, secrets: Sequence[str] = ()) -> None:
        self._agent = agent
        self._secrets = list(secrets)

    def _command(self, request: AgentRequest, out_file: str) -> list[str] | None:
        if self._agent.adapter == "claude_cli":
            cmd = ["claude", "-p", "--output-format", "text", "--no-session-persistence"]
            if self._agent.models:
                cmd += ["--model", self._agent.models[0]]
            return cmd
        if self._agent.adapter == "codex_cli":
            # Model is deliberately not passed: the CLI default is the one that works on the user's login.
            return [
                "codex", "exec", "-s", "read-only", "--skip-git-repo-check", "--ephemeral",
                "--ignore-rules", "-C", str(request.cwd), "-o", out_file, "-",
            ]
        return None

    def run(self, request: AgentRequest) -> AgentResult:
        out_file = request.cwd / "agent-output.txt"
        cmd = self._command(request, str(out_file))
        if cmd is None:
            return AgentResult.failed(
                "unsupported_adapter", f"{self._agent.adapter} cannot be run automatically by ATLAS"
            )
        try:
            proc = subprocess.run(
                cmd,
                input=request.prompt,
                capture_output=True,
                text=True,
                cwd=request.cwd,
                env=scrubbed_env(),
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
