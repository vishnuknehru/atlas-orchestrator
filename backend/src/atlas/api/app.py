"""FastAPI REST & SSE server for ATLAS Orchestrator (Slice 1.5 & 1.7)."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import subprocess
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from atlas import __version__
from atlas.agents import adapter_for
from atlas.analysis import make_analyse_handler
from atlas.api.security import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    LocalhostSecurityMiddleware,
    derive_csrf_token,
    load_or_create_session_token,
)
from atlas.config.model_catalog import suggested_models
from atlas.config.schemas import (
    ADAPTER_MANIFESTS,
    AgentConfigEntry,
    AgentRegistryConfig,
    ProjectConfig,
    ProjectMetadataSpec,
    ProjectRoleMapping,
    RoleEvaluationResult,
    derive_model_family,
    derive_provider,
    evaluate_project_roles,
    load_agent_registry,
    load_global_config,
    save_agent_registry,
)
from atlas.config.secret_store import (
    Redactor,
    SecretStore,
    SecretStoreUnavailableError,
    get_default_secret_store,
)
from atlas.db.connection import DatabaseManager
from atlas.engine.workflow import (
    CapabilityUnavailableError,
    LangGraphWorkflowEngine,
    RebuildPreconditionError,
)
from atlas.fsm.states import PhaseState, TaskState
from atlas.fsm.transitions import (
    EntityNotFoundError,
    GuardFailedError,
    InvalidTransitionError,
    apply_phase_transition,
    apply_task_transition,
    bump_project_policy_version,
)
from atlas.implementation import load_candidate, make_implement_handler
from atlas.planning import PlanContext, PlanGenerationError, PlanGeneratorFn, cli_plan_generator
from atlas.planning.challenge import (
    append_user_feedback,
    load_plan_doc,
    make_challenge_handler,
    make_revise_handler,
)
from atlas.planning.task_plan import AdapterFactory, make_plan_handler
from atlas.recovery.startup import (
    InstanceLock,
    RecoverySummary,
    run_startup_recovery,
    sync_registry_to_db,
)
from atlas.sandbox import sandbox_status
from atlas.sandbox.selftest import run_self_test
from atlas.utils import ensure_secure_dir, new_ulid, utc_now_iso

_REPO_SLUG_RE = re.compile(r"^[a-z0-9_.-]+/[a-z0-9_.-]+$")

# blocked_reason values owned by role evaluation; any other reason (credentials, secret store)
# takes precedence and is only cleared by the check that set it.
_ROLE_BLOCKED_REASONS = ("agent_config", "api_key_not_isolated")


def _project_block_after_role_eval(
    status: str, blocked_reason: str | None, role_eval: RoleEvaluationResult
) -> tuple[str, str | None]:
    """Return (status, blocked_reason) after combining a project's current block with a role evaluation."""
    if status == "archived":
        return status, blocked_reason
    if blocked_reason is not None and blocked_reason not in _ROLE_BLOCKED_REASONS:
        return "blocked", blocked_reason
    if not role_eval.ok:
        return "blocked", role_eval.blocked_reason
    if blocked_reason is not None:
        return "active", None
    return status, None


def _role_evaluation_dict(role_eval: RoleEvaluationResult) -> dict[str, Any]:
    return {
        "ok": role_eval.ok,
        "blocked_reason": role_eval.blocked_reason,
        "detail": role_eval.detail,
        "warnings": list(role_eval.remote_endpoint_warnings),
    }


def _github_secret_services(project_id: str, repo: str) -> tuple[str, str]:
    """Secret store service names checked for a project's PAT, in lookup order."""
    return f"atlas.{project_id}.github", f"atlas.{repo}.github"


def _agent_secret_service(agent_id: str) -> str:
    return f"atlas.agent.{agent_id}"


def _clean_credential(value: str) -> str:
    cleaned = value.strip()
    if not cleaned or any(c.isspace() for c in cleaned):
        raise HTTPException(status_code=422, detail="Credential must be a single token with no whitespace")
    return cleaned


def _delete_all_secrets(active_store: SecretStore, service: str) -> None:
    """Remove every item stored under service (the Keychain may hold one per account)."""
    for _ in range(10):
        if active_store.get_secret(service) is None:
            return
        active_store.delete_secret(service)


@dataclass(frozen=True)
class GitHubRepoCheckResult:
    ok: bool
    visibility: Literal["private", "public", "internal"] = "private"
    default_branch: str = "main"
    error_reason: str | None = None
    error_detail: str | None = None


GitHubVerifierFn = Callable[[str, str], GitHubRepoCheckResult]


def _gh_failure(proc: subprocess.CompletedProcess[str], repo: str, pat: str) -> str:
    """Turn a failed `gh api` call into a message that says what to fix (never includes the token)."""
    text = (proc.stderr or proc.stdout or "").replace(pat, "***").lower()
    if "401" in text or "bad credentials" in text:
        return "GitHub rejected the token (401): it is wrong, expired or revoked. Create a new token and paste it again."
    if "404" in text or "not found" in text:
        return (
            f"GitHub returned 404 for {repo}: the repository name is wrong, or the token has no access to it. "
            "Check the owner/name and that the token's repository access includes it."
        )
    if "403" in text:
        return (
            "GitHub refused access (403): the token lacks permission (needs Metadata: read) "
            "or the organization requires SSO/approval for it."
        )
    detail = " ".join((proc.stderr or proc.stdout or "").replace(pat, "***").split())[:200]
    return f"gh api failed: {detail or 'no output'}"


def default_gh_repo_verifier(repo: str, pat: str) -> GitHubRepoCheckResult:
    """Verify repository access, visibility, and default branch via `gh api` using only the project PAT."""
    scrubbed_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "GH_TOKEN": pat,
        "GH_PROMPT_DISABLED": "1",
    }
    # REST, not `gh repo view` (GraphQL): clearer HTTP errors and works with fine-grained tokens.
    argv = ["gh", "api", f"repos/{repo}"]
    try:
        proc = subprocess.run(
            argv,
            env=scrubbed_env,
            capture_output=True,
            text=True,
            timeout=15.0,
            check=False,
        )
    except FileNotFoundError:
        return GitHubRepoCheckResult(
            ok=False,
            error_reason="github_credential",
            error_detail="gh CLI not found on PATH. Install it from https://cli.github.com.",
        )
    except subprocess.TimeoutExpired:
        return GitHubRepoCheckResult(
            ok=False,
            error_reason="github_credential",
            error_detail="GitHub did not answer within 15 seconds. Check your network and try again.",
        )

    if proc.returncode != 0:
        return GitHubRepoCheckResult(
            ok=False,
            error_reason="github_credential",
            error_detail=_gh_failure(proc, repo, pat),
        )

    try:
        data = json.loads(proc.stdout)
        vis_raw = str(data.get("visibility") or ("private" if data.get("private") else "public")).lower()
        vis: Literal["private", "public", "internal"] = (
            "public"
            if vis_raw == "public"
            else ("internal" if vis_raw == "internal" else "private")
        )
        branch = str(data.get("default_branch") or "main")
        return GitHubRepoCheckResult(ok=True, visibility=vis, default_branch=branch)
    except (json.JSONDecodeError, TypeError, AttributeError):
        return GitHubRepoCheckResult(
            ok=False,
            error_reason="github_credential",
            error_detail="Invalid JSON response from gh api",
        )


# ---------------------------------------------------------------------------
# API Request Schemas
# ---------------------------------------------------------------------------


class RegisterProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=80)
    repository: str = Field(min_length=3, max_length=140)
    base_branch: str = Field(default="main", min_length=1, max_length=80)
    visibility: Literal["private", "public", "internal"] = "private"
    agents: ProjectRoleMapping = Field(default_factory=ProjectRoleMapping)


class UpdateProjectConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=80)
    base_branch: str | None = Field(default=None, min_length=1, max_length=80)
    agents: ProjectRoleMapping | None = None
    required_checks: list[str] | None = None
    writable_paths: list[str] | None = None


class AcceptanceCriterionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str | None = None
    text: str = Field(min_length=1)
    mandatory: bool = True


class StoreCredentialRequest(BaseModel):
    """A credential sent once from the dashboard; it is written to the OS secret store and never returned."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    secret: str = Field(min_length=1, max_length=512)


ROADMAP_GOAL_MAX_CHARS = 32000


class CreateRoadmapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    goal: str = Field(min_length=10, max_length=ROADMAP_GOAL_MAX_CHARS)


class RequestPlanChangesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    comment: str = Field(min_length=3, max_length=4000)


class CreateTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=200)
    request_text: str = Field(min_length=1)
    category: Literal[
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
    ] = "feature"
    priority: int = Field(default=3, ge=1, le=5)
    acceptance_criteria: list[AcceptanceCriterionInput] = Field(default_factory=list)


class EventBroadcaster:
    """In-memory pub/sub for SSE clients receiving persisted transition events.

    Handlers run in FastAPI's threadpool, so publish() hands events to the event loop
    that owns the subscriber queues rather than touching them from a worker thread.
    """

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        self._loop = asyncio.get_running_loop()
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(q)

    def publish(self, event: dict[str, Any]) -> None:
        loop = self._loop
        if loop is None:
            return
        try:
            on_loop = asyncio.get_running_loop() is loop
        except RuntimeError:
            on_loop = False
        if on_loop:
            self._deliver(event)
            return
        try:
            loop.call_soon_threadsafe(self._deliver, event)
        except RuntimeError:
            pass  # loop already closed; no subscribers left to notify

    def _deliver(self, event: dict[str, Any]) -> None:
        for q in list(self._subscribers):
            if not q.full():
                q.put_nowait(event)


def create_app(
    data_dir: Path | None = None,
    *,
    secret_store: SecretStore | None = None,
    gh_verifier: GitHubVerifierFn | None = None,
    plan_generator: PlanGeneratorFn | None = None,
    agent_adapter_factory: AdapterFactory | None = None,
    allow_testserver: bool = False,
    acquire_lock: bool = True,
) -> FastAPI:
    """Create and configure the ATLAS FastAPI application."""
    resolved_dir = ensure_secure_dir(
        (data_dir or Path(os.environ.get("ATLAS_HOME", "~/.atlas"))).expanduser().resolve()
    )
    db = DatabaseManager(resolved_dir)
    db.initialize()
    load_global_config(resolved_dir)
    load_agent_registry(resolved_dir)

    class _LazyStore:
        """Resolves the OS secret store on first use so app start-up never touches the keychain."""

        def _active(self) -> SecretStore:
            return store if store is not None else get_default_secret_store()

        def get_secret(self, service: str, account: str | None = None) -> str | None:
            return self._active().get_secret(service, account)

        def set_secret(self, service: str, account: str, secret: str) -> None:
            self._active().set_secret(service, account, secret)

        def delete_secret(self, service: str, account: str | None = None) -> None:
            self._active().delete_secret(service, account)

    def _lookup_agent(agent_id: str) -> AgentConfigEntry | None:
        return load_agent_registry(resolved_dir).agents.get(agent_id)

    engine = LangGraphWorkflowEngine(
        db,
        step_handlers={
            "analyse": make_analyse_handler(_LazyStore(), db.projects_dir),
            "plan": make_plan_handler(db.projects_dir, _lookup_agent, agent_adapter_factory or adapter_for),
            "challenge": make_challenge_handler(
                db.projects_dir, _lookup_agent, agent_adapter_factory or adapter_for
            ),
            "revise": make_revise_handler(db.projects_dir, _lookup_agent, agent_adapter_factory or adapter_for),
            "implement": make_implement_handler(
                db.projects_dir, _lookup_agent, agent_adapter_factory or adapter_for
            ),
        },
    )
    instance_lock = InstanceLock(resolved_dir)
    redactor = Redactor()
    store = secret_store
    verifier = gh_verifier or default_gh_repo_verifier
    planner = plan_generator or cli_plan_generator
    broadcaster = EventBroadcaster()

    session_token = load_or_create_session_token(resolved_dir)
    redactor.register(session_token)
    csrf_token = derive_csrf_token(session_token)

    state_holder: dict[str, Any] = {
        "recovery_summary": RecoverySummary(),
    }

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if acquire_lock:
            instance_lock.acquire()
        try:
            summary = run_startup_recovery(db, engine)
            state_holder["recovery_summary"] = summary
            yield
        finally:
            if acquire_lock:
                instance_lock.release()

    app = FastAPI(
        title="ATLAS Orchestrator",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    app.state.db = db
    app.state.engine = engine
    app.state.instance_lock = instance_lock
    app.state.session_token = session_token
    app.state.csrf_token = csrf_token
    app.state.redactor = redactor

    app.add_middleware(
        LocalhostSecurityMiddleware,
        session_token=session_token,
        allow_testserver=allow_testserver,
    )

    # ------------------------------------------------------------------
    # Public / Bootstrap Endpoints
    # ------------------------------------------------------------------

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        db_ok = db.run_quick_check()
        return {
            "status": "ok" if db_ok else "degraded",
            "db": "ok" if db_ok else "failed",
            "version": __version__,
            "instance_locked": instance_lock.is_locked if acquire_lock else True,
        }

    @app.get("/auth/bootstrap")
    def auth_bootstrap(token: str = Query(...)) -> RedirectResponse:
        if not hmac.compare_digest(token, session_token):
            raise HTTPException(status_code=403, detail="Invalid bootstrap session token")
        resp = RedirectResponse(url="/", status_code=303)
        resp.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=session_token,
            httponly=True,
            samesite="strict",
            path="/",
        )
        resp.set_cookie(
            key=CSRF_COOKIE_NAME,
            value=csrf_token,
            httponly=False,
            samesite="strict",
            path="/",
        )
        return resp

    @app.get("/api/v1/session")
    def get_session() -> dict[str, Any]:
        return {
            "authenticated": True,
            "csrf_token": csrf_token,
            "version": __version__,
        }

    # ------------------------------------------------------------------
    # Overview & Agents Endpoints
    # ------------------------------------------------------------------

    @app.get("/api/v1/overview")
    def get_overview() -> dict[str, Any]:
        registry = load_agent_registry(resolved_dir)
        global_cfg = load_global_config(resolved_dir)
        with db.read_conn() as conn:
            projects = [dict(r) for r in conn.execute("SELECT * FROM projects WHERE status != 'archived' ORDER BY created_at DESC;").fetchall()]
            tasks = [dict(r) for r in conn.execute("SELECT tasks.* FROM tasks JOIN projects ON tasks.project_id = projects.id WHERE projects.status != 'archived' ORDER BY tasks.created_at DESC;").fetchall()]
            agents_rows = {
                str(r["id"]): dict(r)
                for r in conn.execute("SELECT * FROM agents ORDER BY id ASC;").fetchall()
            }

        agents_list: list[dict[str, Any]] = []
        for agent_id, entry in registry.agents.items():
            db_agent = agents_rows.get(agent_id, {})
            primary_model = entry.models[0] if entry.models else None
            agents_list.append(
                {
                    "id": agent_id,
                    "adapter": entry.adapter,
                    "billing": entry.billing,
                    "models": entry.models,
                    "provider": derive_provider(entry),
                    "model_family": (
                        "declared_at_import"
                        if entry.adapter == "manual_handoff" and entry.declared_family_required
                        else derive_model_family(primary_model, entry.model_family)
                    ),
                    "endpoint": entry.endpoint,
                    "allow_remote_endpoint": entry.allow_remote_endpoint,
                    "capacity_state": db_agent.get("capacity_state", "AVAILABLE"),
                    "auth_status": db_agent.get("auth_status", "configured"),
                    "current_revision": db_agent.get("current_revision", 1),
                }
            )

        rec: RecoverySummary = state_holder["recovery_summary"]
        return {
            "version": __version__,
            "recovery": rec.to_dict(),
            "global_config": global_cfg.model_dump(mode="json"),
            "projects": projects,
            "tasks": tasks,
            "agents": agents_list,
        }

    @app.get("/api/v1/agents")
    def list_agents() -> dict[str, Any]:
        registry = load_agent_registry(resolved_dir)
        return registry.model_dump(mode="json")

    @app.put("/api/v1/agents")
    def update_agents(payload: AgentRegistryConfig) -> dict[str, Any]:
        save_agent_registry(resolved_dir, payload)
        sync_registry_to_db(db, payload)
        now = utc_now_iso()

        # Re-evaluate all projects' role configurations against updated registry
        with db.begin_immediate() as conn:
            projects = conn.execute(
                "SELECT id, visibility, config_json, status, blocked_reason FROM projects;"
            ).fetchall()
            for proj in projects:
                cfg_dict = json.loads(proj["config_json"])
                roles = ProjectRoleMapping.model_validate(cfg_dict.get("agents", {}))
                eval_res = evaluate_project_roles(
                    roles, payload, repo_visibility=str(proj["visibility"])
                )
                new_status, new_reason = _project_block_after_role_eval(
                    str(proj["status"]), proj["blocked_reason"], eval_res
                )
                if (new_status, new_reason) != (proj["status"], proj["blocked_reason"]):
                    conn.execute(
                        "UPDATE projects SET status = ?, blocked_reason = ?, updated_at = ? WHERE id = ?;",
                        (new_status, new_reason, now, proj["id"]),
                    )

            conn.execute(
                """
                INSERT INTO audit_log (id, project_id, actor, action, detail_json, correlation_id, at)
                VALUES (?, NULL, 'user', 'agents_registry_updated', ?, ?, ?);
                """,
                (
                    new_ulid(),
                    json.dumps({"agent_ids": sorted(payload.agents.keys())}),
                    new_ulid(),
                    now,
                ),
            )
        broadcaster.publish({"type": "agents_updated", "at": now})
        return payload.model_dump(mode="json")

    # ------------------------------------------------------------------
    # Settings & Credentials Endpoints (write-only secrets; values are never returned)
    # ------------------------------------------------------------------

    def _resolve_store() -> SecretStore | None:
        if store is not None:
            return store
        try:
            return get_default_secret_store()
        except SecretStoreUnavailableError:
            return None

    def _require_store() -> SecretStore:
        active_store = _resolve_store()
        if active_store is None:
            raise HTTPException(status_code=503, detail="No OS secret store is available (no_secret_store)")
        return active_store

    def _has_secret(active_store: SecretStore | None, *services: str) -> bool | None:
        """True/False if any service holds a secret; None when the store can't be read."""
        if active_store is None:
            return None
        try:
            return any(active_store.get_secret(svc) for svc in services)
        except SecretStoreUnavailableError:
            return None

    def _audit(conn: Any, project_id: str | None, action: str, detail: dict[str, Any], now: str) -> None:
        conn.execute(
            """
            INSERT INTO audit_log (id, project_id, actor, action, detail_json, correlation_id, at)
            VALUES (?, ?, 'user', ?, ?, ?, ?);
            """,
            (new_ulid(), project_id, action, json.dumps(detail), new_ulid(), now),
        )

    @app.get("/api/v1/sandbox")
    def get_sandbox_status() -> dict[str, Any]:
        """Read-only checks of the test sandbox. Nothing is started or changed."""
        return sandbox_status().as_dict()

    @app.post("/api/v1/sandbox/selftest")
    def run_sandbox_selftest() -> dict[str, Any]:
        """Create the canary container under the full contract and report what it saw."""
        checks = run_self_test()
        return {"ready": all(c.ok for c in checks if c.level == "required"), "checks": [asdict(c) for c in checks]}

    @app.get("/api/v1/settings")
    def get_settings() -> dict[str, Any]:
        registry = load_agent_registry(resolved_dir)
        global_cfg = load_global_config(resolved_dir)
        active_store = _resolve_store()
        with db.read_conn() as conn:
            rows = conn.execute(
                "SELECT id, name, github_repo, visibility, status, blocked_reason, config_json "
                "FROM projects WHERE status != 'archived' ORDER BY created_at DESC;"
            ).fetchall()

        projects: list[dict[str, Any]] = []
        for row in rows:
            roles = ProjectRoleMapping.model_validate(json.loads(row["config_json"]).get("agents", {}))
            role_eval = evaluate_project_roles(roles, registry, repo_visibility=str(row["visibility"]))
            projects.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "github_repo": row["github_repo"],
                    "status": row["status"],
                    "blocked_reason": row["blocked_reason"],
                    "roles": roles.model_dump(mode="json"),
                    "role_evaluation": _role_evaluation_dict(role_eval),
                    "github_credential_configured": _has_secret(
                        active_store, *_github_secret_services(str(row["id"]), str(row["github_repo"]))
                    ),
                }
            )

        return {
            "secret_store_available": active_store is not None,
            "roles": list(ProjectRoleMapping.model_fields),
            "adapters": [
                {
                    "name": m.name,
                    "kind": m.kind,
                    "default_provider": m.default_provider,
                    "allowed_roles": sorted(m.allowed_roles),
                    "suggested_models": suggested_models(m.name),
                }
                for m in ADAPTER_MANIFESTS.values()
            ],
            "registry": registry.model_dump(mode="json"),
            "agent_api_keys": {
                agent_id: _has_secret(active_store, _agent_secret_service(agent_id))
                for agent_id, entry in registry.agents.items()
                if entry.billing == "api_key"
            },
            "projects": projects,
            "global_config": global_cfg.model_dump(mode="json"),
        }

    @app.put("/api/v1/projects/{project_id}/github-credential")
    def put_github_credential(project_id: str, req: StoreCredentialRequest) -> dict[str, Any]:
        pat = _clean_credential(req.secret)
        with db.read_conn() as conn:
            proj = conn.execute("SELECT * FROM projects WHERE id = ?;", (project_id,)).fetchone()
        if not proj:
            raise HTTPException(status_code=404, detail="Project not found")
        if proj["status"] == "archived":
            raise HTTPException(status_code=409, detail="Restore this project before changing credentials")
        repo = str(proj["github_repo"])
        active_store = _require_store()

        project_service, repo_service = _github_secret_services(project_id, repo)
        redactor.register(pat)
        try:
            # Clear older items (any account, either name) so lookups can only find the new PAT
            _delete_all_secrets(active_store, project_service)
            _delete_all_secrets(active_store, repo_service)
            active_store.set_secret(project_service, "atlas", pat)
        except SecretStoreUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        gh_res = verifier(repo, pat)
        visibility = gh_res.visibility if gh_res.ok else str(proj["visibility"])
        credential_reason = None if gh_res.ok else (gh_res.error_reason or "github_credential")
        roles = ProjectRoleMapping.model_validate(json.loads(proj["config_json"]).get("agents", {}))
        role_eval = evaluate_project_roles(roles, load_agent_registry(resolved_dir), repo_visibility=visibility)
        status_val, blocked_reason = _project_block_after_role_eval("active", credential_reason, role_eval)

        now = utc_now_iso()
        with db.begin_immediate() as conn:
            conn.execute(
                "UPDATE projects SET visibility = ?, status = ?, blocked_reason = ?, updated_at = ? WHERE id = ?;",
                (visibility, status_val, blocked_reason, now, project_id),
            )
            _audit(
                conn,
                project_id,
                "github_credential_updated",
                {"verified": gh_res.ok, "blocked_reason": blocked_reason},
                now,
            )
            row = conn.execute("SELECT * FROM projects WHERE id = ?;", (project_id,)).fetchone()
        broadcaster.publish({"type": "project_updated", "project_id": project_id, "at": now})
        return {
            "project": dict(row),
            "verified": gh_res.ok,
            "error_reason": credential_reason,
            "error_detail": None if gh_res.ok else gh_res.error_detail,
            "role_evaluation": _role_evaluation_dict(role_eval),
        }

    @app.delete("/api/v1/projects/{project_id}/github-credential")
    def delete_github_credential(project_id: str) -> dict[str, Any]:
        with db.read_conn() as conn:
            proj = conn.execute("SELECT github_repo, status FROM projects WHERE id = ?;", (project_id,)).fetchone()
        if not proj:
            raise HTTPException(status_code=404, detail="Project not found")
        if proj["status"] == "archived":
            raise HTTPException(status_code=409, detail="Restore this project before changing credentials")
        active_store = _require_store()
        try:
            for service in _github_secret_services(project_id, str(proj["github_repo"])):
                _delete_all_secrets(active_store, service)
        except SecretStoreUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        now = utc_now_iso()
        with db.begin_immediate() as conn:
            conn.execute(
                "UPDATE projects SET status = 'blocked', blocked_reason = 'github_credential', updated_at = ? "
                "WHERE id = ?;",
                (now, project_id),
            )
            _audit(conn, project_id, "github_credential_removed", {}, now)
            row = conn.execute("SELECT * FROM projects WHERE id = ?;", (project_id,)).fetchone()
        broadcaster.publish({"type": "project_updated", "project_id": project_id, "at": now})
        return dict(row)

    def _api_key_agent(agent_id: str) -> None:
        entry = load_agent_registry(resolved_dir).agents.get(agent_id)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")
        if entry.billing != "api_key":
            raise HTTPException(
                status_code=409,
                detail=f"Agent {agent_id!r} uses billing={entry.billing!r}; only api_key agents take a key",
            )

    @app.put("/api/v1/agents/{agent_id}/api-key")
    def put_agent_api_key(agent_id: str, req: StoreCredentialRequest) -> dict[str, Any]:
        api_key = _clean_credential(req.secret)
        _api_key_agent(agent_id)
        active_store = _require_store()
        redactor.register(api_key)
        service = _agent_secret_service(agent_id)
        try:
            _delete_all_secrets(active_store, service)
            active_store.set_secret(service, "atlas", api_key)
        except SecretStoreUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        now = utc_now_iso()
        with db.begin_immediate() as conn:
            _audit(conn, None, "agent_api_key_updated", {"agent_id": agent_id}, now)
        return {"agent_id": agent_id, "configured": True}

    @app.delete("/api/v1/agents/{agent_id}/api-key")
    def delete_agent_api_key(agent_id: str) -> dict[str, Any]:
        _api_key_agent(agent_id)
        active_store = _require_store()
        try:
            _delete_all_secrets(active_store, _agent_secret_service(agent_id))
        except SecretStoreUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        now = utc_now_iso()
        with db.begin_immediate() as conn:
            _audit(conn, None, "agent_api_key_removed", {"agent_id": agent_id}, now)
        return {"agent_id": agent_id, "configured": False}

    # ------------------------------------------------------------------
    # Projects Endpoints
    # ------------------------------------------------------------------

    @app.get("/api/v1/projects")
    def list_projects(archived: bool = False) -> list[dict[str, Any]]:
        with db.read_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM projects WHERE (status = 'archived') = ? ORDER BY created_at DESC;",
                (int(archived),),
            ).fetchall()
            return [dict(r) for r in rows]

    @app.post("/api/v1/projects", status_code=201)
    def register_project(req: RegisterProjectRequest) -> dict[str, Any]:
        repo_norm = req.repository.strip().lower()
        if not _REPO_SLUG_RE.match(repo_norm):
            raise HTTPException(
                status_code=422,
                detail="Repository must be in canonical 'owner/name' format",
            )

        project_id = new_ulid()
        registry = load_agent_registry(resolved_dir)

        # Check PAT in OS SecretStore under atlas.<project_id>.github (or atlas.repo.<owner/name>.github)
        active_store = store
        if active_store is None:
            try:
                active_store = get_default_secret_store()
            except SecretStoreUnavailableError:
                active_store = None

        pat: str | None = None
        secret_store_error = False
        if active_store is not None:
            try:
                pat = active_store.get_secret(f"atlas.{project_id}.github") or active_store.get_secret(
                    f"atlas.{repo_norm}.github"
                )
            except SecretStoreUnavailableError:
                secret_store_error = True

        visibility = req.visibility
        base_branch = req.base_branch
        blocked_reason: str | None = None

        if secret_store_error:
            blocked_reason = "no_secret_store"
        elif not pat:
            blocked_reason = "github_credential"
        else:
            redactor.register(pat)
            gh_res = verifier(repo_norm, pat)
            if not gh_res.ok:
                blocked_reason = gh_res.error_reason or "github_credential"
            else:
                visibility = gh_res.visibility
                base_branch = gh_res.default_branch or req.base_branch

        # Evaluate ADR-005 / ADR-007 role configuration
        role_eval = evaluate_project_roles(
            req.agents, registry, repo_visibility=visibility
        )
        status_val, blocked_reason = _project_block_after_role_eval(
            "active", blocked_reason, role_eval
        )

        proj_cfg = ProjectConfig(
            project=ProjectMetadataSpec(
                name=req.name.strip(),
                repository=repo_norm,
                base_branch=base_branch,
            ),
            agents=req.agents,
        )

        proj_dir = ensure_secure_dir(db.projects_dir / project_id)
        clone_path = str(proj_dir / "repo")
        proj_yaml_path = proj_dir / "project.yaml"
        proj_yaml_path.write_text(
            yaml.safe_dump(proj_cfg.model_dump(mode="json"), sort_keys=False),
            encoding="utf-8",
        )

        now = utc_now_iso()

        try:
            with db.begin_immediate() as conn:
                conn.execute(
                    """
                    INSERT INTO projects (
                      id, name, github_repo, visibility, base_branch,
                      protection_status, protection_json, merge_queue,
                      clone_path, config_json, policy_version,
                      detected_stack_json, status, blocked_reason,
                      created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'unknown', NULL, 0, ?, ?, 1, NULL, ?, ?, ?, ?);
                    """,
                    (
                        project_id,
                        req.name.strip(),
                        repo_norm,
                        visibility,
                        base_branch,
                        clone_path,
                        json.dumps(proj_cfg.model_dump(mode="json")),
                        status_val,
                        blocked_reason,
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO audit_log (id, project_id, actor, action, detail_json, correlation_id, at)
                    VALUES (?, ?, 'user', 'project_registered', ?, ?, ?);
                    """,
                    (
                        new_ulid(),
                        project_id,
                        json.dumps(
                            {
                                "github_repo": repo_norm,
                                "status": status_val,
                                "blocked_reason": blocked_reason,
                            }
                        ),
                        new_ulid(),
                        now,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM projects WHERE id = ?;", (project_id,)
                ).fetchone()
        except Exception as exc:
            if "UNIQUE constraint failed" in str(exc):
                raise HTTPException(
                    status_code=409,
                    detail=f"Project name or repository {repo_norm!r} is already registered. If it was removed, restore it under Archived projects in Settings (ATLAS-012)",
                ) from exc
            raise

        broadcaster.publish({"type": "project_registered", "project_id": project_id, "at": now})
        out = dict(row)
        out["role_evaluation"] = _role_evaluation_dict(role_eval)
        return out

    @app.delete("/api/v1/projects/{project_id}")
    def remove_project(project_id: str) -> dict[str, Any]:
        now = utc_now_iso()
        with db.begin_immediate() as conn:
            proj = conn.execute("SELECT * FROM projects WHERE id = ?;", (project_id,)).fetchone()
            if not proj:
                raise HTTPException(status_code=404, detail="Project not found")
            if proj["status"] != "archived":
                unfinished = conn.execute(
                    "SELECT 1 FROM tasks WHERE project_id = ? AND state NOT IN ('COMPLETED', 'CANCELLED') LIMIT 1;",
                    (project_id,),
                ).fetchone()
                if unfinished:
                    raise HTTPException(status_code=409, detail="Finish or cancel this project's tasks before removing it. Paused tasks must be cancelled too.")
                conn.execute("UPDATE projects SET status = 'archived', updated_at = ? WHERE id = ?;", (now, project_id))
                _audit(conn, project_id, "project_archived", {}, now)
            updated = conn.execute("SELECT * FROM projects WHERE id = ?;", (project_id,)).fetchone()
        broadcaster.publish({"type": "project_archived", "project_id": project_id, "at": now})
        return dict(updated)

    @app.post("/api/v1/projects/{project_id}/restore")
    def restore_project(project_id: str) -> dict[str, Any]:
        registry = load_agent_registry(resolved_dir)
        now = utc_now_iso()
        with db.begin_immediate() as conn:
            proj = conn.execute("SELECT * FROM projects WHERE id = ?;", (project_id,)).fetchone()
            if not proj:
                raise HTTPException(status_code=404, detail="Project not found")
            if proj["status"] == "archived":
                roles = ProjectRoleMapping.model_validate(json.loads(proj["config_json"]).get("agents", {}))
                evaluation = evaluate_project_roles(roles, registry, repo_visibility=str(proj["visibility"]))
                status, reason = _project_block_after_role_eval("active", proj["blocked_reason"], evaluation)
                conn.execute("UPDATE projects SET status = ?, blocked_reason = ?, updated_at = ? WHERE id = ?;", (status, reason, now, project_id))
                _audit(conn, project_id, "project_restored", {}, now)
            updated = conn.execute("SELECT * FROM projects WHERE id = ?;", (project_id,)).fetchone()
        broadcaster.publish({"type": "project_restored", "project_id": project_id, "at": now})
        return dict(updated)

    @app.get("/api/v1/projects/{project_id}")
    def get_project(project_id: str) -> dict[str, Any]:
        registry = load_agent_registry(resolved_dir)
        with db.read_conn() as conn:
            proj = conn.execute(
                "SELECT * FROM projects WHERE id = ?;", (project_id,)
            ).fetchone()
            if not proj:
                raise HTTPException(status_code=404, detail="Project not found")
            tasks = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM tasks WHERE project_id = ? ORDER BY created_at DESC;",
                    (project_id,),
                ).fetchall()
            ]
        cfg_dict = json.loads(proj["config_json"])
        roles = ProjectRoleMapping.model_validate(cfg_dict.get("agents", {}))
        role_eval = evaluate_project_roles(
            roles, registry, repo_visibility=str(proj["visibility"])
        )
        res = dict(proj)
        res["tasks"] = tasks
        res["role_evaluation"] = _role_evaluation_dict(role_eval)
        return res

    @app.put("/api/v1/projects/{project_id}/config")
    def update_project_config(
        project_id: str, req: UpdateProjectConfigRequest
    ) -> dict[str, Any]:
        registry = load_agent_registry(resolved_dir)
        with db.begin_immediate() as conn:
            proj = conn.execute(
                "SELECT * FROM projects WHERE id = ?;", (project_id,)
            ).fetchone()
            if not proj:
                raise HTTPException(status_code=404, detail="Project not found")

            if proj["status"] == "archived":
                raise HTTPException(status_code=409, detail="Restore this project before editing it")
            cfg_data = json.loads(proj["config_json"])
            if req.name is not None:
                cfg_data["project"]["name"] = req.name
            if req.base_branch is not None:
                busy = conn.execute(
                    "SELECT 1 FROM tasks WHERE project_id = ? AND state NOT IN ('CREATED', 'COMPLETED', 'CANCELLED') LIMIT 1;",
                    (project_id,),
                ).fetchone()
                if req.base_branch != proj["base_branch"] and busy:
                    raise HTTPException(status_code=409, detail="Finish or cancel existing work before changing the base branch")
            if req.name is not None:
                duplicate = conn.execute("SELECT 1 FROM projects WHERE name = ? AND id != ?;", (req.name, project_id)).fetchone()
                if duplicate:
                    raise HTTPException(status_code=409, detail="A project with this name already exists")
            if req.base_branch is not None:
                cfg_data["project"]["base_branch"] = req.base_branch
            if req.agents is not None:
                cfg_data["agents"] = req.agents.model_dump(mode="json")
            if req.required_checks is not None:
                cfg_data.setdefault("ci", {})["required_checks"] = req.required_checks
            if req.writable_paths is not None:
                cfg_data.setdefault("tests", {})["writable_paths"] = req.writable_paths

            validated_cfg = ProjectConfig.model_validate(cfg_data)
            role_eval = evaluate_project_roles(
                validated_cfg.agents,
                registry,
                repo_visibility=str(proj["visibility"]),
            )

            status_val, blocked_reason = _project_block_after_role_eval(
                str(proj["status"]), proj["blocked_reason"], role_eval
            )

            new_policy_ver = bump_project_policy_version(conn, project_id)
            now = utc_now_iso()
            conn.execute(
                """
                UPDATE projects
                SET name = ?, base_branch = ?, config_json = ?, status = ?, blocked_reason = ?, updated_at = ?
                WHERE id = ?;
                """,
                (
                    validated_cfg.project.name,
                    validated_cfg.project.base_branch,
                    json.dumps(validated_cfg.model_dump(mode="json")),
                    status_val,
                    blocked_reason,
                    now,
                    project_id,
                ),
            )
            proj_dir = ensure_secure_dir(db.projects_dir / project_id)
            (proj_dir / "project.yaml").write_text(
                yaml.safe_dump(validated_cfg.model_dump(mode="json"), sort_keys=False),
                encoding="utf-8",
            )
            conn.execute(
                """
                INSERT INTO audit_log (id, project_id, actor, action, detail_json, correlation_id, at)
                VALUES (?, ?, 'user', 'project_config_updated', ?, ?, ?);
                """,
                (
                    new_ulid(),
                    project_id,
                    json.dumps({"policy_version": new_policy_ver}),
                    new_ulid(),
                    now,
                ),
            )
            updated = conn.execute(
                "SELECT * FROM projects WHERE id = ?;", (project_id,)
            ).fetchone()
        return dict(updated)

    # ------------------------------------------------------------------
    # Tasks & Phases Endpoints
    # ------------------------------------------------------------------

    @app.get("/api/v1/projects/{project_id}/tasks")
    def list_project_tasks(project_id: str) -> list[dict[str, Any]]:
        with db.read_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE project_id = ? ORDER BY created_at DESC;",
                (project_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def _insert_task(
        conn: Any,
        *,
        project_id: str,
        policy_version: int,
        title: str,
        request_text: str,
        category: str,
        priority: int,
        criteria: list[dict[str, Any]],
        now: str,
        reason: str,
        actor: str,
        roadmap_id: str | None = None,
        roadmap_seq: int | None = None,
    ) -> dict[str, Any]:
        task_id = new_ulid()
        conn.execute(
            """
            INSERT INTO tasks (
              id, project_id, title, request_text, category, priority,
              autonomy, acceptance_criteria_json, state, resume_to,
              blocked_reason, current_plan_id, generation, version,
              created_at, updated_at, roadmap_id, roadmap_seq
            ) VALUES (?, ?, ?, ?, ?, ?, 'supervised', ?, ?, NULL, NULL, NULL, 1, 0, ?, ?, ?, ?);
            """,
            (
                task_id,
                project_id,
                title,
                request_text,
                category,
                priority,
                json.dumps(criteria),
                TaskState.CREATED.value,
                now,
                now,
                roadmap_id,
                roadmap_seq,
            ),
        )
        # Record initial creation transition in `transitions` so the timeline shows it from persisted transitions
        conn.execute(
            """
            INSERT INTO transitions (
              id, project_id, entity_type, entity_id, from_state, to_state,
              reason, actor, candidate_id, policy_version, underlying,
              evidence_refs_json, correlation_id, at
            ) VALUES (?, ?, 'task', ?, NULL, ?, ?, ?, NULL, ?, 0, '[]', ?, ?);
            """,
            (
                new_ulid(),
                project_id,
                task_id,
                TaskState.CREATED.value,
                reason,
                actor,
                policy_version,
                new_ulid(),
                now,
            ),
        )
        return dict(conn.execute("SELECT * FROM tasks WHERE id = ?;", (task_id,)).fetchone())

    @app.post("/api/v1/projects/{project_id}/tasks", status_code=201)
    def create_task(project_id: str, req: CreateTaskRequest) -> dict[str, Any]:
        now = utc_now_iso()
        criteria = [
            {
                "id": c.id or f"AC-{idx + 1}",
                "text": c.text.strip(),
                "mandatory": c.mandatory,
            }
            for idx, c in enumerate(req.acceptance_criteria)
        ]
        if not criteria:
            criteria = [{"id": "AC-1", "text": req.title.strip(), "mandatory": True}]

        with db.begin_immediate() as conn:
            proj = conn.execute(
                "SELECT id, policy_version FROM projects WHERE id = ? AND status != 'archived';",
                (project_id,),
            ).fetchone()
            if not proj:
                raise HTTPException(status_code=404, detail="Project not found")
            task = _insert_task(
                conn,
                project_id=project_id,
                policy_version=int(proj["policy_version"]),
                title=req.title.strip(),
                request_text=req.request_text.strip(),
                category=req.category,
                priority=req.priority,
                criteria=criteria,
                now=now,
                reason="Task created by user",
                actor="user",
            )

        broadcaster.publish(
            {
                "type": "task_created",
                "project_id": project_id,
                "task_id": task["id"],
                "state": TaskState.CREATED.value,
                "at": now,
            }
        )
        return task

    @app.post("/api/v1/projects/{project_id}/roadmap", status_code=201)
    def create_roadmap(project_id: str, req: CreateRoadmapRequest) -> dict[str, Any]:
        """Have the project's architect plan the goal in phases; each phase becomes a task."""
        registry = load_agent_registry(resolved_dir)
        with db.read_conn() as conn:
            proj = conn.execute(
                "SELECT * FROM projects WHERE id = ? AND status != 'archived';", (project_id,)
            ).fetchone()
        if not proj:
            raise HTTPException(status_code=404, detail="Project not found")
        roles = ProjectRoleMapping.model_validate(json.loads(proj["config_json"]).get("agents", {}))
        agent = registry.agents.get(roles.architect)
        if agent is None:
            raise HTTPException(
                status_code=422,
                detail=f"The architect agent {roles.architect!r} is not in the agent registry. Choose one in Settings.",
            )

        try:
            plan = planner(
                PlanContext(
                    project_name=str(proj["name"]),
                    repository=str(proj["github_repo"]),
                    base_branch=str(proj["base_branch"]),
                    goal=req.goal,
                    agent_id=roles.architect,
                    agent=agent,
                )
            )
        except PlanGenerationError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        roadmap_id = new_ulid()
        now = utc_now_iso()
        with db.begin_immediate() as conn:
            current = conn.execute(
                "SELECT policy_version FROM projects WHERE id = ? AND status != 'archived';", (project_id,)
            ).fetchone()
            if not current:
                raise HTTPException(status_code=404, detail="Project not found")
            # A refined plan replaces the earlier one: phases that were never started are retired.
            stale = conn.execute(
                "SELECT id FROM tasks WHERE project_id = ? AND roadmap_id IS NOT NULL AND state = ?;",
                (project_id, TaskState.CREATED.value),
            ).fetchall()
            for row in stale:
                apply_task_transition(
                    conn,
                    task_id=str(row["id"]),
                    to_state=TaskState.CANCELLED.value,
                    reason=f"Superseded by a refined plan ({roadmap_id})",
                    actor="user",
                )
            conn.execute(
                "INSERT INTO roadmaps (id, project_id, goal, summary, author_agent, created_at) VALUES (?, ?, ?, ?, ?, ?);",
                (roadmap_id, project_id, req.goal, plan.summary, roles.architect, now),
            )
            tasks = [
                _insert_task(
                    conn,
                    project_id=project_id,
                    policy_version=int(current["policy_version"]),
                    title=phase.title,
                    request_text=phase.objective,
                    category=phase.category,
                    priority=3,
                    criteria=[
                        {"id": f"AC-{n + 1}", "text": text, "mandatory": True}
                        for n, text in enumerate(phase.acceptance_criteria)
                    ],
                    now=now,
                    reason=f"Phase {seq} of roadmap planned by {roles.architect}",
                    actor=roles.architect,
                    roadmap_id=roadmap_id,
                    roadmap_seq=seq,
                )
                for seq, phase in enumerate(plan.phases, start=1)
            ]
            _audit(conn, project_id, "roadmap_created", {"roadmap_id": roadmap_id, "phases": len(tasks)}, now)

        for task in tasks:
            broadcaster.publish(
                {
                    "type": "task_created",
                    "project_id": project_id,
                    "task_id": task["id"],
                    "state": TaskState.CREATED.value,
                    "at": now,
                }
            )
        return {
            "id": roadmap_id,
            "project_id": project_id,
            "goal": req.goal,
            "summary": plan.summary,
            "author_agent": roles.architect,
            "created_at": now,
            "tasks": tasks,
        }

    @app.get("/api/v1/projects/{project_id}/roadmaps")
    def list_roadmaps(project_id: str) -> list[dict[str, Any]]:
        with db.read_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM roadmaps WHERE project_id = ? ORDER BY created_at DESC, id DESC;", (project_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def _read_task_detail(conn: Any, task_id: str) -> dict[str, Any]:
        task = conn.execute(
            "SELECT * FROM tasks WHERE id = ?;", (task_id,)
        ).fetchone()
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        phases = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM phases WHERE task_id = ? ORDER BY seq ASC;",
                (task_id,),
            ).fetchall()
        ]
        phase_ids = [str(p["id"]) for p in phases]

        if phase_ids:
            ph_placeholders = ", ".join("?" for _ in phase_ids)
            transitions = [
                dict(r)
                for r in conn.execute(
                    f"""
                    SELECT * FROM transitions
                    WHERE (entity_type = 'task' AND entity_id = ?)
                       OR (entity_type = 'phase' AND entity_id IN ({ph_placeholders}))
                    ORDER BY at ASC, rowid ASC;
                    """,
                    (task_id, *phase_ids),
                ).fetchall()
            ]
        else:
            transitions = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT * FROM transitions
                    WHERE entity_type = 'task' AND entity_id = ?
                    ORDER BY at ASC, rowid ASC;
                    """,
                    (task_id,),
                ).fetchall()
            ]
        res = dict(task)
        res["acceptance_criteria"] = json.loads(task["acceptance_criteria_json"] or "[]")
        res["phases"] = phases
        res["timeline"] = transitions
        return res

    @app.get("/api/v1/tasks/{task_id}")
    def get_task(task_id: str) -> dict[str, Any]:
        """Return task state, phases, and its persisted transition timeline."""
        with db.read_conn() as conn:
            return _read_task_detail(conn, task_id)

    @app.get("/api/v1/tasks/{task_id}/history")
    def get_task_history(task_id: str) -> dict[str, Any]:
        """Read persisted investigation metadata; credentials and raw process arguments are excluded."""
        with db.read_conn() as conn:
            # One read snapshot keeps exported task state and evidence consistent.
            conn.execute("BEGIN;")
            task = _read_task_detail(conn, task_id)
            project_id = task["project_id"]
            executions = [dict(row) for row in conn.execute(
                "SELECT id, project_id, task_id, phase_id, candidate_id, step_id, kind, agent_id, role, "
                "model, provider, model_family, status, retry_count, git_sha, exit_code, error_class, "
                "error_detail, correlation_id, started_at, finished_at, created_at "
                "FROM executions WHERE task_id = ? AND project_id = ? ORDER BY created_at, id;",
                (task_id, project_id),
            )]
            steps = [dict(row) for row in conn.execute(
                "SELECT * FROM workflow_steps WHERE project_id = ? AND "
                "((entity_type = 'task' AND entity_id = ?) OR "
                "(entity_type = 'phase' AND entity_id IN (SELECT id FROM phases WHERE task_id = ? AND project_id = ?))) "
                "ORDER BY created_at, step_id;", (project_id, task_id, task_id, project_id),
            )]
            artifacts = [dict(row) for row in conn.execute(
                "SELECT a.* FROM artifact_refs a JOIN executions e ON a.execution_id = e.id AND a.project_id = e.project_id "
                "WHERE e.task_id = ? AND e.project_id = ? ORDER BY a.created_at, a.id;", (task_id, project_id),
            )]
            approvals = [dict(row) for row in conn.execute(
                "SELECT * FROM approvals WHERE project_id = ? AND "
                "(task_id = ? OR phase_id IN (SELECT id FROM phases WHERE task_id = ? AND project_id = ?)) "
                "ORDER BY requested_at, id;", (project_id, task_id, task_id, project_id),
            )]
            operations = [dict(row) for row in conn.execute(
                "SELECT id, phase_id, candidate_id, step_id, op, status, attempts, remote_ref, last_error, created_at, updated_at "
                "FROM outbox_ops WHERE project_id = ? AND phase_id IN "
                "(SELECT id FROM phases WHERE task_id = ? AND project_id = ?) ORDER BY created_at, id;",
                (project_id, task_id, project_id),
            )]

        def scrub(value: Any) -> Any:
            if isinstance(value, str):
                return redactor.redact(value)
            if isinstance(value, list):
                return [scrub(item) for item in value]
            if isinstance(value, dict):
                return {key: scrub(item) for key, item in value.items()}
            return value

        return dict(scrub({
            "schema_version": 1, "exported_at": utc_now_iso(), "task": task,
            "workflow_thread_id": engine.thread_id("task", task_id, int(task["generation"])),
            "executions": executions, "workflow_steps": steps, "artifacts": artifacts,
            "approvals": approvals, "remote_operations": operations,
        }))

    def _run_in_background(task_id: str) -> None:
        """Drive the task's built steps; failures leave the task in its last recorded state."""
        try:
            engine.run_until_halt(task_id)
        except Exception:  # noqa: BLE001 - a background crash must not take the server down
            logging.getLogger("atlas.engine").exception("Task %s run failed", task_id)
        with db.read_conn() as conn:
            row = conn.execute("SELECT state FROM tasks WHERE id = ?;", (task_id,)).fetchone()
        if row:
            broadcaster.publish(
                {"type": "task_transition", "task_id": task_id, "state": row["state"], "at": utc_now_iso()}
            )

    def _task_row(task_id: str) -> dict[str, Any]:
        with db.read_conn() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?;", (task_id,)).fetchone()
        return dict(row)

    @app.post("/api/v1/tasks/{task_id}/start")
    def start_task_endpoint(task_id: str, background: BackgroundTasks) -> dict[str, Any]:
        try:
            engine.begin_task(task_id)
        except EntityNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (InvalidTransitionError, GuardFailedError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        background.add_task(_run_in_background, task_id)
        row = _task_row(task_id)
        broadcaster.publish(
            {"type": "task_transition", "task_id": task_id, "state": row["state"], "at": utc_now_iso()}
        )
        return row

    @app.post("/api/v1/tasks/{task_id}/retry")
    def retry_task_endpoint(task_id: str, background: BackgroundTasks) -> dict[str, Any]:
        try:
            engine.retry_task(task_id)
        except EntityNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (InvalidTransitionError, GuardFailedError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        background.add_task(_run_in_background, task_id)
        row = _task_row(task_id)
        broadcaster.publish(
            {"type": "task_transition", "task_id": task_id, "state": row["state"], "at": utc_now_iso()}
        )
        return row

    def _task_project(task_id: str) -> tuple[str, str]:
        with db.read_conn() as conn:
            row = conn.execute("SELECT project_id, state FROM tasks WHERE id = ?;", (task_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return str(row["project_id"]), str(row["state"])

    @app.get("/api/v1/tasks/{task_id}/plan")
    def get_task_plan(task_id: str) -> dict[str, Any]:
        project_id, _ = _task_project(task_id)
        try:
            return load_plan_doc(db.projects_dir, project_id, task_id)
        except CapabilityUnavailableError as exc:
            raise HTTPException(status_code=404, detail="No plan has been drafted for this task yet") from exc

    @app.get("/api/v1/tasks/{task_id}/candidate")
    def get_task_candidate(task_id: str) -> dict[str, Any]:
        project_id, _ = _task_project(task_id)
        candidate = load_candidate(db.projects_dir, project_id, task_id)
        if candidate is None:
            raise HTTPException(status_code=404, detail="No candidate has been produced for this task yet")
        return candidate

    @app.post("/api/v1/tasks/{task_id}/plan/approve")
    def approve_task_plan(task_id: str, background: BackgroundTasks) -> dict[str, Any]:
        _task_project(task_id)
        try:
            with db.begin_immediate() as conn:
                state = conn.execute("SELECT state FROM tasks WHERE id = ?;", (task_id,)).fetchone()["state"]
                if state != TaskState.AWAITING_APPROVAL.value:
                    raise HTTPException(status_code=409, detail=f"Task is in state {state}, not awaiting approval")
                apply_task_transition(
                    conn,
                    task_id=task_id,
                    to_state=TaskState.READY_FOR_IMPLEMENTATION.value,
                    reason="Plan approved by user",
                    actor="user",
                )
        except (InvalidTransitionError, GuardFailedError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        background.add_task(_run_in_background, task_id)
        row = _task_row(task_id)
        broadcaster.publish(
            {"type": "task_transition", "task_id": task_id, "state": row["state"], "at": utc_now_iso()}
        )
        return row

    @app.post("/api/v1/tasks/{task_id}/plan/request-changes")
    def request_plan_changes(
        task_id: str, req: RequestPlanChangesRequest, background: BackgroundTasks
    ) -> dict[str, Any]:
        project_id, _ = _task_project(task_id)
        try:
            with db.begin_immediate() as conn:
                state = conn.execute("SELECT state FROM tasks WHERE id = ?;", (task_id,)).fetchone()["state"]
                if state != TaskState.AWAITING_APPROVAL.value:
                    raise HTTPException(status_code=409, detail=f"Task is in state {state}, not awaiting approval")
                apply_task_transition(
                    conn,
                    task_id=task_id,
                    to_state=TaskState.PLAN_REVISION.value,
                    reason="Changes requested by user",
                    actor="user",
                )
        except (InvalidTransitionError, GuardFailedError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        append_user_feedback(db.projects_dir, project_id, task_id, req.comment)
        background.add_task(_run_in_background, task_id)
        row = _task_row(task_id)
        broadcaster.publish(
            {"type": "task_transition", "task_id": task_id, "state": row["state"], "at": utc_now_iso()}
        )
        return row

    @app.post("/api/v1/tasks/{task_id}/pause")
    def pause_task_endpoint(task_id: str) -> dict[str, Any]:
        try:
            engine.pause_task(task_id, actor="user")
        except EntityNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (InvalidTransitionError, GuardFailedError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        with db.read_conn() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?;", (task_id,)).fetchone()
        broadcaster.publish(
            {"type": "task_transition", "task_id": task_id, "state": row["state"], "at": utc_now_iso()}
        )
        return dict(row)

    @app.post("/api/v1/tasks/{task_id}/resume")
    def resume_task_endpoint(task_id: str) -> dict[str, Any]:
        try:
            engine.resume_task(task_id)
        except EntityNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (InvalidTransitionError, GuardFailedError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        with db.read_conn() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?;", (task_id,)).fetchone()
        broadcaster.publish(
            {"type": "task_transition", "task_id": task_id, "state": row["state"], "at": utc_now_iso()}
        )
        return dict(row)

    @app.post("/api/v1/tasks/{task_id}/cancel")
    def cancel_task_endpoint(task_id: str) -> dict[str, Any]:
        try:
            engine.cancel_task(task_id, actor="user")
        except EntityNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (InvalidTransitionError, GuardFailedError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        with db.read_conn() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?;", (task_id,)).fetchone()
        broadcaster.publish(
            {"type": "task_transition", "task_id": task_id, "state": row["state"], "at": utc_now_iso()}
        )
        return dict(row)

    @app.post("/api/v1/tasks/{task_id}/rebuild")
    def rebuild_task_endpoint(task_id: str) -> dict[str, Any]:
        try:
            engine.rebuild_task(task_id, actor="user")
        except EntityNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (RebuildPreconditionError, InvalidTransitionError, GuardFailedError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        with db.read_conn() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?;", (task_id,)).fetchone()
        broadcaster.publish(
            {"type": "task_transition", "task_id": task_id, "state": row["state"], "at": utc_now_iso()}
        )
        return dict(row)

    @app.post("/api/v1/phases/{phase_id}/pause")
    def pause_phase_endpoint(phase_id: str) -> dict[str, Any]:
        try:
            engine.request_pause("phase", phase_id, actor="user")
        except EntityNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (InvalidTransitionError, GuardFailedError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        with db.read_conn() as conn:
            row = conn.execute("SELECT * FROM phases WHERE id = ?;", (phase_id,)).fetchone()
        broadcaster.publish(
            {"type": "phase_transition", "phase_id": phase_id, "state": row["state"], "at": utc_now_iso()}
        )
        return dict(row)

    @app.post("/api/v1/phases/{phase_id}/resume")
    def resume_phase_endpoint(phase_id: str) -> dict[str, Any]:
        with db.begin_immediate() as conn:
            row = conn.execute("SELECT * FROM phases WHERE id = ?;", (phase_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Phase not found")
            if row["state"] != PhaseState.PAUSED.value:
                raise HTTPException(status_code=409, detail=f"Phase is in state {row['state']}, not PAUSED")
            # If remote merge was withdrawn while paused, route to CI_VALIDATION instead of MERGING (ATLAS-008 r4)
            target = str(row["resume_to"] or PhaseState.PENDING.value)
            if int(row["remote_merge_withdrawn"] or 0) == 1 and target in (
                PhaseState.MERGE_PENDING_REMOTE.value,
                PhaseState.CANCELLING_REMOTE_MERGE.value,
            ):
                target = PhaseState.CI_VALIDATION.value
                conn.execute(
                    "UPDATE phases SET resume_to = ?, pause_requested_at = NULL WHERE id = ?;",
                    (target, phase_id),
                )
            else:
                conn.execute("UPDATE phases SET pause_requested_at = NULL WHERE id = ?;", (phase_id,))
            apply_phase_transition(
                conn,
                phase_id=phase_id,
                to_state=target,
                reason=f"Phase resumed by user to {target}",
                actor="user",
            )
            updated = conn.execute("SELECT * FROM phases WHERE id = ?;", (phase_id,)).fetchone()
        broadcaster.publish(
            {"type": "phase_transition", "phase_id": phase_id, "state": updated["state"], "at": utc_now_iso()}
        )
        return dict(updated)

    @app.post("/api/v1/phases/{phase_id}/cancel")
    def cancel_phase_endpoint(phase_id: str) -> dict[str, Any]:
        try:
            engine.request_cancel("phase", phase_id, actor="user")
        except EntityNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (InvalidTransitionError, GuardFailedError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        with db.read_conn() as conn:
            row = conn.execute("SELECT * FROM phases WHERE id = ?;", (phase_id,)).fetchone()
        broadcaster.publish(
            {"type": "phase_transition", "phase_id": phase_id, "state": row["state"], "at": utc_now_iso()}
        )
        return dict(row)

    @app.post("/api/v1/phases/{phase_id}/rebuild")
    def rebuild_phase_endpoint(phase_id: str) -> dict[str, Any]:
        try:
            engine.rebuild_thread("phase", phase_id, actor="user")
        except EntityNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (RebuildPreconditionError, InvalidTransitionError, GuardFailedError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        with db.read_conn() as conn:
            row = conn.execute("SELECT * FROM phases WHERE id = ?;", (phase_id,)).fetchone()
        broadcaster.publish(
            {"type": "phase_transition", "phase_id": phase_id, "state": row["state"], "at": utc_now_iso()}
        )
        return dict(row)

    # ------------------------------------------------------------------
    # SSE Stream Endpoint
    # ------------------------------------------------------------------

    @app.get("/api/v1/events")
    async def sse_events(request: Request) -> StreamingResponse:
        queue = broadcaster.subscribe()

        async def event_generator() -> AsyncIterator[str]:
            try:
                yield f"data: {json.dumps({'type': 'connected', 'at': utc_now_iso()})}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15.0)
                        yield f"data: {json.dumps(event)}\n\n"
                    except TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                broadcaster.unsubscribe(queue)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    # ------------------------------------------------------------------
    # Static Frontend Serving (Dashboard v1)
    # ------------------------------------------------------------------

    frontend_dist = Path(__file__).resolve().parents[4] / "frontend" / "dist"
    if (frontend_dist / "assets").exists():
        app.mount(
            "/assets",
            StaticFiles(directory=str(frontend_dist / "assets")),
            name="assets",
        )

    @app.get("/{full_path:path}", response_model=None)
    def serve_spa(full_path: str) -> FileResponse | HTMLResponse:
        index_file = frontend_dist / "index.html"
        if index_file.exists():
            return FileResponse(str(index_file))
        return HTMLResponse(
            content=(
                "<!doctype html><html><head><meta charset='utf-8'>"
                "<title>ATLAS Orchestrator</title></head>"
                "<body><h1>ATLAS Orchestrator</h1>"
                "<p>Backend is running on 127.0.0.1. Build the frontend with "
                "<code>npm --prefix frontend run build</code> to view the full dashboard.</p>"
                "</body></html>"
            )
        )

    return app
