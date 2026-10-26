"""AgentBoundary: an outer OS sandbox around agent processes (security-model.md section 5.1.9).

The agent CLI keeps network access and its own config/auth directories. It loses access to ATLAS state,
container sockets and credentials, and can only write inside its own worktree, scratch space and CLI state.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

# Never readable by an agent process. ~/.atlas also holds every other project's worktree.
DENY_UNDER_HOME = (".atlas", ".colima", ".docker", ".ssh", ".aws", ".gnupg", ".config/gh")
DENY_ABSOLUTE = ("/var/run/docker.sock", "/run/docker.sock")
# Where an agent may write besides its worktree: scratch space and the CLI's own state.
WRITE_UNDER_HOME = (".claude", ".codex", ".cache")


class AgentBoundary(Protocol):
    def wrap(self, argv: Sequence[str], *, worktree: Path | None) -> list[str]: ...


def _real(path: Path) -> str:
    return os.path.realpath(path)


class SeatbeltBoundary:
    """macOS `sandbox-exec`. Paths are passed as -D parameters, never interpolated into the profile text."""

    def __init__(self, home: Path | None = None) -> None:
        self._home = Path(home) if home is not None else Path.home()
        self._profile_path: Path | None = None

    def _build(self) -> tuple[Path, list[str]]:
        params: list[str] = []
        denied: list[str] = []
        for i, rel in enumerate(DENY_UNDER_HOME):
            params += ["-D", f"DENY{i}={_real(self._home / rel)}"]
            denied.append(f'(subpath (param "DENY{i}"))')
        for j, absolute in enumerate(DENY_ABSOLUTE):
            params += ["-D", f"DENYABS{j}={absolute}"]
            denied.append(f'(literal (param "DENYABS{j}"))')
        params += ["-D", f"TMP={_real(Path(tempfile.gettempdir()))}", "-D", "VARTMP=/private/var/tmp"]
        params += ["-D", "VARFOLDERS=/private/var/folders"]
        writes = ['(subpath (param "WORKTREE"))', '(subpath (param "TMP"))', '(subpath (param "VARTMP"))']
        writes.append('(subpath (param "VARFOLDERS"))')
        for k, rel in enumerate(WRITE_UNDER_HOME):
            params += ["-D", f"HOMEW{k}={_real(self._home / rel)}"]
            writes.append(f'(subpath (param "HOMEW{k}"))')
        rules = [
            "(version 1)",
            "(allow default)",
            f"(deny file-read* {' '.join(denied)})",
            "(deny file-write*)",
            f'(allow file-write* {" ".join(writes)} (subpath "/dev"))',
        ]
        rules += [f"(deny network-outbound (remote unix-socket {d}))" for d in denied]
        if self._profile_path is None or not self._profile_path.exists():
            fd, name = tempfile.mkstemp(prefix="atlas-seatbelt-", suffix=".sb")
            with os.fdopen(fd, "w") as fh:
                fh.write("\n".join(rules) + "\n")
            self._profile_path = Path(name)
        return self._profile_path, params

    def wrap(self, argv: Sequence[str], *, worktree: Path | None) -> list[str]:
        profile, params = self._build()
        # Without a worktree nothing project-specific is writable.
        root = _real(worktree) if worktree is not None else "/nonexistent-atlas-worktree"
        return ["sandbox-exec", *params, "-D", f"WORKTREE={root}", "-f", str(profile), *argv]


def get_boundary() -> AgentBoundary | None:
    """The boundary for this platform, or None when none is available (writer roles then fail closed)."""
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        return SeatbeltBoundary()
    return None
