"""Read-only status checks for the test sandbox. Nothing here starts a container or changes the VM."""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from atlas.sandbox.config import (
    APPROVED_HELPER_IMAGE,
    PROFILE,
    RESERVE_GB,
    VOLUME_NAME,
    SandboxPaths,
    start_command,
)

Runner = Callable[[list[str], dict[str, str]], "subprocess.CompletedProcess[str]"]

HOST_MOUNT_FSTYPES = frozenset({"virtiofs", "9p", "9pnet", "fuse.sshfs", "vboxsf", "fuse.virtiofs"})
GIB = 1024**3
VOLUME_COMMAND = (
    f"sudo diskutil apfs addVolume <container> APFS {VOLUME_NAME} -reserve {RESERVE_GB}g -quota {RESERVE_GB}g"
    '   # find <container> (e.g. disk3) with: diskutil apfs list'
)


@dataclass(frozen=True)
class Check:
    id: str
    label: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class SandboxStatus:
    ready: bool
    checks: list[Check]
    start_command: str
    volume_command: str = VOLUME_COMMAND

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "checks": [asdict(c) for c in self.checks],
            "start_command": self.start_command,
            "volume_command": self.volume_command,
        }


def base_env() -> dict[str, str]:
    """No DOCKER_HOST, no Docker context: the sandbox is only ever addressed by its explicit endpoint."""
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"), "HOME": os.environ.get("HOME", "")}


def default_runner(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=120, check=False)
    except FileNotFoundError:
        return subprocess.CompletedProcess(argv, 127, "", f"{argv[0]}: command not found")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"{argv[0]} timed out")


def docker(paths: SandboxPaths, runner: Runner, *args: str) -> subprocess.CompletedProcess[str]:
    return runner(["docker", "-H", paths.docker_endpoint, *args], base_env())


def colima(paths: SandboxPaths, runner: Runner, *args: str) -> subprocess.CompletedProcess[str]:
    return runner(["colima", *args], {**base_env(), "COLIMA_HOME": str(paths.colima_home)})


def check_storage(paths: SandboxPaths, runner: Runner) -> Check:
    label = f"Reserved storage ({RESERVE_GB} GB APFS volume {VOLUME_NAME})"
    proc = runner(["diskutil", "apfs", "list", "-plist"], base_env())
    if proc.returncode != 0:
        return Check("storage", label, False, "Could not read APFS volumes (diskutil failed).")
    try:
        data = plistlib.loads(proc.stdout.encode())
    except Exception:  # noqa: BLE001 - any malformed plist is a failed check
        return Check("storage", label, False, "Could not parse diskutil output.")
    for container in data.get("Containers", []):
        for volume in container.get("Volumes", []):
            if volume.get("Name") != VOLUME_NAME:
                continue
            quota = int(volume.get("CapacityQuota") or 0)
            reserve = int(volume.get("CapacityReserve") or 0)
            if quota <= 0 or reserve != quota:
                return Check("storage", label, False, "The volume needs reserve equal to quota (both set).")
            if quota < RESERVE_GB * GIB:
                return Check("storage", label, False, f"The volume is {quota // GIB} GB; at least {RESERVE_GB} GB is required.")
            if not paths.volume.is_dir():
                return Check("storage", label, False, f"The volume exists but is not mounted at {paths.volume}.")
            return Check("storage", label, True, f"{quota // GIB} GB reserved and capped")
    return Check("storage", label, False, f"No APFS volume named {VOLUME_NAME}. Create it with the command below.")


def check_data_path(paths: SandboxPaths) -> Check:
    label = "VM data is on the reserved volume"
    try:
        home = paths.colima_home.resolve(strict=True)
        same_device = os.stat(home).st_dev == os.stat(paths.volume).st_dev
    except OSError:
        return Check("data_path", label, False, f"{paths.colima_home} does not exist yet. Start the VM with the command below.")
    if not same_device or not home.is_relative_to(paths.volume.resolve()):
        return Check("data_path", label, False, f"{home} is not on the {VOLUME_NAME} volume.")
    return Check("data_path", label, True, str(home))


def check_vm_running(paths: SandboxPaths, runner: Runner) -> Check:
    label = f"Sandbox VM ({PROFILE}) is running"
    proc = colima(paths, runner, "list", "--json")
    for line in proc.stdout.splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("name") == PROFILE:
            ok = str(entry.get("status")).lower() == "running"
            return Check("vm_running", label, ok, f"status: {entry.get('status')}")
    return Check("vm_running", label, False, f"No colima profile named {PROFILE}.")


def check_no_host_mounts(paths: SandboxPaths, runner: Runner) -> Check:
    label = "The VM shares no folders from your computer"
    proc = colima(paths, runner, "ssh", "--profile", PROFILE, "--", "cat", "/proc/mounts")
    if proc.returncode != 0:
        return Check("no_host_mounts", label, False, "Could not list the VM's mounts.")
    shared = sorted(
        {parts[1] for parts in (ln.split() for ln in proc.stdout.splitlines()) if len(parts) >= 3 and parts[2] in HOST_MOUNT_FSTYPES}
    )
    if shared:
        return Check("no_host_mounts", label, False, f"Host folders are shared into the VM: {', '.join(shared)}")
    return Check("no_host_mounts", label, True, "no shared folders")


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
    """Run the checks in dependency order; once one fails the rest are reported as not checked."""
    p = paths or SandboxPaths()
    steps: list[tuple[str, str, Callable[[], Check]]] = [
        ("storage", "Reserved storage", lambda: check_storage(p, runner)),
        ("data_path", "VM data is on the reserved volume", lambda: check_data_path(p)),
        ("vm_running", "Sandbox VM is running", lambda: check_vm_running(p, runner)),
        ("no_host_mounts", "The VM shares no folders from your computer", lambda: check_no_host_mounts(p, runner)),
        ("daemon", "Docker daemon answers on the sandbox socket", lambda: check_daemon(p, runner)),
        ("image", "Approved helper image is present", lambda: check_image(p, runner)),
    ]
    checks: list[Check] = []
    blocked = False
    for check_id, label, run in steps:
        if blocked:
            checks.append(Check(check_id, label, False, "not checked yet"))
            continue
        result = run()
        checks.append(result)
        blocked = not result.ok
    return SandboxStatus(ready=all(c.ok for c in checks), checks=checks, start_command=start_command(p))


__all__ = ["Check", "Path", "Runner", "SandboxStatus", "default_runner", "sandbox_status"]
