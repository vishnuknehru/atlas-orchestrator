"""Checkout and deterministic scan of a project's repository (the `analyse` workflow step)."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from atlas.config.secret_store import SecretStore, SecretStoreUnavailableError
from atlas.engine.workflow import CapabilityUnavailableError, StepHandlerFn, StepHandlerResult
from atlas.fsm.states import TaskState

GIT_TIMEOUT_SECONDS = 300
README_CHARS = 4000
MAX_FILES = 50_000
MANIFESTS = frozenset(
    {
        "pyproject.toml", "requirements.txt", "setup.py", "package.json", "pnpm-lock.yaml", "yarn.lock",
        "Cargo.toml", "go.mod", "pom.xml", "build.gradle", "Gemfile", "composer.json", "Dockerfile",
        "Makefile", "docker-compose.yml",
    }
)
_SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", "target"})


class CheckoutError(RuntimeError):
    """The repository could not be cloned or updated. `code` becomes the task's blocked reason."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


def _git_env(pat: str) -> dict[str, str]:
    """Environment for git. The token travels in env config, never in argv, so `ps` can't show it."""
    basic = base64.b64encode(f"x-access-token:{pat}".encode()).decode()
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"),
        "HOME": os.environ.get("HOME", ""),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
    }


def _git(args: list[str], *, env: dict[str, str], cwd: Path | None = None) -> None:
    # Hooks from the repository must never run on the host.
    cmd = ["git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.file.allow=user", *args]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, env=env, cwd=cwd, timeout=GIT_TIMEOUT_SECONDS, check=False
        )
    except FileNotFoundError as exc:
        raise CheckoutError("git_not_installed", "git is not installed or not on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise CheckoutError("clone_failed", "git took too long to reach the repository.") from exc
    if proc.returncode != 0:
        lines = [ln for ln in (proc.stderr or proc.stdout).strip().splitlines() if ln.strip()]
        raise CheckoutError("clone_failed", lines[-1][:300] if lines else "git failed")


def ensure_checkout(clone_path: Path, remote_url: str, base_branch: str, pat: str) -> None:
    """Clone the repository on first use, otherwise fast-forward to the remote base branch."""
    env = _git_env(pat)
    if (clone_path / ".git").is_dir():
        _git(["fetch", "--depth", "1", "origin", base_branch], env=env, cwd=clone_path)
        _git(["checkout", "--detach", "--force", "FETCH_HEAD"], env=env, cwd=clone_path)
        return
    clone_path.parent.mkdir(parents=True, exist_ok=True)
    _git(["clone", "--depth", "1", "--branch", base_branch, remote_url, str(clone_path)], env=env)


def scan_repository(root: Path) -> dict[str, Any]:
    """Deterministic facts about a checkout. Symlinks are never followed and nothing is executed."""
    extensions: Counter[str] = Counter()
    manifests: list[str] = []
    top_level = sorted(p.name for p in root.iterdir() if p.name != ".git")
    total = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for name in sorted(filenames):
            total += 1
            if total > MAX_FILES:
                truncated = True
                break
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            extensions[path.suffix.lower() or name] += 1
            if name in MANIFESTS:
                manifests.append(str(path.relative_to(root)))
        if truncated:
            break
    readme = ""
    for candidate in sorted(root.glob("README*")):
        if candidate.is_file() and not candidate.is_symlink():
            readme = candidate.read_text(errors="replace")[:README_CHARS]
            break
    return {
        "top_level": top_level,
        "file_count": min(total, MAX_FILES),
        "truncated": truncated,
        "extensions": dict(extensions.most_common(15)),
        "manifests": sorted(manifests),
        "readme_excerpt": readme,
    }


def make_analyse_handler(store: SecretStore, projects_dir: Path) -> StepHandlerFn:
    """Build the `analyse` step handler: checkout, scan, write analysis.json, move the task to PLANNING."""

    def handler(conn: sqlite3.Connection, project_id: str, entity_id: str, step_id: str) -> StepHandlerResult:
        proj = conn.execute(
            "SELECT github_repo, base_branch, clone_path FROM projects WHERE id = ?;", (project_id,)
        ).fetchone()
        if proj is None:
            raise CapabilityUnavailableError("project_missing")
        repo = str(proj["github_repo"])
        try:
            pat = store.get_secret(f"atlas.{project_id}.github") or store.get_secret(f"atlas.{repo.lower()}.github")
        except SecretStoreUnavailableError as exc:
            raise CapabilityUnavailableError("no_secret_store") from exc
        if not pat:
            raise CapabilityUnavailableError("no_github_token")
        clone_path = Path(str(proj["clone_path"]))
        try:
            ensure_checkout(clone_path, f"https://github.com/{repo}.git", str(proj["base_branch"]), pat)
            analysis = scan_repository(clone_path)
        except CheckoutError as exc:
            raise CapabilityUnavailableError(exc.code) from exc
        out = projects_dir / project_id / "analysis" / f"{entity_id}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(analysis, indent=2, sort_keys=True), encoding="utf-8")
        return StepHandlerResult(
            to_state=TaskState.PLANNING.value,
            reason=f"Repository analysed: {analysis['file_count']} files",
            result_ref=str(out),
        )

    return handler
