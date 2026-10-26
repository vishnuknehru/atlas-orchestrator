"""Candidate export: a tar built from git objects, and refusal of anything that cannot be exported faithfully."""

from __future__ import annotations

import hashlib
import io
import subprocess
import tarfile
from pathlib import Path

import pytest

from atlas.sandbox.export import ExportError, export_candidate

_GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.hooksPath=/dev/null"]


def git(repo: Path, *args: str) -> str:
    return subprocess.run([*_GIT, *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    return r


def commit(repo: Path, files: dict[str, str | tuple[str, str]]) -> str:
    for rel, content in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, tuple):  # ("symlink", target)
            target.symlink_to(content[1])
        else:
            target.write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "c")
    return git(repo, "rev-parse", "HEAD")


def commit_paths(repo: Path, files: dict[str, str]) -> str:
    """Commit through git plumbing, for paths this filesystem cannot hold (case collisions, huge names)."""
    for path, content in files.items():
        oid = subprocess.run([*_GIT, "hash-object", "-w", "--stdin"], cwd=repo, input=content, check=True, capture_output=True, text=True).stdout.strip()
        git(repo, "update-index", "--add", "--cacheinfo", f"100644,{oid},{path}")
    git(repo, "commit", "-q", "-m", "c")
    return git(repo, "rev-parse", "HEAD")


def test_tar_matches_the_git_tree_and_manifest(repo: Path) -> None:
    (repo / "bin").mkdir()
    (repo / "bin" / "run.sh").write_text("#!/bin/sh\necho hi\n")
    (repo / "bin" / "run.sh").chmod(0o755)
    sha = commit(repo, {"src/app.py": "x = 1\n", "README.md": "hello\n", "link": ("symlink", "src/app.py")})
    export = export_candidate(repo, sha, empty_dirs=[".pytest_cache"])

    with tarfile.open(fileobj=io.BytesIO(export.tar)) as tar:
        names = {m.name: m for m in tar.getmembers()}
        assert names["src/app.py"].isfile() and tar.extractfile("src/app.py").read() == b"x = 1\n"  # type: ignore[union-attr]
        assert names["bin/run.sh"].mode == 0o755 and names["README.md"].mode == 0o644
        assert names["link"].issym() and names["link"].linkname == "src/app.py"
        assert names[".pytest_cache"].isdir()
        assert ".git" not in names and not any(n.startswith(".git/") for n in names)
        assert all(m.mtime == 0 for m in tar.getmembers())  # reproducible
    by_path = {m.path: m for m in export.manifest}
    assert by_path["src/app.py"].sha256 == hashlib.sha256(b"x = 1\n").hexdigest()
    assert (by_path["bin/run.sh"].mode, by_path["README.md"].mode, by_path["link"].kind) == ("755", "644", "l")
    assert ".pytest_cache" not in by_path  # mountpoints are not tracked content
    assert [m.path for m in export.manifest] == sorted(by_path)


def test_gitattributes_and_filters_are_not_applied(repo: Path) -> None:
    sha = commit(repo, {".gitattributes": "secret.txt export-ignore\n*.txt filter=nothing\n", "secret.txt": "kept\n"})
    export = export_candidate(repo, sha)
    with tarfile.open(fileobj=io.BytesIO(export.tar)) as tar:
        assert tar.extractfile("secret.txt").read() == b"kept\n"  # type: ignore[union-attr]


def test_submodules_are_refused(repo: Path) -> None:
    commit(repo, {"a": "1"})
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{git(repo, 'rev-parse', 'HEAD')},vendor/lib")
    git(repo, "commit", "-q", "-m", "gitlink")
    with pytest.raises(ExportError, match="submodule"):
        export_candidate(repo, git(repo, "rev-parse", "HEAD"))


@pytest.mark.parametrize(
    ("files", "fragment"),
    [
        ({"l": ("symlink", "/etc/passwd")}, "absolute"),
        ({"d/l": ("symlink", "../../outside")}, "escapes"),
        ({"pointer.bin": "version https://git-lfs.github.com/spec/v1\noid sha256:aa\nsize 3\n"}, "LFS"),
        ({".gitattributes": "*.bin filter=lfs diff=lfs merge=lfs -text\n"}, "LFS"),
    ],
)
def test_unsupported_entries_block_the_run(repo: Path, files: dict[str, str | tuple[str, str]], fragment: str) -> None:
    sha = commit(repo, files)
    with pytest.raises(ExportError, match=fragment) as err:
        export_candidate(repo, sha)
    assert err.value.code == "unsupported_repo_feature"


@pytest.mark.parametrize(
    ("files", "fragment"),
    [({"A.txt": "1", "a.txt": "2"}, "collide"), ({"x" * 300: "1"}, "too long")],
)
def test_paths_this_filesystem_cannot_hold_are_still_refused(repo: Path, files: dict[str, str], fragment: str) -> None:
    with pytest.raises(ExportError, match=fragment) as err:
        export_candidate(repo, commit_paths(repo, files))
    assert err.value.code == "unsupported_repo_feature"


def test_writable_path_overlapping_tracked_content_is_refused(repo: Path) -> None:
    sha = commit(repo, {"build/out.txt": "tracked\n"})
    with pytest.raises(ExportError) as err:
        export_candidate(repo, sha, empty_dirs=["build"])
    assert err.value.code == "unsafe_writable_path"


def test_a_symlink_that_stays_inside_the_tree_is_fine(repo: Path) -> None:
    sha = commit(repo, {"a/real.txt": "1", "a/l": ("symlink", "real.txt"), "b/up": ("symlink", "../a/real.txt")})
    assert {m.path for m in export_candidate(repo, sha).manifest} == {"a/real.txt", "a/l", "b/up"}


def test_rejects_a_malformed_commit_id(repo: Path) -> None:
    with pytest.raises(ExportError):
        export_candidate(repo, "--output=/tmp/x")
