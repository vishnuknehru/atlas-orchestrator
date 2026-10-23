"""Settings page API: write-only GitHub PAT and agent API key storage, verification, and registry view."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from atlas.api.app import GitHubRepoCheckResult, create_app
from atlas.config.secret_store import InMemorySecretStore

_PAT = "github_pat_settings_page_0123456789abcdef"
_ORIGIN = "http://127.0.0.1:8420"


def _client(
    tmp_path: Path, store: InMemorySecretStore, *, verify_ok: bool = True
) -> tuple[TestClient, dict[str, str]]:
    def fake_gh_verifier(repo: str, pat: str) -> GitHubRepoCheckResult:
        if verify_ok and pat == _PAT:
            return GitHubRepoCheckResult(ok=True, visibility="private", default_branch="main")
        return GitHubRepoCheckResult(ok=False, error_reason="github_credential")

    app = create_app(
        data_dir=tmp_path / "atlas-home",
        secret_store=store,
        gh_verifier=fake_gh_verifier,
        allow_testserver=True,
        acquire_lock=False,
    )
    client = TestClient(app)
    assert client.get(f"/auth/bootstrap?token={app.state.session_token}", follow_redirects=False).status_code == 303
    return client, {"X-Atlas-CSRF": app.state.csrf_token, "Origin": _ORIGIN}


def _register(client: TestClient, headers: dict[str, str]) -> str:
    r = client.post(
        "/api/v1/projects",
        headers=headers,
        json={"name": "settings-proj", "repository": "octocat/settings-proj", "base_branch": "main"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["blocked_reason"] == "github_credential"
    return str(r.json()["id"])


def _settings_project(client: TestClient, project_id: str) -> dict[str, object]:
    r = client.get("/api/v1/settings")
    assert r.status_code == 200, r.text
    return next(p for p in r.json()["projects"] if p["id"] == project_id)


def test_store_verify_and_remove_github_credential(tmp_path: Path) -> None:
    store = InMemorySecretStore()
    client, headers = _client(tmp_path, store)
    project_id = _register(client, headers)
    assert _settings_project(client, project_id)["github_credential_configured"] is False

    r_put = client.put(
        f"/api/v1/projects/{project_id}/github-credential", headers=headers, json={"secret": f"  {_PAT}\n"}
    )
    assert r_put.status_code == 200, r_put.text
    body = r_put.json()
    assert body["verified"] is True
    assert body["project"]["status"] == "active"
    assert body["project"]["blocked_reason"] is None
    assert _PAT not in r_put.text
    assert store.get_secret(f"atlas.{project_id}.github") == _PAT

    settings = client.get("/api/v1/settings")
    assert _PAT not in settings.text
    assert _settings_project(client, project_id)["github_credential_configured"] is True

    with client.app.state.db.read_conn() as conn:
        audit = [r["detail_json"] for r in conn.execute("SELECT detail_json FROM audit_log;").fetchall()]
    assert not any(_PAT in a for a in audit)

    r_del = client.delete(f"/api/v1/projects/{project_id}/github-credential", headers=headers)
    assert r_del.status_code == 200, r_del.text
    assert r_del.json()["blocked_reason"] == "github_credential"
    assert store.get_secret(f"atlas.{project_id}.github") is None
    assert _settings_project(client, project_id)["github_credential_configured"] is False


def test_rejected_pat_keeps_project_blocked(tmp_path: Path) -> None:
    client, headers = _client(tmp_path, InMemorySecretStore(), verify_ok=False)
    project_id = _register(client, headers)
    r = client.put(f"/api/v1/projects/{project_id}/github-credential", headers=headers, json={"secret": _PAT})
    assert r.status_code == 200, r.text
    assert r.json()["verified"] is False
    assert r.json()["project"]["status"] == "blocked"
    assert r.json()["project"]["blocked_reason"] == "github_credential"


def test_credential_input_validation_and_csrf(tmp_path: Path) -> None:
    client, headers = _client(tmp_path, InMemorySecretStore())
    project_id = _register(client, headers)
    url = f"/api/v1/projects/{project_id}/github-credential"

    assert client.put(url, headers=headers, json={"secret": "two words"}).status_code == 422
    assert client.put(url, headers=headers, json={"secret": "   "}).status_code == 422
    assert client.put(url, json={"secret": _PAT}).status_code == 403
    assert (
        client.put("/api/v1/projects/missing/github-credential", headers=headers, json={"secret": _PAT}).status_code
        == 404
    )


def test_agent_api_key_only_for_api_key_agents(tmp_path: Path) -> None:
    store = InMemorySecretStore()
    client, headers = _client(tmp_path, store)
    registry = client.get("/api/v1/agents").json()
    registry["agents"]["claude-api"] = {
        "adapter": "claude_cli",
        "models": ["claude-sonnet-4-5"],
        "billing": "api_key",
    }
    assert client.put("/api/v1/agents", headers=headers, json=registry).status_code == 200

    settings = client.get("/api/v1/settings").json()
    assert settings["agent_api_keys"] == {"claude-api": False}
    assert {a["name"] for a in settings["adapters"]} >= {"claude_cli", "codex_cli"}
    assert settings["roles"] == ["architect", "challenger", "developer", "reviewer", "qa"]

    key = "sk-settings-page-key-0123456789"
    r = client.put("/api/v1/agents/claude-api/api-key", headers=headers, json={"secret": key})
    assert r.status_code == 200, r.text
    assert key not in r.text
    assert store.get_secret("atlas.agent.claude-api") == key
    assert client.get("/api/v1/settings").json()["agent_api_keys"] == {"claude-api": True}

    assert client.put("/api/v1/agents/claude/api-key", headers=headers, json={"secret": key}).status_code == 409
    assert client.put("/api/v1/agents/nope/api-key", headers=headers, json={"secret": key}).status_code == 404

    assert client.delete("/api/v1/agents/claude-api/api-key", headers=headers).status_code == 200
    assert store.get_secret("atlas.agent.claude-api") is None
    assert json.dumps(client.get("/api/v1/settings").json()["agent_api_keys"]) == '{"claude-api": false}'
