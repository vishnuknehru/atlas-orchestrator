"""Run a candidate's test commands in the sandbox (security-model.md 5.1.3 - 5.1.7).

Flow: export the candidate from git objects -> populate a fresh source volume -> attest it -> run each command in a
container created under the contract and verified before start -> attest the source again -> clean up.
The source is read-only for every command, so repository code cannot alter what it is testing.
"""

from __future__ import annotations

import json
import posixpath
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from atlas.sandbox.checks import Check, Runner, default_runner, docker, sandbox_status
from atlas.sandbox.config import APPROVED_HELPER_IMAGE, SandboxPaths
from atlas.sandbox.contract import USER, contract_args, verify_contract
from atlas.sandbox.export import CandidateExport, ExportError, export_candidate
from atlas.utils import new_ulid, utc_now_iso

LOG_CHARS = 64_000
MAX_COMMANDS = 20
PINNED_IMAGE = re.compile(r"^[a-z0-9]+([._/-][a-z0-9]+)*(:[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}$")
TEST_ENV = {
    "CI": "1", "HOME": "/tmp", "TMPDIR": "/tmp",  # noqa: S108 - paths inside the container
    "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPYCACHEPREFIX": "/tmp/pyc",  # noqa: S108
}
ATTEST_SCRIPT = r"""
cd /work || exit 3
find . -mindepth 1 \( -type f -o -type l \) | LC_ALL=C sort | while IFS= read -r p; do
  if [ -L "$p" ]; then echo "l - $(readlink "$p" | sha256sum | cut -d' ' -f1) $p"
  else echo "f $(stat -c %a "$p") $(sha256sum "$p" | cut -d' ' -f1) $p"; fi
done
echo "end"
"""


