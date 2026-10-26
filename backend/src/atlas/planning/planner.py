"""Architect-agent plan generation.

The architect runs read-only in an empty scratch directory (never inside the target repository, so
repository-supplied hooks or settings cannot execute) and returns one JSON document that is validated
before anything is written to the database.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

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


def _command(ctx: PlanContext, scratch: Path, out_file: Path) -> list[str]:
    model = ctx.agent.models[0] if ctx.agent.models else None
    if ctx.agent.adapter == "claude_cli":
        cmd = ["claude", "-p", "--output-format", "text", "--no-session-persistence"]
        if model:
            cmd += ["--model", model]
        return cmd
    if ctx.agent.adapter == "codex_cli":
        # Model is deliberately not passed: the CLI default is the one that works on the user's login.
        return [
            "codex", "exec", "-s", "read-only", "--skip-git-repo-check", "--ephemeral",
            "--ignore-rules", "-C", str(scratch), "-o", str(out_file), "-",
        ]
    raise PlanGenerationError(
        f"Agent {ctx.agent_id!r} ({ctx.agent.adapter}) cannot generate plans automatically. "
        "Choose a Claude Code or Codex agent as this project's architect in Settings."
    )


def cli_plan_generator(ctx: PlanContext) -> ProjectPlan:
    """Run the project's architect agent CLI once and parse its plan."""
    with tempfile.TemporaryDirectory(prefix="atlas-plan-") as tmp:
        scratch = Path(tmp)
        out_file = scratch / "plan.txt"
        cmd = _command(ctx, scratch, out_file)
        try:
            proc = subprocess.run(
                cmd,
                input=build_prompt(ctx),
                capture_output=True,
                text=True,
                cwd=scratch,
                timeout=PLAN_TIMEOUT_SECONDS,
                check=False,
            )
        except FileNotFoundError as exc:
            raise PlanGenerationError(f"The {cmd[0]!r} command is not installed or not on PATH.") from exc
        except subprocess.TimeoutExpired as exc:
            raise PlanGenerationError("The architect took too long to produce a plan. Try a narrower goal.") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise PlanGenerationError(
                f"The architect agent failed (exit {proc.returncode}): {detail[-1][:300] if detail else 'no output'}"
            )
        output = out_file.read_text() if out_file.exists() else proc.stdout
        return parse_plan_output(output)
