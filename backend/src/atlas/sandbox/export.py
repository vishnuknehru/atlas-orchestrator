"""Candidate export (security-model.md 5.1.2): a tar built directly from git objects plus the expected manifest.

No `.gitattributes` processing, no smudge filters, no `.git` directory. Anything that cannot be exported
faithfully blocks the run with `unsupported_repo_feature`.
"""

from __future__ import annotations

import hashlib
import io
import os
import posixpath
import re
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path

MAX_FILES = 20_000
MAX_TOTAL_BYTES = 200 * 1024 * 1024
MAX_PATH_BYTES = 1024
MAX_COMPONENT_BYTES = 255
GIT_TIMEOUT_SECONDS = 120
_LFS_POINTER = b"version https://git-lfs.github.com/spec"
_ENTRY = re.compile(rb"^(\d{6}) (blob|commit|tree) ([0-9a-f]{40,64})\t(.+)$", re.DOTALL)


class ExportError(RuntimeError):
    """The candidate cannot be exported faithfully. `code` is the blocked reason."""

    def __init__(self, detail: str, code: str = "unsupported_repo_feature") -> None:
        super().__init__(detail)
        self.code = code


@dataclass(frozen=True)
class ManifestEntry:
    kind: str  # "f" regular file, "l" symlink
    mode: str  # "644" / "755"; "-" for symlinks
    sha256: str  # content hash; for symlinks the hash of the link text
    path: str

    def line(self) -> str:
        return f"{self.kind} {self.mode} {self.sha256} ./{self.path}"


@dataclass(frozen=True)
class CandidateExport:
    tar: bytes
    manifest: list[ManifestEntry]


def _git(repo: Path, args: list[str], stdin: bytes | None = None) -> bytes:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"), "HOME": os.environ.get("HOME", "")}
    try:
        proc = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args], cwd=repo, env=env, input=stdin,
            capture_output=True, timeout=GIT_TIMEOUT_SECONDS, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise ExportError("git is unavailable or timed out", "export_failed") from exc
    if proc.returncode != 0:
        raise ExportError(proc.stderr.decode(errors="replace").strip()[-300:] or "git failed", "export_failed")
    return proc.stdout


def _read_blobs(repo: Path, oids: list[str]) -> dict[str, bytes]:
    """Read blobs with one `git cat-file --batch` process."""
    out = _git(repo, ["cat-file", "--batch"], ("\n".join(oids) + "\n").encode())
    blobs: dict[str, bytes] = {}
    pos = 0
    for oid in oids:
        end = out.index(b"\n", pos)
        header = out[pos:end].split()
        if len(header) != 3 or header[1] != b"blob":
            raise ExportError(f"object {oid} is not a blob", "export_failed")
        size = int(header[2])
        blobs[oid] = out[end + 1 : end + 1 + size]
        pos = end + 1 + size + 1
    return blobs


def _check_path(path: str) -> None:
    raw = path.encode()
    if len(raw) > MAX_PATH_BYTES or any(len(part.encode()) > MAX_COMPONENT_BYTES for part in path.split("/")):
        raise ExportError(f"path too long: {path[:80]}")
    if any(ord(c) < 32 or c == "\\" for c in path) or path.startswith("/") or ".." in path.split("/") or "" in path.split("/"):
        raise ExportError(f"unsupported path: {path[:80]!r}")


def _check_symlink(path: str, target: str) -> None:
    if target.startswith("/") or not target or any(ord(c) < 32 for c in target):
        raise ExportError(f"symlink {path} has an absolute or empty target")
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
    if resolved == ".." or resolved.startswith("../") or resolved.startswith("/"):
        raise ExportError(f"symlink {path} escapes the tree")


