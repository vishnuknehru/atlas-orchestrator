"""Slice 1.5 & 1.6 tests: FastAPI localhost security, CSRF/Host negatives, tasks CRUD, and minimal safe restart."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atlas.api.app import GitHubRepoCheckResult, create_app
from atlas.config.secret_store import InMemorySecretStore
from atlas.recovery.startup import InstanceAlreadyRunningError, InstanceLock


def _make_authed_client(
    data_dir: Path,
    secret_store: InMemorySecretStore | None = None,
) -> tuple[TestClient, str, str]:
    store = secret_store or InMemorySecretStore()
    store.set_secret("atlas.octocat/hello-world.github", "octocat", "ghp_valid_pat_12345678901234567890")

    def fake_gh_verifier(repo: str, pat: str) -> GitHubRepoCheckResult:
        return GitHubRepoCheckResult(ok=True, visibility="private", default_branch="main")

    app = create_app(
        data_dir=data_dir,
        secret_store=store,
        gh_verifier=fake_gh_verifier,
        allow_testserver=True,
        acquire_lock=False,
    )
    client = TestClient(app)
    session_token: str = app.state.session_token
    csrf_token: str = app.state.csrf_token

    # Bootstrap session cookie
    resp = client.get(f"/auth/bootstrap?token={session_token}", follow_redirects=False)
    assert resp.status_code == 303
    return client, session_token, csrf_token


def test_healthz_and_host_csrf_origin_security_negatives(tmp_path: Path) -> None:
    client, _, csrf_token = _make_authed_client(tmp_path / "atlas-home")

    # 1. /healthz works on allowed host
    r_health = client.get("/healthz", headers={"Host": "127.0.0.1:8420"})
    assert r_health.status_code == 200
    assert r_health.json()["status"] == "ok"
    assert "default-src 'self'" in r_health.headers.get("Content-Security-Policy", "")

    # 2. Disallowed Host header rejected (DNS rebinding defence)
    r_bad_host = client.get("/healthz", headers={"Host": "evil.attacker.com"})
    assert r_bad_host.status_code == 400

    # 3. Unauthenticated /api/v1/overview rejected
    unauthed = TestClient(client.app)
    r_unauth = unauthed.get("/api/v1/overview")
    assert r_unauth.status_code == 401

    # 4. Mutating POST without X-Atlas-CSRF header rejected (403)
    r_no_csrf = client.post(
        "/api/v1/projects",
        json={"name": "p1", "repository": "octocat/hello-world"},
    )
    assert r_no_csrf.status_code == 403

    # 5. Mutating POST with foreign Origin header rejected even with valid CSRF token (403)
    r_bad_origin = client.post(
        "/api/v1/projects",
        headers={
            "X-Atlas-CSRF": csrf_token,
            "Origin": "https://evil.example.com",
        },
        json={"name": "p1", "repository": "octocat/hello-world"},
    )
    assert r_bad_origin.status_code == 403


def test_phase_1_acceptance_register_create_pause_resume_cancel_and_safe_restart(
    tmp_path: Path,
) -> None:
    """Phase 1 Acceptance Test (implementation-roadmap.md):
    Register a project, create a task, and pause, resume or cancel it.
    The timeline shows only persisted transitions.
    After a restart, the state is identical and nothing proceeds without the user.
    """
    data_dir = tmp_path / "atlas-home"
    store = InMemorySecretStore()
    store.set_secret(
        "atlas.octocat/hello-world.github",
        "octocat",
        "ghp_valid_pat_12345678901234567890",
    )

    client, _, csrf_token = _make_authed_client(data_dir, store)
    headers = {"X-Atlas-CSRF": csrf_token, "Origin": "http://127.0.0.1:8420"}

    # 1. Register project
    r_proj = client.post(
        "/api/v1/projects",
        headers=headers,
        json={
            "name": "hello-world",
            "repository": "OctoCat/Hello-World",
            "base_branch": "main",
            "visibility": "private",
        },
    )
    assert r_proj.status_code == 201, r_proj.text
    proj = r_proj.json()
    project_id = proj["id"]
    assert proj["github_repo"] == "octocat/hello-world"
    assert proj["status"] == "active"

    # Duplicate repo registration rejected (ATLAS-012)
    r_dup = client.post(
        "/api/v1/projects",
        headers=headers,
        json={
            "name": "hello-world-2",
            "repository": "octocat/hello-world",
            "base_branch": "main",
        },
    )
    assert r_dup.status_code == 409

    # 2. Create a task
    r_task = client.post(
        f"/api/v1/projects/{project_id}/tasks",
        headers=headers,
        json={
            "title": "Add Google OAuth login",
            "request_text": "Implement Google OAuth2 login flow",
            "category": "feature",
            "priority": 2,
            "acceptance_criteria": [{"text": "User can sign in with Google", "mandatory": True}],
        },
    )
    assert r_task.status_code == 201
    task = r_task.json()
    task_id = task["id"]
    assert task["state"] == "CREATED"

    # 3. Pause and Resume the task
    r_pause = client.post(f"/api/v1/tasks/{task_id}/pause", headers=headers)
    assert r_pause.status_code == 200
    assert r_pause.json()["state"] == "PAUSED"
    assert r_pause.json()["resume_to"] == "CREATED"

    r_resume = client.post(f"/api/v1/tasks/{task_id}/resume", headers=headers)
    assert r_resume.status_code == 200
    assert r_resume.json()["state"] == "CREATED"

    # 4. Simulate a server restart while task is in CREATED (non-terminal):
    # Startup recovery transitions every non-terminal task to PAUSED and does NOT auto-resume.
    app2 = create_app(
        data_dir=data_dir,
        secret_store=store,
        allow_testserver=True,
        acquire_lock=True,
    )
    with TestClient(app2) as client2:
        token2 = app2.state.session_token
        csrf2 = app2.state.csrf_token
        client2.get(f"/auth/bootstrap?token={token2}", follow_redirects=False)
        headers2 = {"X-Atlas-CSRF": csrf2, "Origin": "http://127.0.0.1:8420"}

        # Verify a second instance lock on the same data_dir is refused while app2 is running
        second_lock = InstanceLock(data_dir)
        with pytest.raises(InstanceAlreadyRunningError):
            second_lock.acquire()

        # Inspect task after restart -> must be PAUSED with resume_to='CREATED'
        r_detail = client2.get(f"/api/v1/tasks/{task_id}")
        assert r_detail.status_code == 200
        detail = r_detail.json()
        assert detail["state"] == "PAUSED"
        assert detail["resume_to"] == "CREATED"

        # Timeline shows every persisted transition in order
        timeline = detail["timeline"]
        states_sequence = [(t["from_state"], t["to_state"]) for t in timeline]
        assert states_sequence == [
            (None, "CREATED"),
            ("CREATED", "PAUSED"),
            ("PAUSED", "CREATED"),
            ("CREATED", "PAUSED"),
        ]

        # 5. Cancel the task
        r_cancel = client2.post(f"/api/v1/tasks/{task_id}/cancel", headers=headers2)
        assert r_cancel.status_code == 200
        assert r_cancel.json()["state"] == "CANCELLED"


def test_task_investigation_history_is_persisted_scoped_and_redacted(tmp_path: Path) -> None:
    data_dir = tmp_path / "history-home"
    client, _, csrf = _make_authed_client(data_dir)
    headers = {"X-Atlas-CSRF": csrf, "Origin": "http://127.0.0.1:8420"}
    project = client.post("/api/v1/projects", headers=headers, json={"name": "History", "repository": "octocat/hello-world"}).json()
    project_id = project["id"]
    task = client.post(f"/api/v1/projects/{project_id}/tasks", headers=headers, json={"title": "Investigate", "request_text": "Keep this request", "acceptance_criteria": [{"text": "Persist history"}]}).json()
    task_id = task["id"]
    other = client.post(f"/api/v1/projects/{project_id}/tasks", headers=headers, json={"title": "Other", "request_text": "Unrelated"}).json()
    secret = "private_test_credential_0123456789"
    client.app.state.redactor.register(secret)
    with client.app.state.db.begin_immediate() as conn:
        for execution_id, owner in [("run-owned", task_id), ("run-unrelated", other["id"])]:
            conn.execute(
                "INSERT INTO executions (id, project_id, task_id, kind, status, argv_json, error_detail, correlation_id, created_at) VALUES (?, ?, ?, 'test', 'failed', ?, ?, 'trace-1', '2026-10-22T20:00:00Z');",
                (execution_id, project_id, owner, '["hidden_argument"]', f"Failure with {secret}"),
            )
        conn.execute("INSERT INTO workflow_steps (step_id, project_id, entity_type, entity_id, thread_id, generation, node, entry_version, status, created_at) VALUES ('step-owned', ?, 'task', ?, ?, 1, 'planning', 0, 'started', '2026-10-22T20:00:00Z');", (project_id, task_id, f"task:{task_id}:g1"))
        conn.execute("INSERT INTO blobs (sha256, size, media_type, created_at) VALUES ('artifact-hash', 4, 'text/plain', '2026-10-22T20:00:00Z');")
        conn.execute("INSERT INTO artifact_refs (id, project_id, sha256, kind, execution_id, direction, created_at) VALUES ('artifact-owned', ?, 'artifact-hash', 'test-output', 'run-owned', 'out', '2026-10-22T20:00:00Z');", (project_id,))
    result = client.get(f"/api/v1/tasks/{task_id}/history")
    assert result.status_code == 200, result.text
    history = result.json()
    assert history["task"]["request_text"] == "Keep this request"
    assert history["task"]["acceptance_criteria"][0]["text"] == "Persist history"
    assert history["task"]["timeline"][0]["to_state"] == "CREATED"
    assert [run["id"] for run in history["executions"]] == ["run-owned"]
    assert [step["step_id"] for step in history["workflow_steps"]] == ["step-owned"]
    assert history["artifacts"][0]["sha256"] == "artifact-hash"
    assert secret not in result.text
    assert "[REDACTED]" in result.text
    assert "hidden_argument" not in result.text
    assert history["workflow_thread_id"] == f"task:{task_id}:g1"
    # A fresh app instance reading the same files can investigate the same task.
    reopened, _, _ = _make_authed_client(data_dir)
    persisted = reopened.get(f"/api/v1/tasks/{task_id}/history").json()
    assert persisted["task"]["title"] == "Investigate"
    assert persisted["executions"][0]["id"] == "run-owned"
    assert reopened.get("/api/v1/tasks/missing/history").status_code == 404
    unauthenticated = TestClient(reopened.app)
    assert unauthenticated.get(f"/api/v1/tasks/{task_id}/history").status_code == 401
