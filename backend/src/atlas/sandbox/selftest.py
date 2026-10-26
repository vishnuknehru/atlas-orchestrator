"""Sandbox self-test: a container created under the full contract, verified with `inspect` before it starts,
then a trusted canary that asserts what repository code would be able to see. No repository code runs here."""

from __future__ import annotations

import json
from typing import Any

from atlas.sandbox.checks import Check, Runner, default_runner, docker, sandbox_status
from atlas.sandbox.config import APPROVED_HELPER_IMAGE, SandboxPaths
from atlas.utils import new_ulid

USER = "10001:10001"
PIDS_LIMIT = 128
MEMORY = "256m"
CPUS = "1"

# Busybox sh; prints `key value` lines that `parse_canary` turns into checks.
CANARY_SCRIPT = r"""
echo "net $(ls /sys/class/net | tr '\n' ' ')"
echo "uid $(id -u)"
if [ -e /var/run/docker.sock ] || [ -e /run/docker.sock ]; then echo "socket present"; else echo "socket none"; fi
if touch /work/probe 2>/dev/null; then echo "work writable"; else echo "work readonly"; fi
if touch /probe-rootfs 2>/dev/null; then echo "rootfs writable"; else echo "rootfs readonly"; fi
for d in /Users /Volumes /host /mnt/host /home/user; do [ -e "$d" ] && echo "hostpath $d"; done
echo "done"
"""


def contract_args(execution_id: str, role: str, volume: str) -> list[str]:
    """`docker create` arguments for one repository-code container (security-model.md 5.1.4)."""
    return [
        "create", "--name", f"atlas-{execution_id}-{role}",
        "--network", "none",
        "--user", USER,
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
        "--pids-limit", str(PIDS_LIMIT),
        "--memory", MEMORY,
        "--cpus", CPUS,
        "--log-driver", "local", "--log-opt", "max-size=1m",
        "--label", f"atlas.execution_id={execution_id}",
        "--label", f"atlas.role={role}",
        "-v", f"{volume}:/work:ro",
        APPROVED_HELPER_IMAGE,
        "sh", "-c", CANARY_SCRIPT,
    ]


def verify_contract(inspected: dict[str, Any], volume: str, execution_id: str, role: str) -> list[str]:
    """Deviations of the effective container configuration from the contract. Empty means it matches."""
    host = inspected.get("HostConfig", {})
    config = inspected.get("Config", {})
    problems: list[str] = []

    def expect(ok: bool, message: str) -> None:
        if not ok:
            problems.append(message)

    expect(host.get("NetworkMode") == "none", "network is not 'none'")
    expect(host.get("CapDrop") == ["ALL"] and not host.get("CapAdd"), "capabilities are not fully dropped")
    expect(host.get("Privileged") is False, "container is privileged")
    expect(host.get("ReadonlyRootfs") is True, "root filesystem is writable")
    expect(config.get("User") == USER, f"user is not {USER}")
    expect(any(o.startswith("no-new-privileges") for o in host.get("SecurityOpt") or []), "no-new-privileges missing")
    expect(int(host.get("PidsLimit") or 0) > 0, "no PID limit")
    expect(int(host.get("Memory") or 0) > 0, "no memory limit")
    expect(int(host.get("NanoCpus") or 0) > 0, "no CPU limit")
    expect(not host.get("Devices"), "devices are attached")
    expect(not host.get("Binds") or host.get("Binds") == [f"{volume}:/work:ro"], "unexpected bind mounts")
    mounts = inspected.get("Mounts") or []
    expect(
        len(mounts) == 1
        and mounts[0].get("Type") == "volume"
        and mounts[0].get("Name") == volume
        and mounts[0].get("Destination") == "/work"
        and mounts[0].get("RW") is False,
        "mounts are not exactly the read-only source volume",
    )
    expect("/tmp" in (host.get("Tmpfs") or {}), "/tmp is not a tmpfs")
    labels = config.get("Labels") or {}
    expect(labels.get("atlas.execution_id") == execution_id and labels.get("atlas.role") == role, "labels missing")
    expect(config.get("Image") == APPROVED_HELPER_IMAGE, "image is not the approved digest")
    return problems


def parse_canary(output: str) -> list[Check]:
    facts: dict[str, list[str]] = {}
    for line in output.splitlines():
        key, _, rest = line.partition(" ")
        facts.setdefault(key, []).append(rest.strip())
    finished = "done" in facts

    def one(key: str) -> str:
        return facts.get(key, [""])[0]

    return [
        Check("canary_complete", "Canary ran to the end", finished, "" if finished else "the canary did not finish"),
        Check("canary_network", "No network interface except loopback", one("net") == "lo", f"interfaces: {one('net') or 'unknown'}"),
        Check("canary_user", f"Runs as user {USER.split(':')[0]}", one("uid") == USER.split(":")[0], f"uid {one('uid') or 'unknown'}"),
        Check("canary_socket", "No Docker socket inside", one("socket") == "none", one("socket") or "unknown"),
        Check("canary_work_readonly", "Source (/work) is read-only", one("work") == "readonly", one("work") or "unknown"),
        Check("canary_rootfs_readonly", "Root filesystem is read-only", one("rootfs") == "readonly", one("rootfs") or "unknown"),
        Check("canary_no_host_paths", "No host folders visible", "hostpath" not in facts, ", ".join(facts.get("hostpath", [])) or "none"),
    ]


def run_self_test(paths: SandboxPaths | None = None, runner: Runner = default_runner) -> list[Check]:
    """Status checks first; only if the VM is sound do we create, verify and run the canary container."""
    p = paths or SandboxPaths()
    status = sandbox_status(p, runner)
    gate = [c for c in status.checks if c.id != "image"]
    if not all(c.ok for c in gate):
        return gate
    results = list(gate)
    if not status.checks[-1].ok:
        pull = docker(p, runner, "pull", "--quiet", APPROVED_HELPER_IMAGE)
        pulled = pull.returncode == 0
        results.append(Check("image", "Approved helper image downloaded by digest", pulled, "" if pulled else (pull.stderr or "pull failed").strip()[-200:]))
        if not pulled:
            return results

    execution_id = f"selftest-{new_ulid().lower()}"
    role = "test"
    volume = f"src-{execution_id}"
    name = f"atlas-{execution_id}-{role}"
    try:
        made = docker(p, runner, "volume", "create", "--label", f"atlas.execution_id={execution_id}", volume)
        if made.returncode != 0:
            return [*results, Check("volume", "Created the empty source volume", False, made.stderr.strip()[-200:])]
        created = docker(p, runner, *contract_args(execution_id, role, volume))
        if created.returncode != 0:
            return [*results, Check("contract", "Container created under the contract", False, created.stderr.strip()[-200:])]
        inspected = docker(p, runner, "inspect", name)
        try:
            problems = verify_contract(json.loads(inspected.stdout)[0], volume, execution_id, role)
        except (json.JSONDecodeError, IndexError, TypeError):
            problems = ["could not read the container's effective configuration"]
        results.append(Check("contract", "Effective container settings match the contract", not problems, "; ".join(problems) or "verified before start"))
        if problems:
            return results
        started = docker(p, runner, "start", "-a", name)
        results.extend(parse_canary(started.stdout))
        return results
    finally:
        docker(p, runner, "rm", "-f", name)
        docker(p, runner, "volume", "rm", "-f", volume)
