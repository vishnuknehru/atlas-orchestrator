"""Plan challenge loop: an independent agent attacks the plan, the architect answers, the user approves."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from atlas.agents import adapter_for
from atlas.config.schemas import (
    AgentConfigEntry,
    ProjectRoleMapping,
    check_independence,
    derive_model_family,
    derive_provider,
)
from atlas.engine.workflow import CapabilityUnavailableError, StepHandlerFn, StepHandlerResult
from atlas.fsm.states import TaskState
from atlas.planning.task_plan import (
    AdapterFactory,
    AgentLookup,
    TaskPlan,
    extract_json_object,
    run_agent,
    validate_plan,
)

MAX_CHALLENGE_CYCLES = 3
USER_FINDING_ID = "USER"
BLOCKING = ("critical", "high")


class Finding(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=20)
    severity: Literal["critical", "high", "medium", "low"]
    text: str = Field(min_length=1)


class Challenge(BaseModel):
    model_config = ConfigDict(extra="ignore")

    verdict: Literal["approve", "revise"]
    findings: list[Finding] = Field(default_factory=list, max_length=30)


class Response(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    finding_id: str
    response: str = Field(min_length=1)


def plan_path(projects_dir: Path, project_id: str, task_id: str) -> Path:
    return projects_dir / project_id / "plans" / f"{task_id}.json"


def load_plan_doc(projects_dir: Path, project_id: str, task_id: str) -> dict[str, Any]:
    path = plan_path(projects_dir, project_id, task_id)
    if not path.is_file():
        raise CapabilityUnavailableError("plan_missing")
    doc: dict[str, Any] = json.loads(path.read_text())
    return doc


def save_plan_doc(projects_dir: Path, project_id: str, task_id: str, doc: dict[str, Any]) -> str:
    path = plan_path(projects_dir, project_id, task_id)
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return str(path)


def append_user_feedback(projects_dir: Path, project_id: str, task_id: str, text: str) -> None:
    doc = load_plan_doc(projects_dir, project_id, task_id)
    doc["history"].append({"kind": "user_feedback", "text": text})
    save_plan_doc(projects_dir, project_id, task_id, doc)


def _cycle(history: list[dict[str, Any]]) -> int:
    """1-based challenge cycle since the last user feedback (the cap restarts when the user asks for changes)."""
    n = 0
    for entry in reversed(history):
        if entry["kind"] == "user_feedback":
            break
        if entry["kind"] == "challenge":
            n += 1
    return n + 1


def open_findings(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What the next revision must answer: blocking findings of the latest challenge, or the user's feedback."""
    if history and history[-1]["kind"] == "user_feedback":
        return [{"id": USER_FINDING_ID, "severity": "high", "text": history[-1]["text"]}]
    for entry in reversed(history):
        if entry["kind"] == "challenge":
            return [f for f in entry["findings"] if f["severity"] in BLOCKING]
    return []


def _context(doc: dict[str, Any]) -> str:
    plan = {k: doc[k] for k in ("approach", "steps", "risks")}
    return json.dumps(plan, indent=2)


def _load_task(conn: sqlite3.Connection, project_id: str, task_id: str) -> tuple[Any, ProjectRoleMapping]:
    task = conn.execute(
        "SELECT title, request_text, acceptance_criteria_json FROM tasks WHERE id = ?;", (task_id,)
    ).fetchone()
    proj = conn.execute("SELECT config_json FROM projects WHERE id = ?;", (project_id,)).fetchone()
    if task is None or proj is None:
        raise CapabilityUnavailableError("task_missing")
    roles = ProjectRoleMapping.model_validate(json.loads(proj["config_json"]).get("agents", {}))
    return task, roles


def _agent_identity(agent: AgentConfigEntry) -> tuple[str, str]:
    model = agent.models[0] if agent.models else None
    return derive_provider(agent), derive_model_family(model, agent.model_family)


