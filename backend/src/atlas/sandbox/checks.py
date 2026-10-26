"""Read-only status checks for the test sandbox. Nothing here starts a container or changes the VM."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from atlas.sandbox.config import APPROVED_HELPER_IMAGE, SandboxPaths, start_command


class Runner(Protocol):
    """Runs one command. `stdin` is streamed to it; `timeout` (seconds) overrides the default."""

    def __call__(
        self, argv: list[str], env: dict[str, str], *, stdin: bytes | None = None, timeout: int | None = None
    ) -> subprocess.CompletedProcess[str]: ...


HOST_MOUNT_FSTYPES = frozenset({"virtiofs", "9p", "9pnet", "fuse.sshfs", "vboxsf", "fuse.virtiofs"})


@dataclass(frozen=True)
class Check:
    id: str
    label: str
    ok: bool
    detail: str = ""
    # A "warning" is shown to the user but never blocks the sandbox (accepted trade-off, decision D16).
    level: str = "required"


@dataclass(frozen=True)
class SandboxStatus:
    ready: bool
    checks: list[Check]
    start_command: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "checks": [asdict(c) for c in self.checks],
            "start_command": self.start_command,
        }


def base_env() -> dict[str, str]:
    """No DOCKER_HOST, no Docker context: the sandbox is only ever addressed by its explicit endpoint."""
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"), "HOME": os.environ.get("HOME", "")}


def default_runner(
    argv: list[str], env: dict[str, str], *, stdin: bytes | None = None, timeout: int | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(argv, env=env, input=stdin, capture_output=True, timeout=timeout or 120, check=False)
    except FileNotFoundError:
        return subprocess.CompletedProcess(argv, 127, "", f"{argv[0]}: command not found")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"{argv[0]} timed out")
    return subprocess.CompletedProcess(
        argv, proc.returncode, proc.stdout.decode(errors="replace"), proc.stderr.decode(errors="replace")
    )


def docker(
    paths: SandboxPaths, runner: Runner, *args: str, stdin: bytes | None = None, timeout: int | None = None
) -> subprocess.CompletedProcess[str]:
    return runner(["docker", "-H", paths.docker_endpoint, *args], base_env(), stdin=stdin, timeout=timeout)


def colima(paths: SandboxPaths, runner: Runner, *args: str) -> subprocess.CompletedProcess[str]:
    return runner(["colima", *args], {**base_env(), "COLIMA_HOME": str(paths.colima_home)}, stdin=None, timeout=None)


def check_vm_running(paths: SandboxPaths, runner: Runner) -> Check:
    label = f"Colima VM ({paths.profile}) is running"
    proc = colima(paths, runner, "list", "--json")
    for line in proc.stdout.splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("name") == paths.profile:
            ok = str(entry.get("status")).lower() == "running"
            return Check("vm_running", label, ok, f"status: {entry.get('status')}")
    return Check("vm_running", label, False, f"No colima profile named {paths.profile}.")


def check_host_mounts(paths: SandboxPaths, runner: Runner) -> Check:
    label = "The VM shares no folders from your computer"
    proc = colima(paths, runner, "ssh", "--profile", paths.profile, "--", "cat", "/proc/mounts")
    if proc.returncode != 0:
        return Check("host_mounts", label, False, "Could not list the VM's mounts.", level="warning")
    shared = sorted(
        {parts[1] for parts in (ln.split() for ln in proc.stdout.splitlines()) if len(parts) >= 3 and parts[2] in HOST_MOUNT_FSTYPES}
    )
    if shared:
        return Check(
            "host_mounts", label, False,
            f"Shared into the VM: {', '.join(shared)}. Containers never get these, but a container escape would reach them.",
            level="warning",
        )
    return Check("host_mounts", label, True, "no shared folders", level="warning")


def check_daemon(paths: SandboxPaths, runner: Runner) -> Check:
    label = "Docker daemon answers on the sandbox socket"
    proc = docker(paths, runner, "version", "--format", "{{.Server.Version}}")
    if proc.returncode != 0 or not proc.stdout.strip():
        return Check("daemon", label, False, f"No answer on {paths.docker_endpoint}.")
    return Check("daemon", label, True, f"Docker {proc.stdout.strip()}")


def check_image(paths: SandboxPaths, runner: Runner) -> Check:
    label = "Approved helper image is present"
    proc = docker(paths, runner, "image", "inspect", "--format", "{{.Id}}", APPROVED_HELPER_IMAGE)
    if proc.returncode != 0:
        return Check("image", label, False, "Not downloaded yet; the self-test downloads it by its pinned digest.")
    return Check("image", label, True, APPROVED_HELPER_IMAGE.split("@")[0])


def sandbox_status(paths: SandboxPaths | None = None, runner: Runner = default_runner) -> SandboxStatus:
    """Run the checks in dependency order; once a required one fails the rest are reported as not checked."""
    p = paths or SandboxPaths()
    steps: list[tuple[str, str, Callable[[], Check]]] = [
        ("vm_running", "Colima VM is running", lambda: check_vm_running(p, runner)),
        ("daemon", "Docker daemon answers on the Colima socket", lambda: check_daemon(p, runner)),
        ("image", "Approved helper image is present", lambda: check_image(p, runner)),
        ("host_mounts", "The VM shares no folders from your computer", lambda: check_host_mounts(p, runner)),
    ]
    checks: list[Check] = []
    blocked = False
    for check_id, label, run in steps:
        level = "warning" if check_id == "host_mounts" else "required"
        if blocked:
            checks.append(Check(check_id, label, False, "not checked yet", level=level))
            continue
        result = run()
        checks.append(result)
        # A missing image doesn't stop the remaining checks; a stopped VM or silent daemon does.
        blocked = not result.ok and check_id in ("vm_running", "daemon")
    ready = all(c.ok for c in checks if c.level == "required")
    return SandboxStatus(ready=ready, checks=checks, start_command=start_command(p))


__all__ = ["Check", "Runner", "SandboxStatus", "default_runner", "sandbox_status"]
