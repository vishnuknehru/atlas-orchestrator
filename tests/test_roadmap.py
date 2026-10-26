"""Project roadmaps: the architect plans a goal in phases and every phase is captured as a task."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atlas.api.app import GitHubRepoCheckResult, create_app
from atlas.config.secret_store import InMemorySecretStore
from atlas.planning import PlanContext, PlanGenerationError, ProjectPlan, parse_plan_output

_ORIGIN = "http://127.0.0.1:8420"


def _plan() -> ProjectPlan:
    return ProjectPlan.model_validate(
        {
            "summary": "Build auth in two steps.",
            "phases": [
                {"title": "Add user model", "objective": "Persist users.", "category": "feature",
                 "acceptance_criteria": ["Users can be stored", "Passwords are hashed"]},
                {"title": "Add login API", "objective": "Expose login.", "category": "security",
                 "acceptance_criteria": ["Valid credentials return a session"]},
            ],
        }
    )


def _client(tmp_path: Path, generator) -> tuple[TestClient, dict[str, str], str]:
    app = create_app(
        data_dir=tmp_path / "atlas-home",
        secret_store=InMemorySecretStore(),
        gh_verifier=lambda repo, pat: GitHubRepoCheckResult(ok=True, visibility="private", default_branch="main"),
        plan_generator=generator,
        allow_testserver=True,
        acquire_lock=False,
    )
    client = TestClient(app)
    assert client.get(f"/auth/bootstrap?token={app.state.session_token}", follow_redirects=False).status_code == 303
    headers = {"X-Atlas-CSRF": app.state.csrf_token, "Origin": _ORIGIN}
    r = client.post(
        "/api/v1/projects", headers=headers,
        json={"name": "rm-proj", "repository": "octocat/rm-proj", "base_branch": "main"},
    )
    assert r.status_code == 201, r.text
    return client, headers, str(r.json()["id"])


def test_roadmap_creates_one_task_per_phase_with_criteria(tmp_path: Path) -> None:
    seen: list[PlanContext] = []

    def generator(ctx: PlanContext) -> ProjectPlan:
        seen.append(ctx)
        return _plan()

    client, headers, pid = _client(tmp_path, generator)
    r = client.post(f"/api/v1/projects/{pid}/roadmap", headers=headers, json={"goal": "Add user authentication"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert seen[0].goal == "Add user authentication" and seen[0].agent_id == "claude"
    assert body["summary"] == "Build auth in two steps."
    assert [t["title"] for t in body["tasks"]] == ["Add user model", "Add login API"]
    assert [t["roadmap_seq"] for t in body["tasks"]] == [1, 2]
    assert all(t["state"] == "CREATED" and t["roadmap_id"] == body["id"] for t in body["tasks"])
    assert [c["text"] for c in json.loads(body["tasks"][0]["acceptance_criteria_json"])] == [
        "Users can be stored",
        "Passwords are hashed",
    ]

    listed = client.get(f"/api/v1/projects/{pid}/tasks").json()
    assert {t["title"] for t in listed} == {"Add user model", "Add login API"}
    assert client.get(f"/api/v1/projects/{pid}/roadmaps").json()[0]["goal"] == "Add user authentication"
    detail = client.get(f"/api/v1/tasks/{body['tasks'][1]['id']}").json()
    assert detail["timeline"][0]["reason"].startswith("Phase 2 of roadmap")


def test_failed_plan_creates_nothing(tmp_path: Path) -> None:
    def generator(ctx: PlanContext) -> ProjectPlan:
        raise PlanGenerationError("architect offline")

    client, headers, pid = _client(tmp_path, generator)
    r = client.post(f"/api/v1/projects/{pid}/roadmap", headers=headers, json={"goal": "Add user authentication"})
    assert r.status_code == 502 and "architect offline" in r.json()["detail"]
    assert client.get(f"/api/v1/projects/{pid}/tasks").json() == []
    assert client.get(f"/api/v1/projects/{pid}/roadmaps").json() == []


def test_roadmap_requires_csrf_and_known_project(tmp_path: Path) -> None:
    client, headers, pid = _client(tmp_path, lambda ctx: _plan())
    assert client.post(f"/api/v1/projects/{pid}/roadmap", json={"goal": "Add user authentication"}).status_code in (401, 403)
    assert client.post("/api/v1/projects/nope/roadmap", headers=headers, json={"goal": "Add user authentication"}).status_code == 404


def test_parse_plan_output_tolerates_prose_and_rejects_bad_plans() -> None:
    wrapped = "Here you go:\n```json\n" + _plan().model_dump_json() + "\n```"
    assert len(parse_plan_output(wrapped).phases) == 2
    with pytest.raises(PlanGenerationError):
        parse_plan_output("no json here")
    with pytest.raises(PlanGenerationError):
        parse_plan_output('{"summary": "x", "phases": [{"title": "t", "objective": "o", "acceptance_criteria": []}]}')


def test_existing_database_is_upgraded(tmp_path: Path) -> None:
    import sqlite3

    from atlas.db.models import SCHEMA_Statements, apply_schema_ddl

    conn = sqlite3.connect(tmp_path / "old.db")
    for stmt in SCHEMA_Statements:
        conn.execute(stmt)
    apply_schema_ddl(conn)
    apply_schema_ddl(conn)  # idempotent
    cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks);")}
    assert {"roadmap_id", "roadmap_seq"} <= cols


def test_refined_plan_cancels_unstarted_phases_of_the_previous_plan(tmp_path: Path) -> None:
    client, headers, pid = _client(tmp_path, lambda ctx: _plan())
    first = client.post(f"/api/v1/projects/{pid}/roadmap", headers=headers, json={"goal": "Add user authentication"}).json()
    second = client.post(f"/api/v1/projects/{pid}/roadmap", headers=headers, json={"goal": "Add user authentication v2"}).json()

    states = {t["id"]: t["state"] for t in client.get(f"/api/v1/projects/{pid}/tasks").json()}
    assert all(states[t["id"]] == "CANCELLED" for t in first["tasks"])
    assert all(states[t["id"]] == "CREATED" for t in second["tasks"])
