"""CLI entrypoint for ATLAS (`atlas serve`)."""

from __future__ import annotations

import argparse
import copy
import logging
import os
import re
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any

import uvicorn
from uvicorn.config import LOGGING_CONFIG

from atlas import __version__
from atlas.api.app import create_app
from atlas.api.security import load_or_create_session_token
from atlas.utils import ensure_secure_dir

_TOKEN_QUERY_RE = re.compile(r"([?&]token=)[^&\s]+")


class AccessLogTokenFilter(logging.Filter):
    """Redact the session token from /auth/bootstrap?token=... in uvicorn access logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                _TOKEN_QUERY_RE.sub(r"\1[REDACTED]", a) if isinstance(a, str) else a
                for a in record.args
            )
        return True


def _uvicorn_log_config() -> dict[str, Any]:
    log_config = copy.deepcopy(LOGGING_CONFIG)
    log_config.setdefault("filters", {})["redact_token"] = {"()": AccessLogTokenFilter}
    log_config["handlers"]["access"].setdefault("filters", []).append("redact_token")
    return log_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="atlas",
        description="ATLAS — Autonomous Multi-Agent Software Engineering Orchestrator",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"atlas {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command")

    serve_parser = subparsers.add_parser(
        "serve",
        help="Start the local ATLAS server on 127.0.0.1 and open the dashboard",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("ATLAS_PORT", "8420")),
        help="Loopback port to listen on (default: 8420)",
    )
    serve_parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.environ.get("ATLAS_HOME", "~/.atlas")),
        help="ATLAS runtime data directory (default: ~/.atlas)",
    )
    serve_parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not automatically open the dashboard in a browser",
    )
    sandbox_parser = subparsers.add_parser("sandbox", help="Check or self-test the test sandbox")
    sandbox_parser.add_argument(
        "action",
        choices=["command", "status", "selftest"],
        help="command: print the VM start command; status: read-only checks; selftest: run the canary container",
    )
    return parser


def _sandbox(action: str) -> int:
    from atlas.sandbox import sandbox_status, start_command
    from atlas.sandbox.selftest import run_self_test

    if action == "command":
        print(start_command())
        return 0
    checks = sandbox_status().checks if action == "status" else run_self_test()
    for check in checks:
        print(f"[{'ok' if check.ok else 'FAIL'}] {check.label}" + (f": {check.detail}" if check.detail else ""))
    ready = all(c.ok for c in checks)
    print("Sandbox ready." if ready else "Sandbox NOT ready. No tests will run until every check passes.")
    return 0 if ready else 1


def main(argv: list[str] | None = None) -> int:
    # Enforce ADR-006 §1 zero-egress environment variables
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    os.environ.pop("LANGSMITH_API_KEY", None)
    os.environ.pop("LANGCHAIN_API_KEY", None)

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "sandbox":
        return _sandbox(args.action)

    if args.command != "serve":
        parser.print_help()
        return 0

    data_dir = ensure_secure_dir(args.data_dir.expanduser().resolve())
    token = load_or_create_session_token(data_dir)
    port: int = args.port
    bootstrap_url = f"http://127.0.0.1:{port}/auth/bootstrap?token={token}"

    print(f"ATLAS Orchestrator v{__version__}")
    print(f"Data directory : {data_dir} (mode 0700)")
    print(f"Dashboard URL  : http://127.0.0.1:{port}/")
    print(f"Bootstrap login: {bootstrap_url}")

    app = create_app(data_dir=data_dir, acquire_lock=True)

    if not args.no_browser:
        timer = threading.Timer(0.8, lambda: webbrowser.open(bootstrap_url))
        timer.daemon = True
        timer.start()

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=port,
        log_level="info",
        log_config=_uvicorn_log_config(),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
