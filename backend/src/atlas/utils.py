"""Core utilities: ULID generation, UTC ISO-8601 timestamps, and content-addressed blobs."""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

from ulid import ULID


def new_ulid() -> str:
    """Return a monotonic-sortable 26-char ULID string."""
    return str(ULID())


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(UTC).isoformat()


def ensure_secure_dir(path: Path) -> Path:
    """Ensure a directory exists with 0700 permissions."""
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def ensure_secure_file(path: Path) -> None:
    """Set 0600 permissions on a file if it exists."""
    if path.exists():
        os.chmod(path, 0o600)


def sha256_bytes(data: bytes) -> str:
    """Return hex SHA-256 digest of bytes."""
    return hashlib.sha256(data).hexdigest()