def make_challenge_handler(
    projects_dir: Path, lookup_agent: AgentLookup, adapter_factory: AdapterFactory = adapter_for
) -> StepHandlerFn:
    """`challenge` step: the challenger reviews the plan -> PLAN_REVISION, or AWAITING_APPROVAL when clean/capped."""

    def handler(conn: sqlite3.Connection, project_id: str, entity_id: str, step_id: str) -> StepHandlerResult:
        task, roles = _load_task(conn, project_id, entity_id)
        doc = load_plan_doc(projects_dir, project_id, entity_id)
        author = lookup_agent(str(doc["author_agent"]))
        challenger = lookup_agent(roles.challenger)
        if challenger is None or author is None:
            raise CapabilityUnavailableError("challenger_not_configured")
        if not check_independence(*_agent_identity(author), *_agent_identity(challenger)):
            raise CapabilityUnavailableError("challenger_not_independent")

        history: list[dict[str, Any]] = doc["history"]
        previous = [e for e in history if e["kind"] in ("challenge", "revision")][-2:]
        prompt = f"""You are an independent reviewer challenging an implementation plan before any code is written.
Do not modify any files. Find real problems: missed requirements, wrong assumptions, risky steps, missing tests.

Task: {task["title"]}
{task["request_text"]}

Acceptance criteria:
{task["acceptance_criteria_json"]}

Plan (revision {doc["revision"]}):
{_context(doc)}

Earlier review rounds and the architect's answers (do not repeat resolved points):
{json.dumps(previous, indent=2)[:6000]}

Respond with ONLY one JSON object, no prose and no code fences:
{{"verdict": "approve" or "revise",
  "findings": [{{"id": "F1", "severity": "critical|high|medium|low", "text": "specific problem and why it matters"}}]}}
Use "revise" only if there is at least one critical or high finding. Use unique ids."""
        output = run_agent(adapter_factory, challenger, roles.challenger, "challenger", prompt)
        raw = extract_json_object(output, "verdict")
        if raw is None:
            raise CapabilityUnavailableError("challenge_invalid")
        try:
            result = Challenge.model_validate(raw)
        except ValidationError as exc:
            raise CapabilityUnavailableError("challenge_invalid") from exc
        if len({f.id for f in result.findings}) != len(result.findings) or any(
            f.id == USER_FINDING_ID for f in result.findings
        ):
            raise CapabilityUnavailableError("challenge_invalid")

        cycle = _cycle(history)
        blocking = [f for f in result.findings if f.severity in BLOCKING]
        history.append(
            {
                "kind": "challenge",
                "cycle": cycle,
                "revision": doc["revision"],
                "challenger": roles.challenger,
                "verdict": "revise" if blocking else "approve",
                "findings": [f.model_dump() for f in result.findings],
            }
        )
        if blocking and cycle >= MAX_CHALLENGE_CYCLES:
            doc["escalated"] = True
            to_state, reason = (
                TaskState.AWAITING_APPROVAL.value,
                f"{MAX_CHALLENGE_CYCLES} challenge cycles used; {len(blocking)} blocking finding(s) left for your decision",
            )
        elif blocking:
            to_state, reason = (
                TaskState.PLAN_REVISION.value,
                f"{roles.challenger} found {len(blocking)} blocking issue(s) in cycle {cycle}",
            )
        else:
            doc["escalated"] = False
            to_state, reason = (
                TaskState.AWAITING_APPROVAL.value,
                f"{roles.challenger} found no blocking issues ({len(result.findings)} minor)",
            )
        return StepHandlerResult(
            to_state=to_state, reason=reason, result_ref=save_plan_doc(projects_dir, project_id, entity_id, doc)
        )

    return handler


def make_revise_handler(
    projects_dir: Path,
    lookup_agent: AgentLookup,
    adapter_factory: AdapterFactory = adapter_for,
) -> StepHandlerFn:
    """`revise` step: the architect answers every open finding and revises the plan -> PLAN_CHALLENGE."""

    def handler(conn: sqlite3.Connection, project_id: str, entity_id: str, step_id: str) -> StepHandlerResult:
        task, roles = _load_task(conn, project_id, entity_id)
        doc = load_plan_doc(projects_dir, project_id, entity_id)
        architect = lookup_agent(roles.architect)
        if architect is None:
            raise CapabilityUnavailableError("architect_not_configured")
        findings = open_findings(doc["history"])
        criteria = json.loads(task["acceptance_criteria_json"] or "[]")
        prompt = f"""You are the architect. Your plan was challenged. Answer EVERY finding below (accept it and change the plan,
or explain concisely why it does not apply), then give the full revised plan. Do not modify any files.

Task: {task["title"]}
{task["request_text"]}

Acceptance criteria:
{json.dumps(criteria)}

Current plan (revision {doc["revision"]}):
{_context(doc)}

Findings to answer:
{json.dumps(findings, indent=2)}

Respond with ONLY one JSON object, no prose and no code fences:
{{"responses": [{{"finding_id": "F1", "response": "what you did or why not"}}],
  "approach": "...", "steps": [{{"title": "...", "description": "...", "files": [], "criteria": ["AC-1"]}}], "risks": ["..."]}}
Every acceptance criterion id must stay covered by at least one step."""
        output = run_agent(adapter_factory, architect, roles.architect, "architect", prompt)
        raw = extract_json_object(output, "responses")
        if raw is None:
            raise CapabilityUnavailableError("plan_invalid")
        try:
            responses = [Response.model_validate(r) for r in raw["responses"]]
        except (ValidationError, TypeError) as exc:
            raise CapabilityUnavailableError("plan_invalid") from exc
        if {f["id"] for f in findings} - {r.finding_id for r in responses}:
            raise CapabilityUnavailableError("findings_unanswered")
        plan: TaskPlan = validate_plan(raw, {str(c["id"]) for c in criteria})

        doc.update(plan.model_dump())
        doc["revision"] += 1
        doc["history"].append(
            {"kind": "revision", "revision": doc["revision"], "responses": [r.model_dump() for r in responses]}
        )
        return StepHandlerResult(
            to_state=TaskState.PLAN_CHALLENGE.value,
            reason=f"{roles.architect} revised the plan (revision {doc['revision']})",
            result_ref=save_plan_doc(projects_dir, project_id, entity_id, doc),
        )

    return handler
