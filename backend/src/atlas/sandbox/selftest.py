"""Sandbox self-test: a container created under the full contract, verified with `inspect` before it starts,
then a trusted canary that asserts what repository code would be able to see. No repository code runs here."""

from __future__ import annotations

import json

from atlas.sandbox.checks import Check, Runner, default_runner, docker, sandbox_status
from atlas.sandbox.config import APPROVED_HELPER_IMAGE, SandboxPaths
from atlas.sandbox.contract import USER, contract_args, verify_contract
from atlas.utils import new_ulid

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
    by_id = {c.id: c for c in status.checks}
    gate = [by_id["vm_running"], by_id["daemon"]]
    if not all(c.ok for c in gate):
        return gate
    results = [*gate, by_id["host_mounts"]]
    if not by_id["image"].ok:
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
        created = docker(p, runner, *contract_args(
            execution_id, role, volume, ["sh", "-c", CANARY_SCRIPT],
            tmp_options="/tmp:rw,noexec,nosuid,size=16m",
        ))
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
