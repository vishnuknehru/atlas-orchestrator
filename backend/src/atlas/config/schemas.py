"""Pydantic schemas for GlobalConfig, AgentRegistryConfig, ProjectConfig, and ADR-005/007 rules."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

CANONICAL_PROVIDERS: frozenset[str] = frozenset(
    {
        "anthropic",
        "openai",
        "google",
        "mistral",
        "deepseek",
        "together",
        "groq",
        "openrouter",
    }
)

KNOWN_MODEL_FAMILIES: frozenset[str] = frozenset(
    {
        "anthropic",
        "openai",
        "google",
        "meta",
        "alibaba",
        "mistral",
        "deepseek",
    }
)


@dataclass(frozen=True)
class AdapterManifestSpec:
    """Built-in verified adapter manifest metadata (ADR-007 §3)."""

    name: str
    kind: Literal["cli", "manual_handoff", "openai_compatible_http"]
    default_provider: str | None
    allowed_roles: frozenset[str]
    verified: bool = True
    credential_isolation_verified: bool = False  # Phase 2 spike item (ADR-007 §5)


ALL_ROLES: frozenset[str] = frozenset({"architect", "challenger", "developer", "reviewer", "qa"})
READ_ONLY_ROLES: frozenset[str] = frozenset({"architect", "challenger", "reviewer", "qa"})
HTTP_READ_ONLY_ROLES: frozenset[str] = frozenset({"challenger", "reviewer"})

ADAPTER_MANIFESTS: dict[str, AdapterManifestSpec] = {
    "claude_cli": AdapterManifestSpec(
        name="claude_cli",
        kind="cli",
        default_provider="anthropic",
        allowed_roles=ALL_ROLES,
        verified=True,
        credential_isolation_verified=False,
    ),
    "codex_cli": AdapterManifestSpec(
        name="codex_cli",
        kind="cli",
        default_provider="openai",
        allowed_roles=ALL_ROLES,
        verified=True,
        credential_isolation_verified=False,
    ),
    "manual_handoff": AdapterManifestSpec(
        name="manual_handoff",
        kind="manual_handoff",
        default_provider=None,
        allowed_roles=READ_ONLY_ROLES,
        verified=True,
        credential_isolation_verified=True,
    ),
    "openai_compatible_http": AdapterManifestSpec(
        name="openai_compatible_http",
        kind="openai_compatible_http",
        default_provider=None,
        allowed_roles=HTTP_READ_ONLY_ROLES,
        verified=True,
        credential_isolation_verified=True,
    ),
}


def derive_builtin_model_family(model_id: str | None) -> str | None:
    """Return built-in model family from model_id prefix (ADR-005), or None if not in built-in map."""
    if not model_id:
        return None
    m = model_id.strip().lower()
    if m.startswith("claude-"):
        return "anthropic"
    if (
        m.startswith("gpt-")
        or m.startswith("o1")
        or m.startswith("o3")
        or m.startswith("o4")
        or m.startswith("codex")
    ):
        return "openai"
    if m.startswith("gemini-") or m.startswith("gemma-"):
        return "google"
    if m.startswith("llama-"):
        return "meta"
    if m.startswith("qwen"):
        return "alibaba"
    if m.startswith(("mistral-", "mixtral-", "codestral-", "devstral-")):
        return "mistral"
    if m.startswith("deepseek-"):
        return "deepseek"
    return None


def derive_model_family(model_id: str | None, declared_family: str | None = None) -> str:
    """Derive effective model family per ADR-005. Built-in map always wins over declared_family."""
    builtin = derive_builtin_model_family(model_id)
    if builtin is not None:
        return builtin
    if declared_family:
        norm = declared_family.strip().lower()
        if norm and norm != "unknown":
            return norm
    return "unknown"


def is_loopback_endpoint(endpoint: str, *, resolve_dns: bool = True) -> bool:
    """Verify that an endpoint points strictly to loopback (127.0.0.1, ::1, or unix socket)."""
    if endpoint.startswith("unix://") or endpoint.startswith("/"):
        return True
    parsed = urlparse(endpoint)
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    # Check literal IP first
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback
    except ValueError:
        pass

    if host.lower() == "localhost":
        if not resolve_dns:
            return True
        try:
            infos = socket.getaddrinfo(host, parsed.port or 80, proto=socket.IPPROTO_TCP)
            if not infos:
                return False
            for info in infos:
                sockaddr = info[4]
                addr_str = str(sockaddr[0])
                if not ipaddress.ip_address(addr_str).is_loopback:
                    return False
            return True
        except OSError:
            return False

    return False


# ---------------------------------------------------------------------------
# GlobalConfig (~/.atlas/config.yaml) — architecture.md §8
# ---------------------------------------------------------------------------


class ExecutionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["supervised"] = "supervised"  # D7: autonomous is rejected
    max_active_phases_per_repository: int = Field(default=1, ge=1, le=1)
    max_review_cycles: int = Field(default=3, ge=1, le=10)
    max_repair_attempts: int = Field(default=3, ge=1, le=10)


class GitConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    use_worktrees: Literal[True] = True
    create_pull_requests: Literal[True] = True
    auto_merge: Literal[False] = False  # D6: auto_merge=True is rejected
    merge_strategy: Literal["squash", "merge", "rebase"] = "squash"


class QualityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    require_tests: bool = True
    require_code_review: bool = True
    require_independent_qa: bool = True
    block_on_critical_findings: Literal[True] = True
    block_on_high_findings: bool = True


class CapacityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    on_quota_exhausted: Literal["pause"] = "pause"
    allow_agent_substitution: bool = False
    allow_paid_api_fallback: Literal[False] = False  # D15: locked to False
    allow_unsupported_integrations: Literal[False] = False


class SafetyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allow_direct_main_changes: Literal[False] = False
    allow_force_push: Literal[False] = False
    allow_secret_modification: bool = False
    allow_destructive_database_commands: bool = False
    allow_production_deployment: Literal[False] = False


class RecoveryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto_resume: Literal[False] = False  # Locked False until Phase 6 (ATLAS-017)


class GlobalRoleDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    architect: str = "claude"
    challenger: str = "codex"
    developer: str = "claude"
    reviewer: str = "codex"
    qa: str = "antigravity"


class GlobalConfig(BaseModel):
    """Global ATLAS configuration with safe defaults (architecture.md §8)."""

    model_config = ConfigDict(extra="forbid")

    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    git: GitConfig = Field(default_factory=GitConfig)
    quality: QualityConfig = Field(default_factory=QualityConfig)
    capacity: CapacityConfig = Field(default_factory=CapacityConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    recovery: RecoveryConfig = Field(default_factory=RecoveryConfig)
    agents: GlobalRoleDefaults = Field(default_factory=GlobalRoleDefaults)


# ---------------------------------------------------------------------------
# AgentRegistryConfig (~/.atlas/agents.yaml) — ADR-007 §2
# ---------------------------------------------------------------------------


class AgentConfigEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adapter: str
    models: list[str] = Field(default_factory=list)
    billing: Literal["subscription", "api_key", "local", "manual"] = "subscription"
    provider: str | None = None
    model_family: str | None = None
    endpoint: str | None = None
    allow_remote_endpoint: bool = False
    declared_family_required: bool = False

    @model_validator(mode="after")
    def _validate_agent_entry(self) -> AgentConfigEntry:
        if self.adapter not in ADAPTER_MANIFESTS:
            raise ValueError(
                f"Unverified or unknown adapter {self.adapter!r}. "
                f"Only verified manifests load in this release: {sorted(ADAPTER_MANIFESTS)}"
            )
        manifest = ADAPTER_MANIFESTS[self.adapter]
        if not manifest.verified:
            raise ValueError(f"Adapter manifest {self.adapter!r} is not verified")

        if self.adapter == "manual_handoff":
            self.billing = "manual"
            if not self.provider:
                raise ValueError("manual_handoff agent requires an explicit 'provider' (e.g. 'google' or 'local')")
            if self.provider != "local" and self.provider not in CANONICAL_PROVIDERS:
                raise ValueError(
                    f"manual_handoff provider {self.provider!r} must be 'local' or one of {sorted(CANONICAL_PROVIDERS)}"
                )
        else:
            if not self.models:
                raise ValueError(f"Agent with adapter {self.adapter!r} must pin at least one model in 'models'")

        if self.adapter == "openai_compatible_http" and not self.endpoint:
            raise ValueError("openai_compatible_http agent requires an 'endpoint' URL")

        if self.billing == "local":
            if not self.endpoint:
                raise ValueError("billing='local' requires an explicit loopback 'endpoint'")
            if not is_loopback_endpoint(self.endpoint):
                raise ValueError(
                    f"billing='local' requires a loopback endpoint (127.0.0.1, ::1, localhost, or unix://), got {self.endpoint!r}"
                )

        if self.endpoint is not None and not self.allow_remote_endpoint:
            if not is_loopback_endpoint(self.endpoint):
                raise ValueError(
                    f"Non-loopback endpoint {self.endpoint!r} rejected without allow_remote_endpoint=True"
                )

        return self


def derive_provider(entry: AgentConfigEntry) -> str:
    """Derive canonical inference provider per ADR-007 §2 precedence rules."""
    # Rule 1: billing: local and loopback endpoint -> 'local'
    if entry.billing == "local" and entry.endpoint and is_loopback_endpoint(entry.endpoint):
        return "local"

    # Rule 2: remote endpoint (allow_remote_endpoint: true)
    if entry.endpoint and entry.allow_remote_endpoint and not is_loopback_endpoint(entry.endpoint):
        if entry.provider and entry.provider in CANONICAL_PROVIDERS:
            return entry.provider
        return "unknown"

    # Rule 3: hosted CLI with subscription or api_key billing -> manifest provider
    manifest = ADAPTER_MANIFESTS.get(entry.adapter)
    if manifest and manifest.kind == "cli" and entry.billing in ("subscription", "api_key"):
        return manifest.default_provider or "unknown"

    # Rule 4: manual_handoff agent -> configured canonical provider or 'local'
    if entry.adapter == "manual_handoff":
        if entry.provider and (entry.provider == "local" or entry.provider in CANONICAL_PROVIDERS):
            return entry.provider
        return "unknown"

    # Loopback HTTP endpoint with non-local billing (e.g. api_key to a local gateway)
    if entry.endpoint and is_loopback_endpoint(entry.endpoint):
        if entry.provider and entry.provider in CANONICAL_PROVIDERS:
            return entry.provider
        return "local"

    return "unknown"


class AgentRegistryConfig(BaseModel):
    """Global agent registry (~/.atlas/agents.yaml)."""

    model_config = ConfigDict(extra="forbid")

    agents: dict[str, AgentConfigEntry] = Field(default_factory=dict)

    @classmethod
    def default_recommended_profile(cls) -> AgentRegistryConfig:
        """Return Profile A (Claude + Codex + Antigravity) from docs/how-to-use.md Step 6."""
        return cls(
            agents={
                "claude": AgentConfigEntry(
                    adapter="claude_cli",
                    models=["claude-sonnet-4-5"],
                    billing="subscription",
                ),
                "codex": AgentConfigEntry(
                    adapter="codex_cli",
                    models=["gpt-5-codex"],
                    billing="subscription",
                ),
                "antigravity": AgentConfigEntry(
                    adapter="manual_handoff",
                    provider="google",
                    declared_family_required=True,
                ),
            }
        )


# ---------------------------------------------------------------------------
# ProjectConfig (~/.atlas/projects/<id>/project.yaml) & Independence (ADR-005)
# ---------------------------------------------------------------------------


class ProjectMetadataSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    repository: str
    base_branch: str = "main"


class ProjectRoleMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    architect: str = "claude"
    challenger: str = "codex"
    developer: str = "claude"
    reviewer: str = "codex"
    qa: str = "antigravity"


class ProjectTestsSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    writable_paths: list[str] = Field(default_factory=lambda: [".venv", ".pytest_cache"])
    commands: list[str] = Field(default_factory=list)
    # Prebuilt image pinned by digest (repository Dockerfile builds are disabled). Saving it is the user's approval.
    image: str | None = None
    timeout_seconds: int = Field(default=600, ge=10, le=3600)


class ProjectCISpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required_checks: list[str] = Field(default_factory=list)


class ProjectConfig(BaseModel):
    """Per-project configuration file (~/.atlas/projects/<id>/project.yaml)."""

    model_config = ConfigDict(extra="forbid")

    project: ProjectMetadataSpec
    agents: ProjectRoleMapping = Field(default_factory=ProjectRoleMapping)
    tests: ProjectTestsSpec = Field(default_factory=ProjectTestsSpec)
    ci: ProjectCISpec = Field(default_factory=ProjectCISpec)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    git: GitConfig = Field(default_factory=GitConfig)
    quality: QualityConfig = Field(default_factory=QualityConfig)
    capacity: CapacityConfig = Field(default_factory=CapacityConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)


def check_independence(
    author_provider: str,
    author_family: str,
    reviewer_provider: str,
    reviewer_family: str,
) -> bool:
    """Enforce ADR-005 review independence across provider AND model family.

    - 'unknown' provider or family never satisfies independence.
    - When both author_provider and reviewer_provider are 'local', the provider check
      is satisfied and independence rests on the model-family check alone (ADR-005).
    """
    ap = author_provider.strip().lower()
    af = author_family.strip().lower()
    rp = reviewer_provider.strip().lower()
    rf = reviewer_family.strip().lower()

    if "unknown" in (ap, af, rp, rf) or not all((ap, af, rp, rf)):
        return False

    provider_ok = (rp != ap) or (rp == "local" and ap == "local")
    family_ok = rf != af
    return provider_ok and family_ok


@dataclass(frozen=True)
class RoleEvaluationResult:
    ok: bool
    blocked_reason: str | None = None
    detail: str | None = None
    remote_endpoint_warnings: tuple[str, ...] = ()


def evaluate_project_roles(
    roles: ProjectRoleMapping,
    registry: AgentRegistryConfig,
    *,
    repo_visibility: str = "private",
) -> RoleEvaluationResult:
    """Validate project role assignments against registry, adapter roles, D5, D15, and ADR-005."""
    role_map = {
        "architect": roles.architect,
        "challenger": roles.challenger,
        "developer": roles.developer,
        "reviewer": roles.reviewer,
        "qa": roles.qa,
    }

    resolved: dict[str, tuple[AgentConfigEntry, str, str]] = {}
    warnings: list[str] = []

    for role_name, agent_id in role_map.items():
        if agent_id not in registry.agents:
            return RoleEvaluationResult(
                ok=False,
                blocked_reason="agent_config",
                detail=f"Role {role_name!r} references unknown agent {agent_id!r}",
            )
        entry = registry.agents[agent_id]
        manifest = ADAPTER_MANIFESTS[entry.adapter]

        # Check adapter allowed roles
        if role_name not in manifest.allowed_roles:
            return RoleEvaluationResult(
                ok=False,
                blocked_reason="agent_config",
                detail=(
                    f"Agent {agent_id!r} (adapter {entry.adapter!r}) cannot fill role {role_name!r}; "
                    f"allowed roles: {sorted(manifest.allowed_roles)}"
                ),
            )

        # D5: Codex agents blocked on public repositories
        if entry.adapter == "codex_cli" and repo_visibility == "public":
            return RoleEvaluationResult(
                ok=False,
                blocked_reason="agent_config",
                detail=f"Agent {agent_id!r} (codex_cli) is blocked on public repositories (D5)",
            )

        # D15 / ADR-007 §5: CLI agent with api_key billing blocked unless credential_isolation verified
        if manifest.kind == "cli" and entry.billing == "api_key" and not manifest.credential_isolation_verified:
            return RoleEvaluationResult(
                ok=False,
                blocked_reason="api_key_not_isolated",
                detail=f"CLI agent {agent_id!r} with billing='api_key' lacks verified credential isolation",
            )

        provider = derive_provider(entry)
        if provider == "unknown":
            return RoleEvaluationResult(
                ok=False,
                blocked_reason="agent_config",
                detail=f"Agent {agent_id!r} has unknown provider",
            )

        if entry.adapter == "manual_handoff":
            # Manual handoff family is declared at import time; use a sentinel distinct from unknown if declared_family_required
            family = entry.model_family or ("__manual_declared__" if entry.declared_family_required else "unknown")
        else:
            primary_model = entry.models[0] if entry.models else None
            family = derive_model_family(primary_model, entry.model_family)

        if family == "unknown":
            return RoleEvaluationResult(
                ok=False,
                blocked_reason="agent_config",
                detail=f"Agent {agent_id!r} has unknown model_family; set 'model_family' in agents.yaml",
            )

        if entry.endpoint and entry.allow_remote_endpoint and not is_loopback_endpoint(entry.endpoint):
            warnings.append(
                f"Agent {agent_id!r} uses remote endpoint {entry.endpoint}; source code will leave this machine."
            )

        resolved[role_name] = (entry, provider, family)

    # ADR-005 independence checks:
    # 1. Plan challenger vs architect
    _, arch_prov, arch_fam = resolved["architect"]
    _, chal_prov, chal_fam = resolved["challenger"]
    if not _check_config_pair_independence(arch_prov, arch_fam, chal_prov, chal_fam):
        return RoleEvaluationResult(
            ok=False,
            blocked_reason="agent_config",
            detail=(
                f"Challenger ({roles.challenger}: {chal_prov}/{chal_fam}) is not independent "
                f"of Architect ({roles.architect}: {arch_prov}/{arch_fam}) per ADR-005"
            ),
        )

    # 2. Code reviewer vs developer
    _, dev_prov, dev_fam = resolved["developer"]
    _, rev_prov, rev_fam = resolved["reviewer"]
    if not _check_config_pair_independence(dev_prov, dev_fam, rev_prov, rev_fam):
        return RoleEvaluationResult(
            ok=False,
            blocked_reason="agent_config",
            detail=(
                f"Reviewer ({roles.reviewer}: {rev_prov}/{rev_fam}) is not independent "
                f"of Developer ({roles.developer}: {dev_prov}/{dev_fam}) per ADR-005"
            ),
        )

    # 3. QA vs developer
    _, qa_prov, qa_fam = resolved["qa"]
    if not _check_config_pair_independence(dev_prov, dev_fam, qa_prov, qa_fam):
        return RoleEvaluationResult(
            ok=False,
            blocked_reason="agent_config",
            detail=(
                f"QA ({roles.qa}: {qa_prov}/{qa_fam}) is not independent "
                f"of Developer ({roles.developer}: {dev_prov}/{dev_fam}) per ADR-005"
            ),
        )

    return RoleEvaluationResult(
        ok=True,
        blocked_reason=None,
        detail=None,
        remote_endpoint_warnings=tuple(warnings),
    )


def _check_config_pair_independence(
    author_prov: str,
    author_fam: str,
    reviewer_prov: str,
    reviewer_fam: str,
) -> bool:
    """Check independence at config time, accounting for manual_handoff declared family."""
    if reviewer_fam == "__manual_declared__":
        # At config time, manual_handoff requires provider independence (or both local);
        # actual model family is enforced at handoff import time.
        return (reviewer_prov != author_prov) or (reviewer_prov == "local" and author_prov == "local")
    return check_independence(author_prov, author_fam, reviewer_prov, reviewer_fam)


# ---------------------------------------------------------------------------
# File loaders & savers
# ---------------------------------------------------------------------------


def load_global_config(data_dir: Path) -> GlobalConfig:
    """Load ~/.atlas/config.yaml or create default safe config."""
    cfg_path = data_dir / "config.yaml"
    if not cfg_path.exists():
        cfg = GlobalConfig()
        cfg_path.write_text(yaml.safe_dump(cfg.model_dump(mode="json"), sort_keys=False), encoding="utf-8")
        return cfg
    raw: dict[str, Any] = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    return GlobalConfig.model_validate(raw)


def load_agent_registry(data_dir: Path) -> AgentRegistryConfig:
    """Load ~/.atlas/agents.yaml or initialize with recommended Profile A."""
    reg_path = data_dir / "agents.yaml"
    if not reg_path.exists():
        reg = AgentRegistryConfig.default_recommended_profile()
        save_agent_registry(data_dir, reg)
        return reg
    raw: dict[str, Any] = yaml.safe_load(reg_path.read_text(encoding="utf-8")) or {}
    return AgentRegistryConfig.model_validate(raw)


def save_agent_registry(data_dir: Path, registry: AgentRegistryConfig) -> None:
    """Persist ~/.atlas/agents.yaml."""
    reg_path = data_dir / "agents.yaml"
    reg_path.write_text(
        yaml.safe_dump(registry.model_dump(mode="json", exclude_none=True), sort_keys=False),
        encoding="utf-8",
    )
