"""Candidate tests against the real sandbox (Colima). Skipped when the sandbox is not ready, e.g. in CI."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from atlas.sandbox import APPROVED_HELPER_IMAGE, SandboxPaths, default_runner, sandbox_status
from atlas.sandbox.checks import base_env
from atlas.sandbox.runner import TestSpec, reap_orphans, run_candidate_tests

pytestmark = pytest.mark.skipif(not sandbox_status().ready, reason="the Colima test sandbox is not ready")
_GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.hooksPath=/dev/null"]


def make_repo(tmp_path: Path, files: dict[str, str]) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "c"]):
        subprocess.run([*_GIT, *args], cwd=repo, check=True, capture_output=True)
    sha = subprocess.run([*_GIT, "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    return repo, sha


def spec(*commands: str, **kw: object) -> TestSpec:
    return TestSpec(image=APPROVED_HELPER_IMAGE, commands=list(commands), **kw)  # type: ignore[arg-type]


def leftovers() -> str:
    paths = SandboxPaths()
    out = ""
    for sub in (["ps", "-aq"], ["volume", "ls", "-q"]):
        out += default_runner(["docker", "-H", paths.docker_endpoint, *sub, "--filter", "label=atlas.execution_id"], base_env()).stdout
    return out.strip()


def test_passing_failing_and_skipped_commands(tmp_path: Path) -> None:
    repo, sha = make_repo(tmp_path, {"hello.txt": "hi\n", "check.sh": "exit 0\n"})
    run = run_candidate_tests(repo, sha, spec("cat hello.txt", "sh check.sh", "echo boom; exit 3", "echo never"))
    assert run.status == "failed" and run.source_attested
    assert [c.status for c in run.commands] == ["passed", "passed", "failed"]  # the fourth never ran
    assert run.commands[0].log.strip() == "hi" and run.commands[2].exit_code == 3 and "boom" in run.commands[2].log
    assert leftovers() == ""


def test_all_passing_is_a_pass(tmp_path: Path) -> None:
    repo, sha = make_repo(tmp_path, {"hello.txt": "hi\n"})
    run = run_candidate_tests(repo, sha, spec("test -f hello.txt", "test -r /work/hello.txt"))
    assert run.status == "passed" and run.source_attested and run.blocked_reason is None
    assert leftovers() == ""


def test_repository_code_cannot_change_the_source_reach_the_network_or_see_the_host(tmp_path: Path) -> None:
    repo, sha = make_repo(tmp_path, {"hello.txt": "hi\n"})
    attack = (
        "echo owned >> hello.txt 2>/dev/null && exit 10;"  # source is read-only
        "touch /work/newfile 2>/dev/null && exit 11;"
        "touch /probe 2>/dev/null && exit 12;"  # root filesystem is read-only
        "wget -T 3 -qO- http://1.1.1.1 >/dev/null 2>&1 && exit 13;"  # no network
        "[ \"$(ls /sys/class/net)\" = lo ] || exit 14;"
        "[ -e /var/run/docker.sock ] && exit 15;"
        "[ -e /Users ] && exit 16;"
        "[ \"$(id -u)\" = 10001 ] || exit 17;"
        "exit 0"
    )
    run = run_candidate_tests(repo, sha, spec(attack))
    assert run.commands[0].status == "passed", run.commands[0]
    assert run.source_attested and run.status == "passed"


def test_declared_writable_paths_are_the_only_writable_places(tmp_path: Path) -> None:
    repo, sha = make_repo(tmp_path, {"hello.txt": "hi\n"})
    run = run_candidate_tests(repo, sha, spec("echo cache > .pytest_cache/x && cat .pytest_cache/x", "touch other 2>/dev/null; test ! -e other",
                                              writable_paths=[".pytest_cache"]))
    assert run.status == "passed", [c.log for c in run.commands]


def test_timeouts_kill_the_container(tmp_path: Path) -> None:
    repo, sha = make_repo(tmp_path, {"a": "1"})
    run = run_candidate_tests(repo, sha, spec("sleep 120", timeout_seconds=10))
    assert run.commands[0].status == "timed_out" and run.status == "failed"
    assert leftovers() == ""


def test_unsupported_repositories_are_blocked_before_any_container_runs(tmp_path: Path) -> None:
    repo, sha = make_repo(tmp_path, {"pointer.bin": "version https://git-lfs.github.com/spec/v1\noid sha256:aa\nsize 3\n"})
    run = run_candidate_tests(repo, sha, spec("true"))
    assert (run.status, run.blocked_reason) == ("blocked", "unsupported_repo_feature")
    assert leftovers() == ""


def test_orphans_from_a_crashed_run_are_reaped(tmp_path: Path) -> None:
    paths = SandboxPaths()
    default_runner(["docker", "-H", paths.docker_endpoint, "volume", "create", "--label", "atlas.execution_id=t-crashed", "src-t-crashed"], base_env())
    assert reap_orphans() >= 0
    assert leftovers() == ""
