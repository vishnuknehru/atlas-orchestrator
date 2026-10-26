"""Per-task git worktrees: the only place a writer agent may edit files (security-model.md section 5.3).

Worktrees isolate git state, not processes; process isolation comes from the AgentBoundary. Every git call
runs with a scrubbed environment and repository hooks disabled.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

GIT_TIMEOUT_SECONDS = 120
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_SHA = re.compile(r"^[0-9a-f]{7,64}$")
_IDENTITY = ["-c", "user.name=ATLAS", "-c", "user.email=atlas@localhost", "-c", "commit.gpgsign=false"]


class WorktreeError(RuntimeError):
    """A worktree operation failed or was refused."""


def worktree_root(projects_dir: Path, project_id: str) -> Path:
    return projects_dir / project_id / "worktrees"


def _git(args: list[str], cwd: Path) -> str:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"), "HOME": os.environ.get("HOME", "")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    cmd = ["git", "-c", "core.hooksPath=/dev/null", *_IDENTITY, *args]
    try:
        proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS, check=False)
    except FileNotFoundError as exc:
        raise WorktreeError("git is not installed or not on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise WorktreeError("git took too long.") from exc
    if proc.returncode != 0:
        lines = [ln for ln in (proc.stderr or proc.stdout).strip().splitlines() if ln.strip()]
        raise WorktreeError(lines[-1][:300] if lines else "git failed")
    return proc.stdout


def _inside(root: Path, path: Path) -> Path:
    resolved = Path(os.path.realpath(path))
    if not resolved.is_relative_to(Path(os.path.realpath(root))) or resolved == Path(os.path.realpath(root)):
        raise WorktreeError(f"{path} is not inside the worktree root")
    return resolved


def head_sha(repo: Path) -> str:
    return _git(["rev-parse", "HEAD"], repo).strip()


def create_worktree(clone_path: Path, root: Path, name: str, base_sha: str) -> Path:
    """Check out `base_sha` (detached) in a fresh directory under `root`. An existing one is replaced."""
    if not _NAME.fullmatch(name):
        raise WorktreeError(f"Invalid worktree name {name!r}")
    if not _SHA.fullmatch(base_sha):
        raise WorktreeError(f"Invalid base commit {base_sha!r}")
    root.mkdir(parents=True, exist_ok=True)
    path = _inside(root, root / name)
    if path.exists():
        remove_worktree(clone_path, root, path)
    _git(["worktree", "add", "--detach", str(path), base_sha], clone_path)
    return path


def commit_all(worktree: Path, message: str) -> str | None:
    """Commit every change in the worktree as a candidate. Returns the new SHA, or None if nothing changed."""
    _git(["add", "-A"], worktree)
    if not _git(["status", "--porcelain"], worktree).strip():
        return None
    _git(["commit", "-q", "-m", message], worktree)
    return head_sha(worktree)


def changed_files(worktree: Path, base_sha: str) -> list[str]:
    out = _git(["diff", "--name-only", f"{base_sha}..HEAD"], worktree)
    return [ln for ln in out.splitlines() if ln]


def remove_worktree(clone_path: Path, root: Path, path: Path) -> None:
    resolved = _inside(root, path)
    try:
        _git(["worktree", "remove", "--force", str(resolved)], clone_path)
    except WorktreeError:
        shutil.rmtree(resolved, ignore_errors=True)
        _git(["worktree", "prune"], clone_path)