def export_candidate(repo: Path, commit_sha: str, empty_dirs: list[str] | None = None) -> CandidateExport:
    """Build the input tar for `commit_sha`. `empty_dirs` are mountpoints for the project's writable paths."""
    if not re.fullmatch(r"[0-9a-f]{7,64}", commit_sha):
        raise ExportError("invalid commit", "export_failed")
    listing = _git(repo, ["ls-tree", "-r", "-z", "--full-tree", commit_sha])
    raw_entries = [e for e in listing.split(b"\0") if e]
    if len(raw_entries) > MAX_FILES:
        raise ExportError(f"more than {MAX_FILES} files")
    entries: list[tuple[str, str, str]] = []
    for raw in raw_entries:
        match = _ENTRY.match(raw)
        if not match:
            raise ExportError("unreadable tree entry", "export_failed")
        mode, kind, oid, path_b = match.group(1).decode(), match.group(2).decode(), match.group(3).decode(), match.group(4)
        try:
            path = path_b.decode()
        except UnicodeDecodeError as exc:
            raise ExportError("a path is not valid UTF-8") from exc
        if kind == "commit" or mode == "160000":
            raise ExportError(f"submodule at {path}")
        if mode not in ("100644", "100755", "120000"):
            raise ExportError(f"unsupported file mode {mode} at {path}")
        _check_path(path)
        entries.append((mode, oid, path))

    lowered: dict[str, str] = {}
    for _, _, path in entries:
        if lowered.setdefault(path.lower(), path) != path:
            raise ExportError(f"paths collide on case-insensitive systems: {lowered[path.lower()]} and {path}")

    blobs = _read_blobs(repo, sorted({oid for _, oid, _ in entries}))
    if sum(len(b) for b in blobs.values()) > MAX_TOTAL_BYTES:
        raise ExportError("candidate is larger than the export limit")
    for _, oid, path in entries:
        if blobs[oid].startswith(_LFS_POINTER):
            raise ExportError(f"Git LFS pointer at {path}")
        if posixpath.basename(path) == ".gitattributes" and b"filter=lfs" in blobs[oid]:
            raise ExportError(f"Git LFS configured in {path}")

    manifest: list[ManifestEntry] = []
    buffer = io.BytesIO()
    dirs: set[str] = set()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        def add_dirs(path: str) -> None:
            parts = path.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                d = "/".join(parts[:i])
                if d not in dirs:
                    dirs.add(d)
                    info = tarfile.TarInfo(d)
                    info.type, info.mode, info.mtime = tarfile.DIRTYPE, 0o755, 0
                    tar.addfile(info)

        for mode, oid, path in sorted(entries, key=lambda e: e[2]):
            data = blobs[oid]
            add_dirs(path)
            info = tarfile.TarInfo(path)
            info.mtime = 0
            if mode == "120000":
                target = data.decode(errors="strict")
                _check_symlink(path, target)
                info.type, info.linkname, info.mode = tarfile.SYMTYPE, target, 0o777
                tar.addfile(info)
                manifest.append(ManifestEntry("l", "-", hashlib.sha256((target + "\n").encode()).hexdigest(), path))
            else:
                info.size = len(data)
                info.mode = 0o755 if mode == "100755" else 0o644
                tar.addfile(info, io.BytesIO(data))
                manifest.append(ManifestEntry("f", "755" if mode == "100755" else "644", hashlib.sha256(data).hexdigest(), path))
        tracked = {path for _, _, path in entries}
        for d in sorted(empty_dirs or []):
            parts = d.split("/")
            prefixes = {"/".join(parts[: i + 1]) for i in range(len(parts))}
            if d in dirs or prefixes & tracked or any(p.lower() in lowered for p in prefixes):
                raise ExportError(f"writable path {d} overlaps a tracked path", "unsafe_writable_path")
            info = tarfile.TarInfo(d)
            info.type, info.mode, info.mtime = tarfile.DIRTYPE, 0o755, 0
            tar.addfile(info)
    return CandidateExport(buffer.getvalue(), sorted(manifest, key=lambda m: m.path))
