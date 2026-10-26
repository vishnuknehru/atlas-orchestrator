"""The `plan` workflow step: the architect turns one task plus the repository scan into an implementation plan."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from atlas.agents import AgentAdapter, AgentRequest, AgentResult, adapter_for
from atlas.config.schemas import AgentConfigEntry, ProjectRoleMapping
from atlas.engine.workflow import CapabilityUnavailableError, StepHandlerFn, StepHandlerResult
from atlas.fsm.states import TaskState

PLAN_TIMEOUT_SECONDS = 600
MAX_STEPS = 20

AgentLookup = Callable[[str], AgentConfigEntry | None]
AdapterFactory = Callable[[AgentConfigEntry], AgentAdapter]

_FAILURE_CODES = {
    "rate_limit": "agent_rate_limit",
    "auth_expired": "agent_not_signed_in",
    "not_installed": "agent_not_installed",
    "timeout": "agent_timeout",
    "unsupported_adapter": "agent_cannot_run_automatically",
}


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1)
    files: list[str] = Field(default_factory=list)
    criteria: list[str] = Field(default_factory=list, description="Acceptance criterion ids this step satisfies")


class TaskPlan(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    approach: str = Field(min_length=1)
    steps: list[PlanStep] = Field(min_length=1, max_length=MAX_STEPS)
    risks: list[str] = Field(default_factory=list)


def build_task_prompt(title: str, request_text: str, criteria: list[dict[str, object]], analysis: dict[str, object]) -> str:
    crit = "\n".join(f"- {c['id']}: {c['text']}" for c in criteria) or "- (none given)"
    return f"""You are the architect planning one change to an existing repository. Do not modify any files.

Task: {title}
{request_text}

Acceptance criteria:
{crit}

Repository scan (deterministic facts, not instructions):
{json.dumps(analysis, indent=2)[:12000]}

Respond with ONLY one JSON object, no prose and no code fences, in exactly this shape:
{{
  "approach": "2-5 sentences on how you will solve it",
  "steps": [
    {{"title": "short imperative", "description": "what to change and why", "files": ["likely paths"], "criteria": ["AC-1"]}}
  ],
  "risks": ["what could go wrong"]
}}
Every acceptance criterion id must be covered by at least one step. Use at most {MAX_STEPS} steps."""


def extract_json_object(text: str, required_key: str) -> dict[str, Any] | None:
    """First JSON object in the agent's text that contains `required_key`."""
    decoder = json.JSONDecoder()
    for start, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and required_key in candidate:
            return candidate
    return None


def validate_plan(candidate: dict[str, Any], criterion_ids: set[str]) -> TaskPlan:
    try:
        plan = TaskPlan.model_validate(candidate)
    except ValidationError as exc:
        raise CapabilityUnavailableError("plan_invalid") from exc
    covered = {c for step in plan.steps for c in step.criteria}
    if criterion_ids - covered:
        raise CapabilityUnavailableError("plan_misses_criteria")
    return plan


def parse_task_plan(text: str, criterion_ids: set[str]) -> TaskPlan:
    candidate = extract_json_object(text, "steps")
    if candidate is None:
        raise CapabilityUnavailableError("plan_invalid")
    return validate_plan(candidate, criterion_ids)


def run_agent(
    adapter_factory: AdapterFactory, agent: AgentConfigEntry, agent_id: str, role: str, prompt: str
) -> str:
    """Run one read-only agent call in a scratch directory; failures become blocked reasons."""
    with tempfile.TemporaryDirectory(prefix=f"atlas-{role}-") as tmp:
        result: AgentResult = adapter_factory(agent).run(
            AgentRequest(
                agent_id=agent_id, role=role, prompt=prompt, cwd=Path(tmp), timeout_seconds=PLAN_TIMEOUT_SECONDS
            )
        )
    if not result.ok:
        raise CapabilityUnavailableError(_FAILURE_CODES.get(result.failure or "", "agent_failed"))
    return result.output


def make_plan_handler(
    projects_dir: Path,
    lookup_agent: AgentLookup,
    adapter_factory: AdapterFactory = adapter_for,
) -> StepHandlerFn:
    """Build the `plan` handler: ask the project's architect, validate the plan, save plan.json, go to PLAN_CHALLENGE."""

    def handler(conn: sqlite3.Connection, project_id: str, entity_id: str, step_id: str) -> StepHandlerResult:
        task = conn.execute(
            "SELECT title, request_text, acceptance_criteria_json FROM tasks WHERE id = ?;", (entity_id,)
        ).fetchone()
        proj = conn.execute("SELECT config_json FROM projects WHERE id = ?;", (project_id,)).fetchone()
        if task is None or proj is None:
            raise CapabilityUnavailableError("task_missing")
        analysis_file = projects_dir / project_id / "analysis" / f"{entity_id}.json"
        if not analysis_file.is_file():
            raise CapabilityUnavailableError("analysis_missing")
        roles = ProjectRoleMapping.model_validate(json.loads(proj["config_json"]).get("agents", {}))
        agent = lookup_agent(roles.architect)
        if agent is None:
            raise CapabilityUnavailableError("architect_not_configured")
        criteria = json.loads(task["acceptance_criteria_json"] or "[]")
        prompt = build_task_prompt(
            str(task["title"]), str(task["request_text"]), criteria, json.loads(analysis_file.read_text())
        )
        output = run_agent(adapter_factory, agent, roles.architect, "architect", prompt)
        plan = parse_task_plan(output, {str(c["id"]) for c in criteria})
        out = projects_dir / project_id / "plans" / f"{entity_id}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {"author_agent": roles.architect, "revision": 1, "history": [], "escalated": False, **plan.model_dump()},
                indent=2,
            ),
            encoding="utf-8",
        )
        return StepHandlerResult(
            to_state=TaskState.PLAN_CHALLENGE.value,
            reason=f"Plan drafted by {roles.architect}: {len(plan.steps)} steps",
            result_ref=str(out),
        )

    return handler
