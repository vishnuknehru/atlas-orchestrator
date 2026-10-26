"""Candidate tests through the API: project test settings, automatic run after implementation, evidence per commit."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from atlas.agents import AgentResult
from atlas.analysis import ensure_checkout
from atlas.analysis import repo as repo_mod
from atlas.config.secret_store import InMemorySecretStore
from atlas.sandbox.runner import CommandResult, TestRun, TestSpec
from tests.test_analyse_step import CLEAN, PLAN_JSON, _client, _remote, _task
from tests.test_implement_step import RoleAdapter, _write
from tests.test_plan_challenge import Env

IMAGE = "python:3.12-slim@sha256:" + "b" * 64


class RecordingRunner:
    def __init__(self, status: str = "passed") -> None:
        self.status = status
        self.calls: list[tuple[Path, str, TestSpec]] = []

    def __call__(self, repo: Path, sha: str, spec: TestSpec) -> TestRun:
        self.calls.append((repo, sha, spec))
        cmd = CommandResult(spec.commands[0], "passed" if self.status == "passed" else "failed", 0 if self.status == "passed" else 1, 0.5, "log")
        return TestRun(self.status, sha, image=spec.image, started_at="a", finished_at="b", commands=[cmd], source_attested=True)


@pytest.fixture
def make(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    def build(runner: RecordingRunner, *, configure: bool = True) -> Env:
        src = _remote(tmp_path)
        store = InMemorySecretStore()
        adapter = RoleAdapter([AgentResult.success(PLAN_JSON), AgentResult.success(CLEAN)], [_write("app.py"), _write("again.py")])
        client, headers, pid = _client(tmp_path, store, lambda agent: adapter, runner)  # type: ignore[arg-type,return-value]
        store.set_secret(f"atlas.{pid}.github", "atlas", "ghp_token")
        monkeypatch.setattr(repo_mod, "ensure_checkout", lambda p, url, b, pat: ensure_checkout(p, str(src), b, pat))
        env = Env(client, headers, pid, _task(client, headers, pid), adapter.readonly, tmp_path / "atlas-home")
        if configure:
            r = client.put(f"/api/v1/projects/{pid}/config", headers=headers,
                           json={"test_image": IMAGE, "test_commands": ["pytest -q"], "writable_paths": [".pytest_cache"]})
            assert r.status_code == 200, r.text
        env.post("start")
        env.post("plan/approve")
        return env

    return build


def evidence(env: Env) -> dict[str, Any]:
    return dict(env.client.get(f"/api/v1/tasks/{env.tid}/tests").json())


def test_candidate_is_tested_automatically_and_evidence_is_bound_to_its_commit(make) -> None:  # type: ignore[no-untyped-def]
    runner = RecordingRunner("passed")
    env = make(runner)
    assert env.task()["state"] == "EXECUTING_PHASES"
    cand = env.client.get(f"/api/v1/tasks/{env.tid}/candidate").json()
    ev = evidence(env)
    assert ev["status"] == "passed" and ev["commit_sha"] == cand["commit_sha"] and ev["stale"] is False
    repo, sha, spec = runner.calls[0]
    assert repo == env.home / "projects" / env.pid / "repo" and sha == cand["commit_sha"]
    assert spec.image == IMAGE and spec.commands == ["pytest -q"] and spec.writable_paths == [".pytest_cache"]


def test_failed_tests_are_recorded_and_the_task_does_not_complete(make) -> None:  # type: ignore[no-untyped-def]
    env = make(RecordingRunner("failed"))
    assert evidence(env)["status"] == "failed"
    assert env.task()["state"] == "EXECUTING_PHASES"


def test_without_test_settings_the_evidence_says_so(make) -> None:  # type: ignore[no-untyped-def]
    runner = RecordingRunner()
    env = make(runner, configure=False)
    ev = evidence(env)
    assert (ev["status"], ev["blocked_reason"]) == ("blocked", "no_test_config")
    assert runner.calls == []


def test_manual_rerun_and_stale_evidence(make) -> None:  # type: ignore[no-untyped-def]
    runner = RecordingRunner()
    env = make(runner)
    assert env.post("tests/run").status_code == 202
    assert len(runner.calls) == 2
    # a different candidate commit makes the old evidence stale
    import json

    path = env.home / "projects" / env.pid / "tests" / f"{env.tid}.json"
    data = json.loads(path.read_text())
    data["commit_sha"] = "0" * 40
    path.write_text(json.dumps(data))
    assert evidence(env)["stale"] is True


def test_tests_only_run_on_a_finished_candidate(make) -> None:  # type: ignore[no-untyped-def]
    env = make(RecordingRunner())
    env.post("cancel")
    assert env.post("tests/run").status_code == 409
    assert env.client.get("/api/v1/tasks/nope/tests").status_code == 404


def test_test_settings_must_pin_the_image_by_digest(make) -> None:  # type: ignore[no-untyped-def]
    env = make(RecordingRunner(), configure=False)
    url = f"/api/v1/projects/{env.pid}/config"
    for body in ({"test_image": "python:3.12"}, {"test_commands": [""] * 0 + ["x" * 3000]}, {"writable_paths": ["../escape"]}):
        r = env.client.put(url, headers=env.headers, json=body)
        assert r.status_code == 422, (body, r.text)
    ok = env.client.put(url, headers=env.headers, json={"test_image": IMAGE, "test_commands": ["make test"], "test_timeout_seconds": 120})
    assert ok.status_code == 200
    import json

    with env.client.app.state.db.read_conn() as conn:
        tests = json.loads(conn.execute("SELECT config_json FROM projects WHERE id = ?;", (env.pid,)).fetchone()[0])["tests"]
    assert (tests["image"], tests["commands"], tests["timeout_seconds"]) == (IMAGE, ["make test"], 120)
