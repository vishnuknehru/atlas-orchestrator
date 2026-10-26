"""Agent adapter layer: typed failures, env scrubbing, redaction and the planner's use of it."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from atlas.agents import (
    AgentRequest,
    AgentResult,
    CliAdapter,
    FakeAdapter,
    classify_failure,
    redact,
    scrubbed_env,
)
from atlas.config.schemas import AgentConfigEntry
from atlas.planning import PlanContext, PlanGenerationError
from atlas.planning import planner as planner_mod


def _req(tmp_path: Path) -> AgentRequest:
    return AgentRequest(agent_id="claude", role="architect", prompt="hi", cwd=tmp_path, timeout_seconds=5)


def _claude() -> AgentConfigEntry:
    return AgentConfigEntry(adapter="claude_cli", models=["sonnet"])


@pytest.mark.parametrize(
    ("stderr", "kind"),
    [
        ("Claude usage limit reached", "rate_limit"),
        ("HTTP 429 Too Many Requests", "rate_limit"),
        ("Error: Not logged in. Please run /login", "auth_expired"),
        ("segfault", "nonzero_exit"),
    ],
)
def test_classify_failure(stderr: str, kind: str) -> None:
    assert classify_failure(1, "", stderr).failure == kind


def test_scrubbed_env_drops_credentials() -> None:
    env = scrubbed_env({"PATH": "/bin", "HOME": "/h", "GH_TOKEN": "x", "ANTHROPIC_API_KEY": "y", "LC_ALL": "C"})
    assert env == {"PATH": "/bin", "HOME": "/h", "LC_ALL": "C"}


def test_redact_exact_values_only() -> None:
    assert redact("key=sk-secret-123 ok", ["sk-secret-123", "ab"]) == "key=[REDACTED] ok"


@pytest.fixture(autouse=True)
def _binaries_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name, path=None: f"/usr/bin/{name}")


class _Identity:
    """A boundary that leaves argv untouched, so tests can assert on the plain command."""

    def wrap(self, argv: list[str], *, worktree: Path | None) -> list[str]:
        return list(argv)


NO_BOUNDARY = _Identity()


class _Passthrough:
    def wrap(self, argv: list[str], *, worktree: Path | None) -> list[str]:
        return ["boundary", *(["--wt", str(worktree)] if worktree else []), *argv]


def test_cli_adapter_runs_scrubbed_and_returns_stdout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        seen.update(argv=argv, **kw)
        return subprocess.CompletedProcess(argv, 0, "answer sk-live-secret-1", "")

    monkeypatch.setenv("GH_TOKEN", "leak")
    monkeypatch.setattr(subprocess, "run", fake_run)
    result = CliAdapter(_claude(), secrets=["sk-live-secret-1"], boundary=NO_BOUNDARY).run(_req(tmp_path))
    assert result == AgentResult.success("answer [REDACTED]")
    assert seen["argv"][:2] == ["claude", "-p"] and "--model" in seen["argv"]
    assert seen["input"] == "hi" and "GH_TOKEN" not in seen["env"]


def test_cli_adapter_maps_missing_binary_and_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(argv: list[str], **kw: Any) -> Any:
        raise FileNotFoundError

    def slow(argv: list[str], **kw: Any) -> Any:
        raise subprocess.TimeoutExpired(argv, 5)

    monkeypatch.setattr(subprocess, "run", missing)
    assert CliAdapter(_claude(), boundary=NO_BOUNDARY).run(_req(tmp_path)).failure == "not_installed"
    monkeypatch.setattr(subprocess, "run", slow)
    assert CliAdapter(_claude(), boundary=NO_BOUNDARY).run(_req(tmp_path)).failure == "timeout"


def test_manual_handoff_agents_are_not_run(tmp_path: Path) -> None:
    agent = AgentConfigEntry(adapter="manual_handoff", provider="google")
    assert CliAdapter(agent, boundary=NO_BOUNDARY).run(_req(tmp_path)).failure == "unsupported_adapter"


def test_planner_goes_through_adapter_and_reports_typed_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = PlanContext("p", "o/r", "main", "Add auth please", "claude", _claude())
    plan_json = '{"summary":"s","phases":[{"title":"t","objective":"o","category":"feature","acceptance_criteria":["a","b"]}]}'
    fake = FakeAdapter([AgentResult.success(plan_json), AgentResult.failed("rate_limit", "usage limit reached", 1)])
    monkeypatch.setattr(planner_mod, "adapter_for", lambda agent: fake)
    assert planner_mod.cli_plan_generator(ctx).phases[0].title == "t"
    assert fake.requests[0].role == "architect" and fake.requests[0].cwd != Path.cwd()
    with pytest.raises(PlanGenerationError, match="usage limit"):
        planner_mod.cli_plan_generator(ctx)


def test_writer_roles_run_inside_the_boundary_with_only_edit_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "done", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    wt = tmp_path / "wt"
    wt.mkdir()
    req = AgentRequest(agent_id="a", role="developer", prompt="p", cwd=wt, writable=True)
    assert CliAdapter(_claude(), boundary=_Passthrough()).run(req).ok
    assert seen[0][:3] == ["boundary", "--wt", str(wt)]
    assert "acceptEdits" in seen[0] and "Read,Edit,Write,Grep,Glob" in seen[0] and not any("Bash" in a for a in seen[0])

    codex = AgentConfigEntry(adapter="codex_cli", models=["gpt-6.1-sol"])
    # Codex cannot edit files under our boundary (its own sandbox won't nest): fail closed, never launched
    assert CliAdapter(codex, boundary=_Passthrough()).run(req).failure == "no_agent_boundary"
    assert len(seen) == 1
    # Read-only requests get no writable worktree
    assert CliAdapter(codex, boundary=_Passthrough()).run(_req(tmp_path)).ok
    assert "--wt" not in seen[1] and "read-only" in seen[1]


def test_writer_roles_fail_closed_without_a_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def never(argv: list[str], **kw: Any) -> Any:
        raise AssertionError("agent must not be launched without a boundary")

    monkeypatch.setattr(subprocess, "run", never)
    adapter = CliAdapter(_claude(), boundary=None)
    adapter._boundary = None  # get_boundary() may find one on this machine
    req = AgentRequest(agent_id="a", role="developer", prompt="p", cwd=tmp_path, writable=True)
    assert adapter.run(req).failure == "no_agent_boundary"
