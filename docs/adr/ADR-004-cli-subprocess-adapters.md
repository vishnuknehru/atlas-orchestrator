# ADR-004: Agents are invoked only as unmodified official CLIs via subprocess

> Amended by [ADR-007](ADR-007-portable-pluggable-providers.md): ATLAS is self-hosted and single-operator, agents come from a user-configured registry, and API-key billing is an explicit per-agent opt-in (D15).

- Status: Proposed
- Date: 2026-10-22

## Decision
- Agent CLIs (built-in: Claude Code and Codex) are run as their unmodified binaries (`claude -p …`, `codex exec …`), as the logged-in OS user, with subscription login managed solely by those CLIs. Other CLIs plug in only through vetted adapter manifests (ADR-007 §3).
- ATLAS never reads, copies, stores or proxies the subscription credentials that CLIs manage. The only provider credentials it stores are API keys the user explicitly gives it for an agent configured with `billing: api_key` (ADR-007 §5). It never uses the Agent SDK with subscription credentials, never runs browser automation and never calls provider HTTP endpoints directly.
- `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `CODEX_API_KEY` and other provider key variables are stripped from every agent environment, so billing cannot silently switch to an API account. The one exception is an agent configured with `billing: api_key`: only that agent's own process receives its own key, under the variable its manifest names.
- ATLAS is self-hosted and single-operator: anyone may run their own copy with their own accounts. It must not be offered as a hosted or multi-user service that uses anyone else's subscription. [Claude terms](https://code.claude.com/docs/en/legal-and-compliance) (excerpts saved in [`evidence/claude-legal-compliance-2026-10-22.md`](../evidence/claude-legal-compliance-2026-10-22.md)) forbid third-party developers routing requests through Free/Pro/Max credentials "on behalf of their users". They explicitly allow "an end user … signing in to the unmodified Claude Code binary with their own Claude subscription". Self-hosted ATLAS is that case: each operator runs their own copy with their own login, and ATLAS never collects, stores or intermediates Claude.ai credentials. A hosted ATLAS would also fall under the Commercial Terms conditions (unmodified binary, no intermediated billing). That is a further reason the non-goal stands. The page also says Pro/Max usage limits "assume ordinary, individual usage"; ATLAS's single-operator, one-invocation-per-agent default is designed with that in mind.
- Codex (any agent using the `codex_cli` manifest, whatever its billing; D5 unchanged by ADR-007) on **public** repositories is blocked, with no override in this release (D5). [OpenAI docs](https://learn.chatgpt.com/docs/non-interactive-mode) say: "Do not use this workflow for public or open-source repositories."

## Consequences
- Startup capability probes and version pinning cover CLI flag drift.
- Exact quota numbers are unavailable. The capacity state is inferred from documented signals only.
