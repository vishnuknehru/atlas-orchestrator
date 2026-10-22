"""Platform SecretStore interface (macOS Keychain, Linux Secret Service) and exact-value Redactor."""

from __future__ import annotations

import re
import subprocess
import sys
import threading
from typing import Protocol


class SecretStoreUnavailableError(RuntimeError):
    """Raised when the OS secret store is unavailable (maps to BLOCKED(no_secret_store))."""


class SecretStore(Protocol):
    """OS-backed secret store interface (ADR-007 §6). Never falls back to plaintext files."""

    def get_secret(self, service: str, account: str | None = None) -> str | None:
        """Retrieve a secret by service name, or None if not stored."""
        ...

    def set_secret(self, service: str, account: str, secret: str) -> None:
        """Store a secret under service and account."""
        ...

    def delete_secret(self, service: str, account: str | None = None) -> None:
        """Delete a secret if present."""
        ...


def _security_quote(value: str) -> str:
    """Quote a value for the `security -i` command line parser."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class MacOSKeychainSecretStore:
    """macOS Keychain implementation using /usr/bin/security (no shell; secrets never on argv)."""

    def get_secret(self, service: str, account: str | None = None) -> str | None:
        argv = ["/usr/bin/security", "find-generic-password", "-s", service]
        if account:
            argv.extend(["-a", account])
        argv.append("-w")
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=5.0, check=False)
        except OSError as exc:
            raise SecretStoreUnavailableError(f"macOS security CLI unavailable: {exc}") from exc
        if proc.returncode != 0:
            return None
        return proc.stdout.rstrip("\r\n")

    def set_secret(self, service: str, account: str, secret: str) -> None:
        # `security add-generic-password -w <secret>` would expose the secret in the process list,
        # so the command is fed to `security -i` on stdin instead.
        if any(c in value for value in (service, account, secret) for c in "\r\n"):
            raise ValueError("Keychain service, account and secret must not contain newlines")
        command = (
            f"add-generic-password -U -s {_security_quote(service)} "
            f"-a {_security_quote(account)} -w {_security_quote(secret)}\n"
        )
        try:
            proc = subprocess.run(
                ["/usr/bin/security", "-i"],
                input=command,
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
        except OSError as exc:
            raise SecretStoreUnavailableError(f"macOS security CLI unavailable: {exc}") from exc
        if proc.returncode != 0:
            raise SecretStoreUnavailableError(
                f"Failed to store secret in macOS Keychain (exit {proc.returncode})"
            )

    def delete_secret(self, service: str, account: str | None = None) -> None:
        argv = ["/usr/bin/security", "delete-generic-password", "-s", service]
        if account:
            argv.extend(["-a", account])
        subprocess.run(argv, capture_output=True, text=True, timeout=5.0, check=False)


class LinuxSecretServiceStore:
    """Linux Secret Service implementation using secret-tool via argv (no shell)."""

    def get_secret(self, service: str, account: str | None = None) -> str | None:
        argv = ["secret-tool", "lookup", "service", service]
        if account:
            argv.extend(["username", account])
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=5.0, check=False)
        except FileNotFoundError as exc:
            raise SecretStoreUnavailableError("secret-tool is not installed") from exc
        except OSError as exc:
            raise SecretStoreUnavailableError(f"Secret Service error: {exc}") from exc
        if proc.returncode != 0:
            return None
        return proc.stdout.rstrip("\r\n")

    def set_secret(self, service: str, account: str, secret: str) -> None:
        argv = [
            "secret-tool",
            "store",
            f"--label=ATLAS {service}",
            "service",
            service,
            "username",
            account,
        ]
        try:
            proc = subprocess.run(
                argv,
                input=secret,
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
        except FileNotFoundError as exc:
            raise SecretStoreUnavailableError("secret-tool is not installed") from exc
        except OSError as exc:
            raise SecretStoreUnavailableError(f"Secret Service error: {exc}") from exc
        if proc.returncode != 0:
            raise SecretStoreUnavailableError(
                f"Failed to store secret in Secret Service (exit {proc.returncode})"
            )

    def delete_secret(self, service: str, account: str | None = None) -> None:
        argv = ["secret-tool", "clear", "service", service]
        if account:
            argv.extend(["username", account])
        subprocess.run(argv, capture_output=True, text=True, timeout=5.0, check=False)


class InMemorySecretStore:
    """In-memory SecretStore used by unit and integration tests."""

    def __init__(self) -> None:
        self._secrets: dict[str, tuple[str, str]] = {}

    def get_secret(self, service: str, account: str | None = None) -> str | None:
        entry = self._secrets.get(service)
        if not entry:
            return None
        stored_account, secret = entry
        if account is not None and stored_account != account:
            return None
        return secret

    def set_secret(self, service: str, account: str, secret: str) -> None:
        self._secrets[service] = (account, secret)

    def delete_secret(self, service: str, account: str | None = None) -> None:
        self._secrets.pop(service, None)


def get_default_secret_store() -> SecretStore:
    """Return the platform-specific SecretStore (macOS Keychain or Linux Secret Service)."""
    if sys.platform == "darwin":
        return MacOSKeychainSecretStore()
    if sys.platform.startswith("linux"):
        return LinuxSecretServiceStore()
    raise SecretStoreUnavailableError(f"Unsupported platform {sys.platform!r} (D13)")


# ---------------------------------------------------------------------------
# Exact-value & pattern Redactor (security-model.md §3.3, ADR-007 §5)
# ---------------------------------------------------------------------------

_KNOWN_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"gh[opsu]_[A-Za-z0-9_]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"
    ),
)


class Redactor:
    """Registers exact secret values before use and scrubs them + known token patterns."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._exact_secrets: set[str] = set()

    def register(self, secret: str) -> None:
        """Register an exact secret value before any subprocess or HTTP request uses it."""
        cleaned = secret.strip()
        if len(cleaned) >= 4:
            with self._lock:
                self._exact_secrets.add(cleaned)

    def redact(self, text: str) -> str:
        """Remove all registered exact secret values and known token patterns from text."""
        if not text:
            return text
        result = text
        with self._lock:
            # Sort longest first so prefixes don't leave partial suffixes
            secrets_sorted = sorted(self._exact_secrets, key=len, reverse=True)
        for s in secrets_sorted:
            if s in result:
                result = result.replace(s, "[REDACTED]")
        for pattern in _KNOWN_SECRET_PATTERNS:
            result = pattern.sub("[REDACTED]", result)
        return result
