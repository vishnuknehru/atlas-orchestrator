"""Worktree manager: isolated checkouts, candidate commits, and refusal to leave the worktree root."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from atlas.worktrees import (
    WorktreeError,
    changed_files,
    commit_all,
    create_worktree,
    head_sha,
    remove_worktree,
)

_GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    (r / "a.py").write_text("x = 1\n")
    for args in (["init", "-q", "-b", "main"], ["add", "."], ["commit", "-q", "-m", "init"]):
        subprocess.run([*_GIT, *args], cwd=r, check=True, capture_output=True)
    return r


def test_worktree_is_isolated_and_commits_become_candidates(repo: Path, tmp_path: Path) -> None:
    root = tmp_path / "wts"
    base = head_sha(repo)
    wt = create_worktree(repo, root, "task-1", base)
    (wt / "b.py").write_text("y = 2\n")
    (wt / "a.py").write_text("x = 3\n")
    sha = commit_all(wt, "candidate")
    assert sha and sha != base and head_sha(repo) == base
    assert not (repo / "b.py").exists()
    assert sorted(changed_files(wt, base)) == ["a.py", "b.py"]
    assert commit_all(wt, "nothing new") is None


def test_recreating_a_worktree_starts_clean(repo: Path, tmp_path: Path) -> None:
    root = tmp_path / "wts"
    base = head_sha(repo)
    wt = create_worktree(repo, root, "task-1", base)
    (wt / "junk").write_text("left over")
    again = create_worktree(repo, root, "task-1", base)
    assert again == wt and not (again / "junk").exists()


def test_hooks_in_the_repository_never_run(repo: Path, tmp_path: Path) -> None:
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {tmp_path}/hook-ran\n")
    hook.chmod(0o755)
    wt = create_worktree(repo, tmp_path / "wts", "t", head_sha(repo))
    (wt / "c").write_text("1")
    assert commit_all(wt, "m")
    assert not (tmp_path / "hook-ran").exists()


@pytest.mark.parametrize("name", ["../escape", "a/b", "", ".hidden", "x" * 200])
def test_unsafe_names_are_refused(repo: Path, tmp_path: Path, name: str) -> None:
    with pytest.raises(WorktreeError):
        create_worktree(repo, tmp_path / "wts", name, head_sha(repo))


def test_refuses_bad_commit_and_paths_outside_the_root(repo: Path, tmp_path: Path) -> None:
    with pytest.raises(WorktreeError):
        create_worktree(repo, tmp_path / "wts", "t", "--upload-pack=evil")
    with pytest.raises(WorktreeError):
        remove_worktree(repo, tmp_path / "wts", repo)
    assert (repo / "a.py").exists()


def test_remove_worktree(repo: Path, tmp_path: Path) -> None:
    root = tmp_path / "wts"
    wt = create_worktree(repo, root, "t", head_sha(repo))
    remove_worktree(repo, root, wt)
    assert not wt.exists()
