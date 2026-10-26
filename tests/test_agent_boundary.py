"""Seatbelt AgentBoundary: what an agent-launched command can and cannot touch (macOS only)."""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from atlas.agents import SeatbeltBoundary

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or subprocess.run(["which", "sandbox-exec"], capture_output=True).returncode != 0,
    reason="Seatbelt is macOS only",
)


@pytest.fixture
def world() -> Iterator[dict[str, Path]]:
    # Not under the system temp dir (scratch there is deliberately writable by agents), and short enough
    # for a unix-socket path.
    base = Path(tempfile.mkdtemp(prefix=".atlas-bt-", dir=Path.home()))
    home = base / "home"
    for d in (".atlas", ".ssh", ".colima/atlas", ".claude", "project/worktree", "project/other"):
        (home / d).mkdir(parents=True)
    (home / ".atlas" / "session_token").write_text("secret")
    (home / ".ssh" / "id_ed25519").write_text("key")
    try:
        yield {"home": home, "wt": home / "project" / "worktree", "other": home / "project" / "other"}
    finally:
        shutil.rmtree(base, ignore_errors=True)


def run(world: dict[str, Path], script: str) -> subprocess.CompletedProcess[str]:
    argv = SeatbeltBoundary(home=world["home"]).wrap(["/bin/sh", "-c", script], worktree=world["wt"])
    return subprocess.run(argv, capture_output=True, text=True, timeout=30)


def test_agent_can_write_its_worktree_and_scratch(world: dict[str, Path]) -> None:
    r = run(world, f"echo hi > {world['wt']}/new.txt && echo x > $TMPDIR/scratch && echo ok")
    assert r.returncode == 0 and (world["wt"] / "new.txt").read_text() == "hi\n"


def test_agent_cannot_write_outside_its_worktree(world: dict[str, Path]) -> None:
    for target in (world["other"] / "x", world["home"] / "x", world["home"] / ".atlas" / "x"):
        r = run(world, f"echo leak > {target}")
        assert r.returncode != 0 and not target.exists(), target


def test_agent_cannot_read_atlas_state_or_credentials(world: dict[str, Path]) -> None:
    for target in (world["home"] / ".atlas" / "session_token", world["home"] / ".ssh" / "id_ed25519"):
        r = run(world, f"cat {target}")
        assert r.returncode != 0 and "secret" not in r.stdout and "key" not in r.stdout


def test_agent_can_still_use_its_cli_state_and_read_the_project(world: dict[str, Path]) -> None:
    (world["other"] / "readme").write_text("visible")
    r = run(world, f"echo s > {world['home']}/.claude/state && cat {world['other']}/readme")
    assert r.returncode == 0 and r.stdout == "visible"


def test_agent_cannot_connect_to_a_container_socket(world: dict[str, Path]) -> None:
    sock_path = world["home"] / ".colima" / "atlas" / "d.sock"
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(sock_path))
    server.listen(1)
    try:
        script = f"/usr/bin/python3 -c \"import socket; s=socket.socket(socket.AF_UNIX); s.connect('{sock_path}')\""
        assert run(world, script).returncode != 0
        assert subprocess.run(["/usr/bin/python3", "-c", script.split('"')[1]], capture_output=True).returncode == 0
    finally:
        server.close()


def test_no_worktree_means_nothing_project_specific_is_writable(world: dict[str, Path]) -> None:
    argv = SeatbeltBoundary(home=world["home"]).wrap(["/bin/sh", "-c", f"echo x > {world['wt']}/f"], worktree=None)
    assert subprocess.run(argv, capture_output=True, timeout=30).returncode != 0

