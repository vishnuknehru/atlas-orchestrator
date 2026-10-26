"""Plan challenge loop: challenger findings, architect answers, caps, user approval and requested changes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from atlas.agents import AgentResult, FakeAdapter
from atlas.analysis import ensure_checkout
from atlas.analysis import repo as repo_mod
from atlas.config.secret_store import InMemorySecretStore
from tests.test_analyse_step import CLEAN, PLAN_JSON, _client, _fake_factory, _remote, _task


def challenge(*findings: tuple[str, str]) -> AgentResult:
    blocking = any(sev in ("critical", "high") for _, sev in findings)
    return AgentResult.success(
        json.dumps(
            {
                "verdict": "revise" if blocking else "approve",
                "findings": [{"id": fid, "severity": sev, "text": f"problem {fid}"} for fid, sev in findings],
            }
        )
    )


def revision(*finding_ids: str, title: str = "Revised") -> AgentResult:
    return AgentResult.success(
        json.dumps(
            {
                "responses": [{"finding_id": f, "response": f"handled {f}"} for f in finding_ids],
                "approach": "Better.",
                "steps": [{"title": title, "description": "d", "files": [], "criteria": ["AC-1"]}],
                "risks": [],
            }
        )
    )


class Env:
    def __init__(self, client: TestClient, headers: dict[str, str], pid: str, tid: str, fake: FakeAdapter, home: Path):
        self.client, self.headers, self.pid, self.tid, self.fake, self.home = client, headers, pid, tid, fake, home

    def task(self) -> dict[str, Any]:
        body = self.client.get(f"/api/v1/tasks/{self.tid}").json()
        return dict(body.get("task", body))

    def plan(self) -> dict[str, Any]:
        return dict(self.client.get(f"/api/v1/tasks/{self.tid}/plan").json())

    def post(self, path: str, **kw: Any) -> Any:
        return self.client.post(f"/api/v1/tasks/{self.tid}/{path}", headers=self.headers, **kw)


@pytest.fixture
def env_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    def make(*results: AgentResult, start: bool = True) -> Env:
        src = _remote(tmp_path)
        store = InMemorySecretStore()
        fake, factory = _fake_factory(AgentResult.success(PLAN_JSON), *results)
        client, headers, pid = _client(tmp_path, store, factory)
        store.set_secret(f"atlas.{pid}.github", "atlas", "ghp_token")
        monkeypatch.setattr(repo_mod, "ensure_checkout", lambda p, url, b, pat: ensure_checkout(p, str(src), b, pat))
        env = Env(client, headers, pid, _task(client, headers, pid), fake, tmp_path / "atlas-home")
        if start:
            env.post("start")
        return env

    return make


def test_clean_challenge_waits_for_approval_then_approve(env_factory) -> None:  # type: ignore[no-untyped-def]
    env = env_factory(AgentResult.success(CLEAN))
    assert env.task()["state"] == "AWAITING_APPROVAL"
    assert env.fake.requests[1].role == "challenger"
    plan = env.plan()
    assert plan["history"][0]["verdict"] == "approve" and plan["escalated"] is False

    assert env.post("plan/approve").status_code == 200
    assert env.task()["state"] == "READY_FOR_IMPLEMENTATION"
    assert env.post("plan/approve").status_code == 409


def test_blocking_finding_is_answered_and_plan_revised(env_factory) -> None:  # type: ignore[no-untyped-def]
    env = env_factory(challenge(("F1", "high"), ("F2", "low")), revision("F1"), AgentResult.success(CLEAN))
    assert env.task()["state"] == "AWAITING_APPROVAL"
    plan = env.plan()
    assert plan["revision"] == 2 and plan["steps"][0]["title"] == "Revised"
    kinds = [h["kind"] for h in plan["history"]]
    assert kinds == ["challenge", "revision", "challenge"]
    assert plan["history"][1]["responses"][0]["finding_id"] == "F1"
    assert "F1" in env.fake.requests[2].prompt and "F2" not in env.fake.requests[2].prompt


def test_unanswered_finding_blocks_the_task(env_factory) -> None:  # type: ignore[no-untyped-def]
    env = env_factory(challenge(("F1", "critical"), ("F2", "high")), revision("F1"))
    task = env.task()
    assert (task["state"], task["blocked_reason"]) == ("BLOCKED", "findings_unanswered")
    assert env.plan()["revision"] == 1


def test_three_blocking_cycles_escalate_to_the_user(env_factory) -> None:  # type: ignore[no-untyped-def]
    env = env_factory(
        challenge(("F1", "high")), revision("F1"),
        challenge(("F2", "high")), revision("F2"),
        challenge(("F3", "critical")),
    )
    assert env.task()["state"] == "AWAITING_APPROVAL"
    plan = env.plan()
    assert plan["escalated"] is True and len([h for h in plan["history"] if h["kind"] == "challenge"]) == 3


def test_user_can_request_changes_and_the_loop_runs_again(env_factory) -> None:  # type: ignore[no-untyped-def]
    env = env_factory(AgentResult.success(CLEAN), revision("USER", title="With rollback"), AgentResult.success(CLEAN))
    assert env.post("plan/request-changes", json={"comment": "Add a rollback step"}).status_code == 200
    assert env.task()["state"] == "AWAITING_APPROVAL"
    plan = env.plan()
    assert plan["steps"][0]["title"] == "With rollback"
    assert [h["kind"] for h in plan["history"]] == ["challenge", "user_feedback", "revision", "challenge"]
    assert "Add a rollback step" in env.fake.requests[2].prompt


def test_approval_endpoints_require_awaiting_approval(env_factory) -> None:  # type: ignore[no-untyped-def]
    env = env_factory(challenge(("F1", "high")))  # no revision queued -> stops before approval
    assert env.task()["state"] != "AWAITING_APPROVAL"
    assert env.post("plan/approve").status_code == 409
    assert env.post("plan/request-changes", json={"comment": "please change it"}).status_code == 409
    assert env.client.get("/api/v1/tasks/nope/plan").status_code == 404


def test_challenger_must_be_independent_of_the_author(env_factory) -> None:  # type: ignore[no-untyped-def]
    env = env_factory(start=False)
    with env.client.app.state.db.begin_immediate() as conn:
        cfg = json.loads(conn.execute("SELECT config_json FROM projects WHERE id = ?;", (env.pid,)).fetchone()[0])
        cfg.setdefault("agents", {})["challenger"] = "claude"
        conn.execute("UPDATE projects SET config_json = ? WHERE id = ?;", (json.dumps(cfg), env.pid))
    env.post("start")
    task = env.task()
    assert (task["state"], task["blocked_reason"]) == ("BLOCKED", "challenger_not_independent")
    assert len(env.fake.requests) == 1  # only the architect ran; the challenger was never called
