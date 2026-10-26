"""Architect-agent plan generation.

The architect runs read-only in an empty scratch directory (never inside the target repository, so
repository-supplied hooks or settings cannot execute) and returns one JSON document that is validated
before anything is written to the database.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from atlas.agents import AgentRequest, AgentResult, adapter_for
from atlas.config.schemas import AgentConfigEntry

Category = Literal[
    "feature",
    "bug_fix",
    "performance",
    "refactor",
    "security",
    "dependencies",
    "ui",
    "tests",
    "investigation",
    "modernisation",
]

MAX_PHASES = 12
PLAN_TIMEOUT_SECONDS = 300


class PlanGenerationError(RuntimeError):
    """The architect could not produce a usable plan."""


class PlannedPhase(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1)
    category: Category = "feature"
    acceptance_criteria: list[str] = Field(min_length=1)

    @field_validator("acceptance_criteria")
    @classmethod
    def _non_blank(cls, value: list[str]) -> list[str]:
        cleaned = [c.strip() for c in value if c and c.strip()]
        if not cleaned:
            raise ValueError("each phase needs at least one acceptance criterion")
        return cleaned


class ProjectPlan(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    summary: str = Field(min_length=1)
    phases: list[PlannedPhase] = Field(min_length=1, max_length=MAX_PHASES)


@dataclass(frozen=True)
class PlanContext:
    project_name: str
    repository: str
    base_branch: str
    goal: str
    agent_id: str
    agent: AgentConfigEntry


PlanGeneratorFn = Callable[[PlanContext], ProjectPlan]


def build_prompt(ctx: PlanContext) -> str:
    return f"""You are the architect for the software project "{ctx.project_name}" ({ctx.repository}, base branch {ctx.base_branch}).

Break the goal below into a detailed delivery plan of ordered phases. Each phase must be a small,
independently reviewable and mergeable change that builds on the phases before it.

Goal:
{ctx.goal}

Respond with ONLY one JSON object, no prose and no code fences, in exactly this shape:
{{
  "summary": "2-4 sentence overview of the approach",
  "phases": [
    {{
      "title": "short imperative title",
      "objective": "what this phase delivers and why, with enough detail for a developer to start",
      "category": one of {", ".join(get_args(Category))},
      "acceptance_criteria": ["objective, verifiable statement", "..."]
    }}
  ]
}}
Use at most {MAX_PHASES} phases and 2-6 acceptance criteria per phase. Do not modify any files."""


def parse_plan_output(text: str) -> ProjectPlan:
    """Extract and validate the first JSON object in the agent output."""
    decoder = json.JSONDecoder()
    for start, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and "phases" in candidate:
            try:
                return ProjectPlan.model_validate(candidate)
            except ValidationError as exc:
                raise PlanGenerationError(f"The architect's plan was not valid: {exc.errors()[0]['msg']}") from exc
    raise PlanGenerationError("The architect did not return a plan in the expected format.")


def cli_plan_generator(ctx: PlanContext) -> ProjectPlan:
    """Run the project's architect agent once, read-only in an empty scratch directory, and parse its plan."""
    with tempfile.TemporaryDirectory(prefix="atlas-plan-") as tmp:
        result = adapter_for(ctx.agent).run(
            AgentRequest(
                agent_id=ctx.agent_id,
                role="architect",
                prompt=build_prompt(ctx),
                cwd=Path(tmp),
                timeout_seconds=PLAN_TIMEOUT_SECONDS,
            )
        )
    if not result.ok:
        raise PlanGenerationError(_failure_message(ctx, result))
    return parse_plan_output(result.output)


def _failure_message(ctx: PlanContext, result: AgentResult) -> str:
    if result.failure == "unsupported_adapter":
        return (
            f"Agent {ctx.agent_id!r} ({ctx.agent.adapter}) cannot generate plans automatically. "
            "Choose a Claude Code or Codex agent as this project's architect in Settings."
        )
    if result.failure == "timeout":
        return "The architect took too long to produce a plan. Try a narrower goal."
    if result.failure == "rate_limit":
        return f"The architect agent hit a usage limit: {result.detail}. Try again later."
    if result.failure == "auth_expired":
        return f"The architect agent is not signed in: {result.detail}. Sign in to its CLI and retry."
    if result.failure == "not_installed":
        return result.detail
    return f"The architect agent failed (exit {result.exit_code}): {result.detail}"
