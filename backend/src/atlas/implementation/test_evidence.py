"""Run a task's candidate through the test sandbox and keep the result as evidence bound to the candidate commit."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from atlas.implementation.step import load_candidate
from atlas.sandbox.checks import Runner, default_runner
from atlas.sandbox.config import SandboxPaths
from atlas.sandbox.runner import TestRun, TestSpec, reap_orphans, run_candidate_tests
from atlas.utils import utc_now_iso

# One execution at a time (the default is one per repository; the VM is small and shared).
_RUN_LOCK = threading.Lock()
_reaped = False

RunFn = Callable[[Path, str, TestSpec], TestRun]


def test_evidence_path(projects_dir: Path, project_id: str, task_id: str) -> Path:
    return projects_dir / project_id / "tests" / f"{task_id}.json"


def load_test_evidence(projects_dir: Path, project_id: str, task_id: str) -> dict[str, Any] | None:
    path = test_evidence_path(projects_dir, project_id, task_id)
    if not path.is_file():
        return None
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def _write(projects_dir: Path, project_id: str, task_id: str, data: dict[str, Any]) -> None:
    path = test_evidence_path(projects_dir, project_id, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def run_task_tests(
    projects_dir: Path,
    project_id: str,
    task_id: str,
    clone_path: Path,
    tests_config: dict[str, Any],
    *,
    run_fn: RunFn | None = None,
    paths: SandboxPaths | None = None,
    runner: Runner = default_runner,
) -> dict[str, Any] | None:
    """Test the task's current candidate. Evidence is written first as `running`, then replaced with the result.
    Returns None when there is no candidate."""
    candidate = load_candidate(projects_dir, project_id, task_id)
    if candidate is None:
        return None
    sha = str(candidate["commit_sha"])
    started = utc_now_iso()

    def blocked(reason: str, detail: str) -> dict[str, Any]:
        data: dict[str, Any] = {"status": "blocked", "commit_sha": sha, "blocked_reason": reason, "detail": detail,
                "started_at": started, "finished_at": utc_now_iso(), "commands": [], "image": tests_config.get("image") or ""}
        _write(projects_dir, project_id, task_id, data)
        return data

    commands = tests_config.get("commands") or []
    if not tests_config.get("image") or not commands:
        return blocked("no_test_config", "Set the test image (pinned by digest) and the test commands for this project.")
    try:
        spec = TestSpec(
            image=tests_config["image"], commands=commands, writable_paths=tests_config.get("writable_paths") or [],
            timeout_seconds=int(tests_config.get("timeout_seconds") or 600),
        )
    except ValidationError as exc:
        return blocked("invalid_test_config", exc.errors()[0]["msg"])

    with _RUN_LOCK:
        _write(projects_dir, project_id, task_id, {"status": "running", "commit_sha": sha, "started_at": started,
                                                    "image": spec.image, "commands": []})
        global _reaped
        try:
            if run_fn is None and not _reaped:
                reap_orphans(paths, runner)
                _reaped = True
            run = run_fn(clone_path, sha, spec) if run_fn else run_candidate_tests(clone_path, sha, spec, paths, runner)
        except Exception as exc:  # noqa: BLE001 - never leave the evidence stuck on "running"
            return blocked("test_run_crashed", f"{type(exc).__name__}: {exc}"[:300])
        data = run.as_dict()
        _write(projects_dir, project_id, task_id, data)
        return data
