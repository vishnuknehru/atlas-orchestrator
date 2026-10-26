"""The `analyse` workflow step: checkout, deterministic scan and the Start button's first real effect."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atlas.analysis import CheckoutError, ensure_checkout, scan_repository
from atlas.analysis import repo as repo_mod
from atlas.api.app import GitHubRepoCheckResult, create_app
from atlas.config.secret_store import InMemorySecretStore

_ORIGIN = "http://127.0.0.1:8420"
_GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]


def _remote(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "README.md").write_text("# Demo\nHello")
    (src / "pyproject.toml").write_text("[project]\nname='demo'\n")
    (src / "app.py").write_text("print('hi')\n")
    for args in (["init", "-b", "main"], ["add", "."], ["commit", "-m", "init"]):
        subprocess.run([*_GIT, *args], cwd=src, check=True, capture_output=True)
    return src


def test_scan_repository_reports_facts_and_skips_git(tmp_path: Path) -> None:
    src = _remote(tmp_path)
    (src / "link").symlink_to("/etc/passwd")
    scan = scan_repository(src)
    assert ".git" not in scan["top_level"] and "app.py" in scan["top_level"]
    assert scan["manifests"] == ["pyproject.toml"]
    assert scan["extensions"][".py"] == 1
    assert scan["readme_excerpt"].startswith("# Demo")


def test_ensure_checkout_clones_then_fast_forwards(tmp_path: Path) -> None:
    src = _remote(tmp_path)
    dest = tmp_path / "clone"
    ensure_checkout(dest, str(src), "main", "ghp_x")
    assert (dest / "app.py").exists()
    (src / "new.py").write_text("x = 1\n")
    subprocess.run([*_GIT, "add", "."], cwd=src, check=True, capture_output=True)
    subprocess.run([*_GIT, "commit", "-m", "more"], cwd=src, check=True, capture_output=True)
    ensure_checkout(dest, str(src), "main", "ghp_x")
    assert (dest / "new.py").exists()


def test_ensure_checkout_reports_failure_without_leaking_token(tmp_path: Path) -> None:
    with pytest.raises(CheckoutError) as err:
        ensure_checkout(tmp_path / "c", str(tmp_path / "missing"), "main", "ghp_supersecret")
    assert err.value.code == "clone_failed" and "ghp_supersecret" not in str(err.value)


def _client(tmp_path: Path, store: InMemorySecretStore) -> tuple[TestClient, dict[str, str], str]:
    app = create_app(
        data_dir=tmp_path / "atlas-home",
        secret_store=store,
        gh_verifier=lambda repo, pat: GitHubRepoCheckResult(ok=True, visibility="private", default_branch="main"),
        allow_testserver=True,
        acquire_lock=False,
    )
    client = TestClient(app)
    assert client.get(f"/auth/bootstrap?token={app.state.session_token}", follow_redirects=False).status_code == 303
    headers = {"X-Atlas-CSRF": app.state.csrf_token, "Origin": _ORIGIN}
    pid = client.post(
        "/api/v1/projects", headers=headers,
        json={"name": "p", "repository": "octocat/p", "base_branch": "main"},
    ).json()["id"]
    return client, headers, str(pid)


def _task(client: TestClient, headers: dict[str, str], pid: str) -> str:
    r = client.post(
        f"/api/v1/projects/{pid}/tasks", headers=headers,
        json={"title": "t", "request_text": "do it", "category": "feature", "priority": 3},
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def test_start_without_token_blocks_with_a_clear_reason(tmp_path: Path) -> None:
    client, headers, pid = _client(tmp_path, InMemorySecretStore())
    tid = _task(client, headers, pid)
    client.post(f"/api/v1/tasks/{tid}/start", headers=headers)
    body = client.get(f"/api/v1/tasks/{tid}").json()
    task = body.get("task", body)
    assert task["state"] == "BLOCKED" and task["blocked_reason"] == "no_github_token"


def test_start_analyses_repository_and_moves_to_planning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = _remote(tmp_path)
    store = InMemorySecretStore()
    client, headers, pid = _client(tmp_path, store)
    store.set_secret(f"atlas.{pid}.github", "atlas", "ghp_token")
    seen: dict[str, str] = {}

    def fake_checkout(clone_path: Path, url: str, branch: str, pat: str) -> None:
        seen.update(url=url, branch=branch, pat=pat)
        ensure_checkout(clone_path, str(src), branch, pat)

    monkeypatch.setattr(repo_mod, "ensure_checkout", fake_checkout)
    tid = _task(client, headers, pid)
    client.post(f"/api/v1/tasks/{tid}/start", headers=headers)
    body = client.get(f"/api/v1/tasks/{tid}").json()
    task = body.get("task", body)
    assert task["state"] == "PLANNING"
    assert seen == {"url": "https://github.com/octocat/p.git", "branch": "main", "pat": "ghp_token"}
    analysis = json.loads((tmp_path / "atlas-home" / "projects" / pid / "analysis" / f"{tid}.json").read_text())
    assert analysis["manifests"] == ["pyproject.toml"]
