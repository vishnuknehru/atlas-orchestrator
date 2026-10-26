"""The `implement` step: a developer agent edits a task worktree and the change becomes a candidate commit."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from atlas.agents import AgentRequest, AgentResult, FakeAdapter
from atlas.analysis import ensure_checkout
from atlas.analysis import repo as repo_mod
from atlas.config.schemas import ProjectRoleMapping
from atlas.config.secret_store import InMemorySecretStore
from tests.test_analyse_step import CLEAN, PLAN_JSON, _client, _remote, _task
from tests.test_plan_challenge import Env

Edit = Callable[[Path], None]


class RoleAdapter:
    """Replays canned results for read-only roles; the developer role runs `edit` inside its worktree."""

    def __init__(self, readonly: list[AgentResult], edits: list[Edit | None]) -> None:
        self.readonly = FakeAdapter(readonly)
        self.edits = edits
        self.developer_requests: list[AgentRequest] = []

    def run(self, request: AgentRequest) -> AgentResult:
        if request.role != "developer":
            return self.readonly.run(request)
        self.developer_requests.append(request)
        edit = self.edits.pop(0)
        if edit is not None:
            edit(request.cwd)
        return AgentResult.success("Implemented the change.")


def _write(path: str, text: str = "x = 1\n") -> Edit:
    def edit(wt: Path) -> None:
        target = wt / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    return edit


@pytest.fixture
def make(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    def build(readonly: list[AgentResult], edits: list[Edit | None]) -> tuple[Env, RoleAdapter, Path]:
        src = _remote(tmp_path)
        store = InMemorySecretStore()
        adapter = RoleAdapter([AgentResult.success(PLAN_JSON), *readonly], edits)
        client, headers, pid = _client(tmp_path, store, lambda agent: adapter)  # type: ignore[arg-type,return-value]
        store.set_secret(f"atlas.{pid}.github", "atlas", "ghp_token")
        monkeypatch.setattr(repo_mod, "ensure_checkout", lambda p, url, b, pat: ensure_checkout(p, str(src), b, pat))
        env = Env(client, headers, pid, _task(client, headers, pid), adapter.readonly, tmp_path / "atlas-home")
        env.post("start")
        return env, adapter, src

    return build


def _candidate(env: Env) -> dict[str, object]:
    return dict(env.client.get(f"/api/v1/tasks/{env.tid}/candidate").json())


def test_default_roles_make_claude_the_developer_and_codex_the_reviewer() -> None:
    roles = ProjectRoleMapping()
    assert (roles.developer, roles.reviewer, roles.challenger) == ("claude", "codex", "codex")


def test_approved_plan_is_implemented_into_a_candidate_commit(make, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    env, adapter, src = make([AgentResult.success(CLEAN)], [_write("app/login.py", "def login(): ...\n")])
    assert env.client.get(f"/api/v1/tasks/{env.tid}/candidate").status_code == 404
    assert env.post("plan/approve").status_code == 200

    task = env.task()
    assert (task["state"], task["blocked_reason"]) == ("EXECUTING_PHASES", None)
    req = adapter.developer_requests[0]
    assert req.writable and req.cwd == env.home / "projects" / env.pid / "worktrees" / env.tid
    assert "Approach: Add it." in req.prompt and "Do it" in req.prompt

    cand = _candidate(env)
    assert cand["changed_files"] == ["app/login.py"] and cand["guard_status"] == "clear"
    assert cand["author_agent"] == "claude" and cand["commit_sha"] != cand["base_sha"]
    assert "Implemented" in str(cand["summary"])
    # the candidate lives in the task worktree only: the project's clone and its remote are untouched
    clone = env.home / "projects" / env.pid / "repo"
    assert not (clone / "app" / "login.py").exists()
    shown = subprocess.run(["git", "show", f"{cand['commit_sha']}:app/login.py"], cwd=clone, capture_output=True, text=True)
    assert shown.stdout == "def login(): ...\n"


def test_sensitive_paths_hold_the_candidate(make) -> None:  # type: ignore[no-untyped-def]
    env, _, _ = make(
        [AgentResult.success(CLEAN)],
        [lambda wt: (_write(".github/workflows/ci.yml", "on: push\n")(wt), _write("ok.py")(wt))],
    )
    env.post("plan/approve")
    cand = _candidate(env)
    assert cand["guard_status"] == "held"
    assert cand["violations"] == [{"path": ".github/workflows/ci.yml", "kind": "sensitive_path"}]
    assert "held for review" in str(env.client.get(f"/api/v1/tasks/{env.tid}").json())


def test_an_agent_that_changes_nothing_blocks_and_retry_starts_from_a_clean_worktree(make) -> None:  # type: ignore[no-untyped-def]
    env, adapter, _ = make([AgentResult.success(CLEAN)], [None, _write("done.py")])
    env.post("plan/approve")
    task = env.task()
    assert (task["state"], task["blocked_reason"]) == ("BLOCKED", "no_changes_made")
    assert env.post("retry").status_code == 200
    assert env.task()["state"] == "EXECUTING_PHASES"
    assert _candidate(env)["changed_files"] == ["done.py"]
    assert len(adapter.developer_requests) == 2


def test_blocked_developer_failures_have_specific_reasons(make) -> None:  # type: ignore[no-untyped-def]
    env, adapter, _ = make([AgentResult.success(CLEAN)], [])

    def run(request: AgentRequest) -> AgentResult:
        if request.role == "developer":
            return AgentResult.failed("no_agent_boundary", "no sandbox")
        return adapter.readonly.run(request)

    adapter.run = run  # type: ignore[method-assign]
    env.post("plan/approve")
    task = env.task()
    assert (task["state"], task["blocked_reason"]) == ("BLOCKED", "no_agent_boundary")


def test_task_blocked_during_the_challenge_can_be_retried(make) -> None:  # type: ignore[no-untyped-def]
    env, _, _ = make([AgentResult.success("not json"), AgentResult.success(CLEAN)], [])
    task = env.task()
    assert (task["state"], task["blocked_reason"]) == ("BLOCKED", "challenge_invalid")
    assert env.post("retry").status_code == 200
    assert env.task()["state"] == "AWAITING_APPROVAL"


def test_developer_must_be_a_configured_agent(make) -> None:  # type: ignore[no-untyped-def]
    env, _, _ = make([AgentResult.success(CLEAN)], [_write("a.py")])
    # unknown agent id in the project's role mapping
    import json

    with env.client.app.state.db.begin_immediate() as conn:
        cfg = json.loads(conn.execute("SELECT config_json FROM projects WHERE id = ?;", (env.pid,)).fetchone()[0])
        cfg.setdefault("agents", {})["developer"] = "ghost"
        conn.execute("UPDATE projects SET config_json = ? WHERE id = ?;", (json.dumps(cfg), env.pid))
    env.post("plan/approve")
    assert env.task()["blocked_reason"] == "developer_not_configured"
