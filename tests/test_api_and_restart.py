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
