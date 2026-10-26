"""Fixed names, paths and the approved helper image for the test sandbox."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

PROFILE = "atlas"
VOLUME_NAME = "AtlasSandbox"
RESERVE_GB = 80
VM_DISK_GB = 60
VM_CPUS = 2
VM_MEMORY_GB = 4

# Publisher: Docker Official Image (library/alpine). Tag alpine:3.20, multi-arch index digest, approved 2026-10-26.
# Only the pinned digest is ever run; the tag is documentation.
APPROVED_HELPER_IMAGE = "alpine:3.20@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc"


@dataclass(frozen=True)
class SandboxPaths:
    """Where the sandbox VM lives. Overridable so tests never touch the real volume."""

    volume: Path = Path("/Volumes") / VOLUME_NAME

    @property
    def colima_home(self) -> Path:
        return self.volume / "colima"

    @property
    def docker_socket(self) -> Path:
        return self.colima_home / PROFILE / "docker.sock"

    @property
    def docker_endpoint(self) -> str:
        return f"unix://{self.docker_socket}"


def start_command(paths: SandboxPaths | None = None) -> str:
    """The exact command that starts the sandbox VM: data on the reserved volume, nothing shared from the host,
    and the user's current Docker context left alone."""
    p = paths or SandboxPaths()
    return (
        f"COLIMA_HOME={shlex.quote(str(p.colima_home))} colima start {PROFILE} --vm-type vz "
        f"--mount none --activate=false --cpus {VM_CPUS} --memory {VM_MEMORY_GB} --disk {VM_DISK_GB}"
    )
