"""Slice 1.3 tests: Pydantic config schemas, safe defaults, ADR-005/007 independence, and Redactor."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from atlas.config.schemas import (
    AgentConfigEntry,
    AgentRegistryConfig,
    GlobalConfig,
    ProjectRoleMapping,
    check_independence,
    derive_model_family,
    derive_provider,
    evaluate_project_roles,
)
from atlas.config.secret_store import InMemorySecretStore, Redactor


def test_global_config_safe_defaults_and_unsafe_rejection() -> None:
    cfg = GlobalConfig()
    assert cfg.execution.mode == "supervised"
    assert cfg.git.auto_merge is False
    assert cfg.capacity.allow_paid_api_fallback is False
    assert cfg.capacity.allow_unsupported_integrations is False
    assert cfg.recovery.auto_resume is False

    # Reject autonomous mode (D7)
    with pytest.raises(ValidationError):
        GlobalConfig.model_validate({"execution": {"mode": "autonomous"}})

    # Reject auto_merge=True (D6)
    with pytest.raises(ValidationError):
        GlobalConfig.model_validate({"git": {"auto_merge": True}})

    # Reject allow_paid_api_fallback=True (D15)
    with pytest.raises(ValidationError):
        GlobalConfig.model_validate({"capacity": {"allow_paid_api_fallback": True}})

    # Reject allow_unsupported_integrations=True
    with pytest.raises(ValidationError):
        GlobalConfig.model_validate({"capacity": {"allow_unsupported_integrations": True}})

    # Reject unknown keys
    with pytest.raises(ValidationError):
        GlobalConfig.model_validate({"unknown_key": 123})


def test_agent_registry_rejects_unverified_manifests_and_non_loopback_endpoints() -> None:
    # Unverified CLI manifest rejected (ADR-007 §3)
    with pytest.raises(ValidationError, match="Unverified or unknown adapter"):
        AgentConfigEntry(
            adapter="custom_unverified_cli",
            models=["gpt-5"],
            billing="subscription",
        )

    # Non-loopback endpoint rejected without allow_remote_endpoint=True
    with pytest.raises(ValidationError, match="Non-loopback endpoint"):
        AgentConfigEntry(
            adapter="openai_compatible_http",
            models=["llama-4-maverick"],
            model_family="meta",
            billing="api_key",
            endpoint="https://api.example.com/v1",
        )

    # billing='local' ALWAYS rejects non-loopback endpoint even if allow_remote_endpoint=True
    with pytest.raises(ValidationError, match="billing='local' requires a loopback endpoint"):
        AgentConfigEntry(
            adapter="openai_compatible_http",
            models=["llama-4-maverick"],
            model_family="meta",
            billing="local",
            endpoint="https://api.example.com/v1",
            allow_remote_endpoint=True,
        )


def test_profile_a_and_profile_b_independence_evaluation() -> None:
    # Profile A: Recommended (Claude + Codex + Antigravity)
    reg_a = AgentRegistryConfig.default_recommended_profile()
    roles_a = ProjectRoleMapping(
        architect="claude",
        challenger="codex",
        developer="codex",
        reviewer="claude",
        qa="antigravity",
    )
    res_a = evaluate_project_roles(roles_a, reg_a, repo_visibility="private")
    assert res_a.ok is True

    # Profile A on public repo -> BLOCKED because Codex is blocked on public repos (D5)
    res_pub = evaluate_project_roles(roles_a, reg_a, repo_visibility="public")
    assert res_pub.ok is False
    assert res_pub.blocked_reason == "agent_config"
    assert "public repositories (D5)" in (res_pub.detail or "")

    # Profile B: Fully local (Qwen coder + Llama reviewer + manual QA)
    reg_b = AgentRegistryConfig(
        agents={
            "local-coder": AgentConfigEntry(
                adapter="codex_cli",
                models=["qwen3-coder"],
                model_family="alibaba",
                billing="local",
                endpoint="http://127.0.0.1:11434/v1",
            ),
            "local-reviewer": AgentConfigEntry(
                adapter="openai_compatible_http",
                models=["llama-4-maverick"],
                model_family="meta",
                billing="local",
                endpoint="http://127.0.0.1:11434/v1",
            ),
            "manual-qa": AgentConfigEntry(
                adapter="manual_handoff",
                provider="local",
                declared_family_required=True,
            ),
        }
    )
    roles_b = ProjectRoleMapping(
        architect="local-coder",
        challenger="local-reviewer",
        developer="local-coder",
        reviewer="local-reviewer",
        qa="manual-qa",
    )
    res_b = evaluate_project_roles(roles_b, reg_b, repo_visibility="private")
    assert res_b.ok is True
    assert derive_provider(reg_b.agents["local-coder"]) == "local"
    assert derive_provider(reg_b.agents["local-reviewer"]) == "local"


def test_independence_rejections_and_builtin_family_precedence() -> None:
    # Built-in model family map always wins over contradictory user declaration (ADR-005)
    assert derive_model_family("claude-sonnet-4-5", declared_family="openai") == "anthropic"
    assert derive_model_family("gpt-5-codex", declared_family="anthropic") == "openai"
    assert derive_model_family("custom-finetune-v1", declared_family="meta") == "meta"
    assert derive_model_family("custom-unknown-v1", declared_family=None) == "unknown"

    # Unknown provider or family fails independence closed
    assert check_independence("anthropic", "anthropic", "unknown", "openai") is False
    assert check_independence("anthropic", "anthropic", "openai", "unknown") is False

    # Same family fails even across different providers
    assert check_independence("anthropic", "anthropic", "google", "anthropic") is False

    # Same non-local provider fails even across different families
    assert check_independence("openai", "openai", "openai", "alibaba") is False

    # Two local providers succeed when model families differ
    assert check_independence("local", "alibaba", "local", "meta") is True
    assert check_independence("local", "alibaba", "local", "alibaba") is False


def test_cli_api_key_billing_blocked_until_isolation_verified() -> None:
    reg = AgentRegistryConfig.default_recommended_profile()
    reg.agents["claude"] = AgentConfigEntry(
        adapter="claude_cli",
        models=["claude-sonnet-4-5"],
        billing="api_key",
    )
    roles = ProjectRoleMapping()
    res = evaluate_project_roles(roles, reg, repo_visibility="private")
    assert res.ok is False
    assert res.blocked_reason == "api_key_not_isolated"


def test_redactor_exact_value_and_pattern_scrubbing() -> None:
    redactor = Redactor()
    custom_secret = "my_custom_unpatterned_token_998877"
    redactor.register(custom_secret)

    sample = (
        f"Header Authorization: Bearer {custom_secret} and "
        "ghp_1234567890abcdefghijklmnopqrstuvwx and "
        "sk-proj-1234567890abcdefghijklmnop"
    )
    scrubbed = redactor.redact(sample)
    assert custom_secret not in scrubbed
    assert "ghp_" not in scrubbed
    assert "sk-proj-" not in scrubbed
    assert scrubbed.count("[REDACTED]") == 3

    store = InMemorySecretStore()
    store.set_secret("atlas.p1.github", "user", custom_secret)
    assert store.get_secret("atlas.p1.github") == custom_secret
    store.delete_secret("atlas.p1.github")
    assert store.get_secret("atlas.p1.github") is None
