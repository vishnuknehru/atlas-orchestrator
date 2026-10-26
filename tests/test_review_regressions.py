"""Regression tests for code-review findings: thread classification, pause fence, API status codes,
blocked_reason precedence, overlay reasons, recovery robustness, and secret/token leakage."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from atlas.api.app import EventBroadcaster, _project_block_after_role_eval
from atlas.cli import AccessLogTokenFilter
from atlas.config.schemas import RoleEvaluationResult
from atlas.config.secret_store import InMemorySecretStore, MacOSKeychainSecretStore
from atlas.db.connection import DatabaseManager
from atlas.engine.workflow import LangGraphWorkflowEngine
from atlas.fsm.states import TaskState
from atlas.fsm.transitions import apply_task_transition
from tests.test_api_and_restart import _make_authed_client
from tests.test_db_integrity import _seed_project_task_plan_phase

_HEADERS_ORIGIN = "http://127.0.0.1:8420"


def _register_and_create_task(tmp_path: Path) -> tuple[Any, dict[str, str], str]:
    store = InMemorySecretStore()
    client, _, csrf_token = _make_authed_client(tmp_path / "atlas-home", store)
    headers = {"X-Atlas-CSRF": csrf_token, "Origin": _HEADERS_ORIGIN}
    r_proj = client.post(
        "/api/v1/projects",
        headers=headers,
        json={"name": "hello-world", "repository": "octocat/hello-world", "base_branch": "main"},
    )
    assert r_proj.status_code == 201, r_proj.text
    r_task = client.post(
        f"/api/v1/projects/{r_proj.json()['id']}/tasks",
        headers=headers,
        json={"title": "t", "request_text": "do it", "category": "feature"},
    )
    assert r_task.status_code == 201, r_task.text
    return client, headers, str(r_task.json()["id"])


def test_started_task_is_not_reported_diverged_on_restart(tmp_path: Path) -> None:
    client, headers, task_id = _register_and_create_task(tmp_path)
    r_start = client.post(f"/api/v1/tasks/{task_id}/start", headers=headers)
    assert r_start.status_code == 200, r_start.text
    before = client.get(f"/api/v1/tasks/{task_id}").json()
    assert before["state"] == TaskState.BLOCKED.value

    engine: LangGraphWorkflowEngine = client.app.state.engine
    result = engine.classify_thread("task", task_id)
    assert result.status != "incompatible", result.reason

    after = client.get(f"/api/v1/tasks/{task_id}").json()
    assert after["blocked_reason"] == before["blocked_reason"]
    assert after["blocked_reason"] != "engine_state_divergence"


def test_rejected_pause_returns_409_and_does_not_leak_fence(tmp_path: Path) -> None:
    client, headers, task_id = _register_and_create_task(tmp_path)
    assert client.post(f"/api/v1/tasks/{task_id}/cancel", headers=headers).status_code == 200

    r_pause = client.post(f"/api/v1/tasks/{task_id}/pause", headers=headers)
    assert r_pause.status_code == 409, r_pause.text

    with client.app.state.db.read_conn() as conn:
        row = conn.execute("SELECT pause_requested_at FROM tasks WHERE id = ?;", (task_id,)).fetchone()
    assert row["pause_requested_at"] is None


def test_missing_entities_return_404(tmp_path: Path) -> None:
    client, headers, _ = _register_and_create_task(tmp_path)
    for action in ("start", "pause", "resume", "cancel", "rebuild"):
        r = client.post(f"/api/v1/tasks/does-not-exist/{action}", headers=headers)
        assert r.status_code == 404, (action, r.text)
    for action in ("pause", "cancel", "rebuild"):
        r = client.post(f"/api/v1/phases/does-not-exist/{action}", headers=headers)
        assert r.status_code == 404, (action, r.text)


def test_resume_of_unpaused_task_returns_409(tmp_path: Path) -> None:
    client, headers, task_id = _register_and_create_task(tmp_path)
    r = client.post(f"/api/v1/tasks/{task_id}/resume", headers=headers)
    assert r.status_code == 409, r.text


def test_role_evaluation_never_overrides_or_clears_credential_block() -> None:
    bad_roles = RoleEvaluationResult(ok=False, blocked_reason="agent_config")
    good_roles = RoleEvaluationResult(ok=True)

    assert _project_block_after_role_eval("blocked", "github_credential", bad_roles) == (
        "blocked",
        "github_credential",
    )
    assert _project_block_after_role_eval("blocked", "github_credential", good_roles) == (
        "blocked",
        "github_credential",
    )
    assert _project_block_after_role_eval("blocked", "agent_config", good_roles) == ("active", None)
    assert _project_block_after_role_eval("active", None, bad_roles) == ("blocked", "agent_config")
    assert _project_block_after_role_eval("active", None, good_roles) == ("active", None)


def test_underlying_transition_keeps_overlay_blocked_reason(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path / "atlas-home")
    db.initialize()
    with db.begin_immediate() as conn:
        _, tid, _, _ = _seed_project_task_plan_phase(conn)
        apply_task_transition(
            conn, task_id=tid, to_state=TaskState.REPOSITORY_ANALYSIS.value, reason="start", actor="user"
        )
        apply_task_transition(
            conn,
            task_id=tid,
            to_state=TaskState.BLOCKED.value,
            reason="unknown",
            actor="recovery",
            blocked_reason="effect_outcome_unknown",
        )
        apply_task_transition(
            conn,
            task_id=tid,
            to_state=TaskState.PLANNING.value,
            reason="step done",
            actor="orchestrator",
            underlying=True,
        )
        row = conn.execute("SELECT state, resume_to, blocked_reason FROM tasks WHERE id = ?;", (tid,)).fetchone()
    assert row["state"] == TaskState.BLOCKED.value
    assert row["resume_to"] == TaskState.PLANNING.value
    assert row["blocked_reason"] == "effect_outcome_unknown"


def test_recovery_survives_succeeded_step_without_to_state(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path / "atlas-home")
    db.initialize()
    with db.begin_immediate() as conn:
        _, tid, _, _ = _seed_project_task_plan_phase(conn)
        apply_task_transition(
            conn, task_id=tid, to_state=TaskState.REPOSITORY_ANALYSIS.value, reason="start", actor="user"
        )

    engine = LangGraphWorkflowEngine(db)
    s0 = engine.execute_step_begin(
        {
            "entity_type": "task",
            "entity_id": tid,
            "project_id": "p",
            "state": TaskState.REPOSITORY_ANALYSIS.value,
            "entity_version": 1,
            "current_step_id": None,
            "generation": 1,
            "last_node": None,
            "retry_signal": None,
            "halted": False,
        },
        "analyse",
    )
    step_id = str(s0["current_step_id"])
    engine.reserve_execution_dispatch(
        entity_type="task",
        entity_id=tid,
        step_id=step_id,
        kind="agent",
        role="architect",
        model="claude-sonnet-4-5",
    )
    with db.begin_immediate() as conn:
        conn.execute(
            "UPDATE executions SET status = 'succeeded', result_json = ? WHERE step_id = ?;",
            (json.dumps({"reason": "no target state recorded"}), step_id),
        )

    actions = engine.resolve_unfinished_steps()
    assert {"step_id": step_id, "action": "blocked_effect_outcome_unknown"} in actions
    with db.read_conn() as conn:
        row = conn.execute("SELECT state, blocked_reason FROM tasks WHERE id = ?;", (tid,)).fetchone()
    assert row["state"] == TaskState.BLOCKED.value
    assert row["blocked_reason"] == "effect_outcome_unknown"


def test_keychain_secret_is_never_passed_on_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append({"argv": argv, **kwargs})
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    secret = 'ghp_secret"with\\quotes'
    MacOSKeychainSecretStore().set_secret("atlas.x.github", "octocat", secret)

    assert len(calls) == 1
    assert not any(secret in a for a in calls[0]["argv"])
    assert calls[0]["input"] == (
        'add-generic-password -U -s "atlas.x.github" -a "octocat" -w "ghp_secret\\"with\\\\quotes"\n'
    )

    with pytest.raises(ValueError):
        MacOSKeychainSecretStore().set_secret("atlas.x.github", "octocat", "line1\nline2")


def test_access_log_redacts_bootstrap_token() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "GET", "/auth/bootstrap?token=s3cr3t-token", "1.1", 303),
        None,
    )
    assert AccessLogTokenFilter().filter(record)
    assert "s3cr3t-token" not in record.getMessage()
    assert "token=[REDACTED]" in record.getMessage()


def test_broadcaster_delivers_events_published_from_worker_thread() -> None:
    async def scenario() -> dict[str, Any]:
        broadcaster = EventBroadcaster()
        queue = broadcaster.subscribe()
        worker = threading.Thread(target=broadcaster.publish, args=({"type": "ping"},))
        worker.start()
        worker.join()
        return await asyncio.wait_for(queue.get(), timeout=2.0)

    assert asyncio.run(scenario()) == {"type": "ping"}
