"""The container contract (security-model.md 5.1.4): how every repository-code container is created, and the
check that its effective configuration matches before it is allowed to start."""

from __future__ import annotations

from typing import Any

from atlas.sandbox.config import APPROVED_HELPER_IMAGE

USER = "10001:10001"
PIDS_LIMIT = 128
SCRATCH_TMPFS = "/tmp:rw,nosuid,size=64m"  # noqa: S108 - a tmpfs inside the container, not the host
WRITABLE_TMPFS_SIZE = "256m"


def contract_args(
    execution_id: str,
    role: str,
    volume: str,
    command: list[str],
    *,
    image: str = APPROVED_HELPER_IMAGE,
    env: dict[str, str] | None = None,
    writable_paths: tuple[str, ...] = (),
    memory: str = "256m",
    cpus: str = "1",
    tmp_options: str = SCRATCH_TMPFS,
) -> list[str]:
    """`docker create` arguments for one container: no network, non-root, no capabilities, read-only root and
    source, size-capped tmpfs only where declared, bounded resources, labelled for recovery."""
    args = [
        "create", "--name", f"atlas-{execution_id}-{role}",
        "--network", "none",
        "--user", USER,
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--read-only",
        "--tmpfs", tmp_options,
        "--pids-limit", str(PIDS_LIMIT),
        "--memory", memory,
        "--cpus", cpus,
        "--log-driver", "local", "--log-opt", "max-size=1m",
        "--label", f"atlas.execution_id={execution_id}",
        "--label", f"atlas.role={role}",
        "--workdir", "/work",
        "-v", f"{volume}:/work:ro",
    ]
    for path in writable_paths:
        args += ["--tmpfs", f"/work/{path}:rw,nosuid,size={WRITABLE_TMPFS_SIZE},uid=10001,gid=10001,mode=0755"]
    for key, value in sorted((env or {}).items()):
        args += ["-e", f"{key}={value}"]
    return [*args, image, *command]


def verify_contract(
    inspected: dict[str, Any],
    volume: str,
    execution_id: str,
    role: str,
    *,
    image: str = APPROVED_HELPER_IMAGE,
    writable_paths: tuple[str, ...] = (),
) -> list[str]:
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
    expect(set(host.get("Tmpfs") or {}) == {"/tmp", *(f"/work/{p}" for p in writable_paths)}, "tmpfs mounts differ from the declared set")  # noqa: S108
    labels = config.get("Labels") or {}
    expect(labels.get("atlas.execution_id") == execution_id and labels.get("atlas.role") == role, "labels missing")
    expect(config.get("Image") == image, "image is not the approved digest")
    return problems
