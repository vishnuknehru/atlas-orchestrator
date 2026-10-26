"""Names, paths and the approved helper image for the test sandbox.

The sandbox is a set of locked-down containers in the user's existing Colima VM (decision D16). There is no
dedicated VM and no reserved disk; the protection is the per-container contract and the explicit socket.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PROFILE = "default"

# Publisher: Docker Official Image (library/alpine). Tag alpine:3.20, multi-arch index digest, approved 2026-10-26.
# Only the pinned digest is ever run; the tag is documentation.
APPROVED_HELPER_IMAGE = "alpine:3.20@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc"


def _default_profile() -> str:
    return os.environ.get("ATLAS_SANDBOX_PROFILE", DEFAULT_PROFILE)


@dataclass(frozen=True)
class SandboxPaths:
    """Which Colima profile hosts the containers. Overridable so tests never touch the real VM."""

    profile: str = field(default_factory=_default_profile)
    colima_home: Path = field(default_factory=lambda: Path.home() / ".colima")

    @property
    def docker_socket(self) -> Path:
        return self.colima_home / self.profile / "docker.sock"

    @property
    def docker_endpoint(self) -> str:
        return f"unix://{self.docker_socket}"


def start_command(paths: SandboxPaths | None = None) -> str:
    """Starts the Colima VM that hosts the containers. Leaves the user's Docker context alone."""
    p = paths or SandboxPaths()
    profile = "" if p.profile == DEFAULT_PROFILE else f" {shlex.quote(p.profile)}"
    return f"colima start{profile} --activate=false"