class TestSpec(BaseModel):
    """What to run for a project. The image must be pinned by digest (repository Dockerfile builds are disabled)."""

    __test__ = False  # not a pytest class
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    image: str
    commands: list[str] = Field(min_length=1, max_length=MAX_COMMANDS)
    writable_paths: list[str] = Field(default_factory=list, max_length=10)
    timeout_seconds: int = Field(default=600, ge=10, le=3600)
    memory: str = "1g"
    cpus: str = "2"

    @field_validator("image")
    @classmethod
    def _pinned(cls, value: str) -> str:
        if not PINNED_IMAGE.fullmatch(value):
            raise ValueError("The test image must be pinned by digest, e.g. python:3.12-slim@sha256:<64 hex digits>")
        return value

    @field_validator("commands")
    @classmethod
    def _commands(cls, value: list[str]) -> list[str]:
        if any(not c.strip() or len(c) > 2000 or "\0" in c for c in value):
            raise ValueError("Each test command must be 1-2000 characters")
        return value

    @field_validator("writable_paths")
    @classmethod
    def _paths(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        for raw in value:
            norm = posixpath.normpath(raw)
            if raw != norm or norm in (".", "") or norm.startswith(("/", "..")) or ".." in norm.split("/") or "\0" in raw:
                raise ValueError(f"Writable path {raw!r} must be a clean relative path inside the repository")
            if norm.lower() in seen:
                raise ValueError(f"Writable path {raw!r} is listed twice")
            seen.add(norm.lower())
        return value


def validate_test_config(tests: dict[str, Any]) -> None:
    """Raise ValueError when a project's stored test settings could not be run. Missing pieces are allowed."""
    image = tests.get("image")
    if image and not PINNED_IMAGE.fullmatch(image):
        raise ValueError("The test image must be pinned by digest, e.g. python:3.12-slim@sha256:<64 hex digits>")
    try:
        TestSpec(
            image=image or "placeholder@sha256:" + "0" * 64,
            commands=tests.get("commands") or ["true"],
            writable_paths=tests.get("writable_paths") or [],
            timeout_seconds=int(tests.get("timeout_seconds") or 600),
        )
    except ValidationError as exc:
        raise ValueError(exc.errors()[0]["msg"]) from exc


@dataclass
class CommandResult:
    command: str
    status: str  # passed | failed | timed_out | error
    exit_code: int | None = None
    seconds: float = 0.0
    log: str = ""


@dataclass
class TestRun:
    __test__ = False

    status: str  # passed | failed | blocked
    commit_sha: str
    blocked_reason: str | None = None
    detail: str = ""
    image: str = ""
    started_at: str = ""
    finished_at: str = ""
    commands: list[CommandResult] = field(default_factory=list)
    source_attested: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _blocked(commit_sha: str, reason: str, detail: str, started: str, image: str = "") -> TestRun:
    return TestRun("blocked", commit_sha, reason, detail, image, started, utc_now_iso())


def _manifest_lines(export: CandidateExport) -> list[str]:
    return sorted(m.line() for m in export.manifest)


def _attest(paths: SandboxPaths, runner: Runner, execution_id: str, volume: str, expected: list[str], tag: str) -> str | None:
    """Recompute the source manifest inside the VM from a read-only mount. Returns a problem or None."""
    name = f"atlas-{execution_id}-attest-{tag}"
    created = docker(
        paths, runner,
        *contract_args(execution_id, f"attest-{tag}", volume, ["sh", "-c", ATTEST_SCRIPT]),
    )
    if created.returncode != 0:
        return f"attestation container could not be created: {created.stderr.strip()[-200:]}"
    started = docker(paths, runner, "start", "-a", name, timeout=120)
    lines = [ln for ln in started.stdout.splitlines() if ln]
    if started.returncode != 0 or not lines or lines[-1] != "end":
        return "attestation did not complete"
    actual = sorted(lines[:-1])
    if actual != expected:
        extra = sorted(set(actual) - set(expected))[:3]
        missing = sorted(set(expected) - set(actual))[:3]
        return f"source differs from the candidate (extra: {extra}, changed or missing: {missing})"
    return None


def _truncate(text: str) -> str:
    return text if len(text) <= LOG_CHARS else text[:LOG_CHARS // 2] + "\n... [truncated] ...\n" + text[-LOG_CHARS // 2:]


def _run_command(
    paths: SandboxPaths, runner: Runner, spec: TestSpec, execution_id: str, volume: str, index: int, command: str
) -> CommandResult:
    role = f"test{index}"
    name = f"atlas-{execution_id}-{role}"
    args = contract_args(
        execution_id, role, volume, ["sh", "-c", command],
        image=spec.image, env=TEST_ENV, writable_paths=tuple(spec.writable_paths), memory=spec.memory, cpus=spec.cpus,
    )
    created = docker(paths, runner, *args)
    if created.returncode != 0:
        return CommandResult(command, "error", log=f"container could not be created: {created.stderr.strip()[-300:]}")
    try:
        inspected = json.loads(docker(paths, runner, "inspect", name).stdout)[0]
    except (ValueError, IndexError, TypeError):
        return CommandResult(command, "error", log="could not read the container's effective configuration")
    problems = verify_contract(
        inspected, volume, execution_id, role, image=spec.image, writable_paths=tuple(spec.writable_paths)
    )
    if problems:
        return CommandResult(command, "error", log="container does not match the contract: " + "; ".join(problems))

    began = time.monotonic()
    if docker(paths, runner, "start", name).returncode != 0:
        return CommandResult(command, "error", log="container did not start")
    waited = docker(paths, runner, "wait", name, timeout=spec.timeout_seconds)
    timed_out = waited.returncode == 124  # the runner's timeout code
    if timed_out:
        docker(paths, runner, "kill", name)
        waited = docker(paths, runner, "wait", name, timeout=30)
    seconds = round(time.monotonic() - began, 2)
    logs = docker(paths, runner, "logs", "--tail", "2000", name)
    log = _truncate((logs.stdout or "") + (logs.stderr or ""))
    if timed_out:
        return CommandResult(command, "timed_out", None, seconds, log + f"\n[killed after {spec.timeout_seconds}s]")
    try:
        code = int(waited.stdout.strip())
    except ValueError:
        return CommandResult(command, "error", None, seconds, log + "\n[no exit code]")
    try:
        oom = bool(json.loads(docker(paths, runner, "inspect", name).stdout)[0]["State"].get("OOMKilled"))
    except (ValueError, IndexError, KeyError, TypeError):
        oom = False
    if oom:
        log += "\n[out of memory]"
    return CommandResult(command, "passed" if code == 0 else "failed", code, seconds, log)


def cleanup(paths: SandboxPaths, runner: Runner, execution_id: str) -> None:
    """Kill and remove everything labelled with this execution, then confirm none is left running."""
    listing = docker(paths, runner, "ps", "-aq", "--filter", f"label=atlas.execution_id={execution_id}")
    for cid in listing.stdout.split():
        docker(paths, runner, "kill", cid)
        docker(paths, runner, "rm", "-f", cid)
    volumes = docker(paths, runner, "volume", "ls", "-q", "--filter", f"label=atlas.execution_id={execution_id}")
    for vol in volumes.stdout.split():
        docker(paths, runner, "volume", "rm", "-f", vol)


def reap_orphans(paths: SandboxPaths | None = None, runner: Runner = default_runner) -> int:
    """Remove atlas-labelled containers and volumes left by a crashed run. Call before the first run after startup."""
    p = paths or SandboxPaths()
    if not sandbox_status(p, runner).ready:
        return 0
    listing = docker(p, runner, "ps", "-aq", "--filter", "label=atlas.execution_id")
    ids = listing.stdout.split()
    for cid in ids:
        docker(p, runner, "kill", cid)
        docker(p, runner, "rm", "-f", cid)
    for vol in docker(p, runner, "volume", "ls", "-q", "--filter", "label=atlas.execution_id").stdout.split():
        docker(p, runner, "volume", "rm", "-f", vol)
    return len(ids)


def run_candidate_tests(
    repo: Path,
    commit_sha: str,
    spec: TestSpec,
    paths: SandboxPaths | None = None,
    runner: Runner = default_runner,
) -> TestRun:
    p = paths or SandboxPaths()
    started = utc_now_iso()
    status = sandbox_status(p, runner)
    if not status.ready:
        failing = next((c for c in status.checks if not c.ok and c.level == "required"), None)
        return _blocked(commit_sha, "no_test_sandbox", failing.detail if failing else "sandbox not ready", started, spec.image)
    try:
        export = export_candidate(repo, commit_sha, empty_dirs=list(spec.writable_paths))
    except ExportError as exc:
        return _blocked(commit_sha, exc.code, str(exc), started, spec.image)

    execution_id = f"t-{new_ulid().lower()}"
    volume = f"src-{execution_id}"
    expected = _manifest_lines(export)
    run = TestRun("failed", commit_sha, image=spec.image, started_at=started)
    try:
        if docker(p, runner, "image", "inspect", spec.image).returncode != 0:
            pulled = docker(p, runner, "pull", "--quiet", spec.image, timeout=900)
            if pulled.returncode != 0:
                return _blocked(commit_sha, "test_image_unavailable", pulled.stderr.strip()[-300:], started, spec.image)
        if docker(p, runner, "volume", "create", "--label", f"atlas.execution_id={execution_id}", volume).returncode != 0:
            return _blocked(commit_sha, "sandbox_state_unknown", "could not create the source volume", started, spec.image)
        populated = docker(
            p, runner, "run", "--rm", "-i", "--network", "none", "--user", "0", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--read-only", "--pids-limit", "64", "--memory", "256m",
            "--label", f"atlas.execution_id={execution_id}", "--label", "atlas.role=populate",
            "-v", f"{volume}:/work", APPROVED_HELPER_IMAGE, "tar", "-x", "-p", "-C", "/work", "-f", "-",
            stdin=export.tar, timeout=300,
        )
        if populated.returncode != 0:
            return _blocked(commit_sha, "sandbox_state_unknown", "could not populate the source volume", started, spec.image)
        problem = _attest(p, runner, execution_id, volume, expected, "before")
        if problem:
            return _blocked(commit_sha, "source_attestation_failed", problem, started, spec.image)
        for index, command in enumerate(spec.commands, start=1):
            result = _run_command(p, runner, spec, execution_id, volume, index, command)
            run.commands.append(result)
            if result.status != "passed":
                break  # later commands depend on earlier ones
        problem = _attest(p, runner, execution_id, volume, expected, "after")
        if problem:
            return _blocked(commit_sha, "invalid_source_mutated", problem, started, spec.image)
        run.source_attested = True
        ok = len(run.commands) == len(spec.commands) and all(c.status == "passed" for c in run.commands)
        run.status = "passed" if ok else "failed"
        run.finished_at = utc_now_iso()
        return run
    finally:
        cleanup(p, runner, execution_id)


__all__ = [
    "Check", "CommandResult", "TestRun", "TestSpec", "cleanup", "reap_orphans", "run_candidate_tests", "USER",
]
