"""Test runner logic against a scripted Docker (the real-sandbox tests live in test_sandbox_runner_real.py)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from atlas.sandbox import APPROVED_HELPER_IMAGE
from atlas.sandbox.export import export_candidate
from atlas.sandbox.runner import TestSpec, _manifest_lines, run_candidate_tests
from tests.test_sandbox import FakeRunner, inspected
from tests.test_sandbox_export import commit

IMAGE = "python:3.12-slim@sha256:" + "a" * 64


class ScriptedDocker(FakeRunner):
    """A FakeRunner that also understands populate, attest, wait and logs."""

    def __init__(self, tmp: Path, repo: Path, sha: str, *, exit_codes: list[int] | None = None, attest_after: str | None = None,
                 contract_break: dict[str, Any] | None = None, wait_returncode: int = 0, writable: tuple[str, ...] = ()) -> None:
        super().__init__(tmp)
        self.expected = _manifest_lines(export_candidate(repo, sha, empty_dirs=list(writable)))
        self.exit_codes = list(exit_codes or [0])
        self.attest_after, self.contract_break, self.wait_returncode = attest_after, contract_break or {}, wait_returncode
        self.attests = 0
        self.started: list[str] = []
        self.populated: list[bytes] = []
        self.writable = writable

    def __call__(self, argv: list[str], env: dict[str, str], *, stdin: bytes | None = None, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
        if argv[0] != "docker":
            return super().__call__(argv, env, stdin=stdin, timeout=timeout)
        self.calls.append(argv)
        self.envs.append(env)
        sub = argv[3:]

        def ok(out: str = "", code: int = 0) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, code, out, "")

        if sub[0] == "run":
            self.populated.append(stdin or b"")
            return ok()
        if sub[0] == "create":
            self.created.append(sub[2])
            return ok("cid\n")
        if sub[0] == "inspect":
            name = sub[1]
            execution_id = name.split("-test")[0].split("-attest")[0].removeprefix("atlas-")
            role = name.removeprefix(f"atlas-{execution_id}-")
            data = inspected(f"src-{execution_id}", execution_id, **self.contract_break)
            data["Config"]["Labels"]["atlas.role"] = role
            data["Config"]["Image"] = IMAGE if role.startswith("test") else APPROVED_HELPER_IMAGE
            if role.startswith("test"):
                data["HostConfig"]["Tmpfs"].update({f"/work/{p}": "rw" for p in self.writable})
            data["State"] = {"OOMKilled": False}
            return ok(json.dumps([data]))
        if sub[0] == "start" and sub[1] == "-a":  # attestation container
            self.attests += 1
            lines = self.expected if not (self.attest_after and self.attests > 1) else [self.attest_after]
            return ok("\n".join([*lines, "end"]) + "\n")
        if sub[0] == "start":
            self.started.append(sub[1])
            return ok()
        if sub[0] == "wait":
            if self.wait_returncode:
                return ok("", self.wait_returncode)
            return ok(f"{self.exit_codes.pop(0) if self.exit_codes else 0}\n")
        if sub[0] == "logs":
            return ok("test output\n")
        if sub[0] == "ps":
            return ok("c1\n" if "label=atlas.execution_id" in " ".join(sub) else "")
        if sub[0] == "volume" and sub[1] == "ls":
            return ok(f"src-{self.created[0].split('-test')[0].removeprefix('atlas-')}\n" if self.created else "")
        if sub[0] == "image" and sub[1] == "inspect":
            return ok("sha256:x\n")
        return super().__call__(argv, env, stdin=stdin, timeout=timeout)


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    import subprocess as sp

    r = tmp_path / "r"
    r.mkdir()
    sp.run(["git", "init", "-q", "-b", "main"], cwd=r, check=True, capture_output=True)
    return r, commit(r, {"a.py": "x = 1\n", "tests/test_a.py": "def test(): pass\n"})


def spec(*commands: str, **kw: Any) -> TestSpec:
    return TestSpec(image=IMAGE, commands=list(commands), **kw)


def verbs(r: ScriptedDocker) -> list[str]:
    return [c[3] for c in r.calls if c[0] == "docker"]


def test_spec_requires_a_digest_pinned_image_and_clean_paths() -> None:
    for image in ("python:3.12", "python@sha256:abc", "python:latest", "evil;rm@sha256:" + "a" * 64):
        with pytest.raises(ValidationError):
            TestSpec(image=image, commands=["true"])
    for path in ("/abs", "../up", "a/../b", "./x", "a//b", ".", "a/"):
        with pytest.raises(ValidationError):
            TestSpec(image=IMAGE, commands=["true"], writable_paths=[path])
    with pytest.raises(ValidationError):
        TestSpec(image=IMAGE, commands=[])
    assert TestSpec(image=IMAGE, commands=["pytest -q"], writable_paths=[".venv", "build/out"]).timeout_seconds == 600


def test_passing_run_populates_attests_verifies_then_starts_and_cleans_up(tmp_path: Path, repo: tuple[Path, str]) -> None:
    r, sha = repo
    docker = ScriptedDocker(tmp_path, r, sha)
    run = run_candidate_tests(r, sha, spec("pytest -q"), docker.paths, docker)
    assert run.status == "passed" and run.source_attested and run.commands[0].exit_code == 0
    calls = [c[3:] for c in docker.calls if c[0] == "docker"]
    name = next(c[2] for c in calls if c[0] == "create" and c[2].endswith("-test1"))
    order = [i for i, c in enumerate(calls) if (c[0] == "create" and c[2] == name) or (c[0] == "inspect" and c[1] == name) or (c[0] == "start" and c[1] == name)]
    assert [calls[i][0] for i in order][:3] == ["create", "inspect", "start"]  # verified before it starts
    assert [i for i, c in enumerate(calls) if c[0] == "run"][0] < order[0]  # the source was populated first
    assert len(docker.populated) == 1 and len(docker.populated[0]) > 0  # the tar was streamed over stdin
    assert docker.attests == 2  # before and after
    assert calls[-1][0] == "volume" and any(c[0] == "rm" for c in calls)  # cleanup ran last


def test_failing_command_stops_the_remaining_commands(tmp_path: Path, repo: tuple[Path, str]) -> None:
    r, sha = repo
    docker = ScriptedDocker(tmp_path, r, sha, exit_codes=[0, 2, 0])
    run = run_candidate_tests(r, sha, spec("a", "b", "c"), docker.paths, docker)
    assert run.status == "failed" and [c.status for c in run.commands] == ["passed", "failed"]
    assert len(docker.started) == 2


def test_a_container_that_breaks_the_contract_is_never_started(tmp_path: Path, repo: tuple[Path, str]) -> None:
    r, sha = repo
    docker = ScriptedDocker(tmp_path, r, sha, contract_break={"HostConfig__NetworkMode": "bridge"})
    run = run_candidate_tests(r, sha, spec("pytest"), docker.paths, docker)
    assert run.status == "failed" and run.commands[0].status == "error" and "network" in run.commands[0].log
    assert docker.started == []


def test_source_changed_during_the_run_invalidates_the_evidence(tmp_path: Path, repo: tuple[Path, str]) -> None:
    r, sha = repo
    docker = ScriptedDocker(tmp_path, r, sha, attest_after="f 644 deadbeef ./a.py")
    run = run_candidate_tests(r, sha, spec("pytest"), docker.paths, docker)
    assert (run.status, run.blocked_reason) == ("blocked", "invalid_source_mutated") and not run.source_attested


def test_timeout_kills_the_container_and_is_reported(tmp_path: Path, repo: tuple[Path, str]) -> None:
    r, sha = repo
    docker = ScriptedDocker(tmp_path, r, sha, wait_returncode=124)
    run = run_candidate_tests(r, sha, spec("sleep 999", timeout_seconds=10), docker.paths, docker)
    assert run.commands[0].status == "timed_out" and run.status == "failed"
    assert any(c[3] == "kill" for c in docker.calls if c[0] == "docker")


def test_sandbox_not_ready_blocks_before_anything_is_created(tmp_path: Path, repo: tuple[Path, str]) -> None:
    r, sha = repo
    docker = ScriptedDocker(tmp_path, r, sha)
    docker.vm = "stopped"
    run = run_candidate_tests(r, sha, spec("pytest"), docker.paths, docker)
    assert (run.status, run.blocked_reason) == ("blocked", "no_test_sandbox")
    assert "create" not in verbs(docker) and "run" not in verbs(docker)


def test_writable_paths_become_size_capped_tmpfs_owned_by_the_test_user(tmp_path: Path, repo: tuple[Path, str]) -> None:
    r, sha = repo
    docker = ScriptedDocker(tmp_path, r, sha, writable=(".pytest_cache",))
    run = run_candidate_tests(r, sha, spec("pytest", writable_paths=[".pytest_cache"]), docker.paths, docker)
    assert run.status == "passed"
    create = next(c for c in docker.calls if c[0] == "docker" and c[3] == "create" and "--name" in c and c[c.index("--name") + 1].endswith("-test1"))
    assert "/work/.pytest_cache:rw,nosuid,size=256m,uid=10001,gid=10001,mode=0755" in create
    assert "--network" in create and create[create.index("--network") + 1] == "none"


def test_unsafe_writable_path_blocks_before_any_container(tmp_path: Path, repo: tuple[Path, str]) -> None:
    r, sha = repo
    docker = ScriptedDocker(tmp_path, r, sha)
    run = run_candidate_tests(r, sha, spec("pytest", writable_paths=["tests"]), docker.paths, docker)
    assert (run.status, run.blocked_reason) == ("blocked", "unsafe_writable_path")
    assert "create" not in verbs(docker)
