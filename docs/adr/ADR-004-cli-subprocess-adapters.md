# ADR-004: Agents are invoked only as unmodified official CLIs via subprocess

- Status: Proposed
- Date: 2026-10-22

## Decision
- Claude Code and Codex are run as their unmodified binaries (`claude -p …`, `codex exec …`), as the logged-in macOS user, with subscription login managed solely by those CLIs.
- ATLAS never reads, copies, stores or proxies provider tokens. It never uses the Agent SDK with subscription credentials, never runs browser automation and never calls provider HTTP endpoints directly.
- `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` and `CODEX_API_KEY` are stripped from agent environments, so billing cannot silently switch to an API account.
- ATLAS is a personal tool. It must not be offered as a hosted or multi-user service that uses anyone else's subscription. [Claude terms](https://code.claude.com/docs/en/legal-and-compliance) forbid routing requests through Pro/Max credentials "on behalf of their users".
- Codex on **public** repositories is blocked, with no override in this release (D5). [OpenAI docs](https://learn.chatgpt.com/docs/non-interactive-mode) say: "Do not use this workflow for public or open-source repositories."

## Consequences
- Startup capability probes and version pinning cover CLI flag drift.
- Exact quota numbers are unavailable. The capacity state is inferred from documented signals only.
