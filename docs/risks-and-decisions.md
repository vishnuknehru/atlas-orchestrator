# Risks, Mitigations and Decisions Requiring Approval

Status: Phase 0 draft · 2026-10-22

## 1. Technical and contractual risks

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | Provider terms change (e.g. Anthropic restricts `-p` on subscriptions; Google clarifies `agy` either way) | Medium | High | Terms are re-checked in each phase's challenge. The adapter `integration_mode` is a config switch, so moving a provider to `manual_handoff` needs no code change. Evidence URLs are recorded in agent-compatibility.md |
| R2 | Claude Pro limits exhausted by autonomous loops | High | Medium | One Claude call at a time. Read-only roles use tight prompts. Pause on `rate_limit`. Bounded review/repair cycles. No retry storms |
| R3 | Codex Plus 5-hour / weekly caps | High | Medium | Same as R2. Weekly-cap pause may last days; tasks stay PAUSED and resume |
| R4 | CLI flag/output drift (agy changed version *during* Phase 0) | High | Medium | Startup help-hash and required-flag probe. Fixture tests per version. Strict mode blocks untested versions |
| R5 | `claude -p` executes hostile repo hooks/MCP | Medium | Critical | `--setting-sources user`, `disableAllHooks` via flag, empty strict MCP config. Hostile-repo test in Phase 2 |
| R6 | Prompt injection steers agents to malicious edits | Medium | High | Policy outside prompts, structured outputs, independent cross-provider review of the real diff, diff guards on protected paths, tests executed by ATLAS |
| R7 | Test execution runs repo code that could read credentials, ATLAS state or other projects | Medium | Critical | Enforced test sandbox (Seatbelt profile with canary self-test, or a dedicated user). Fail closed without it (security-model.md §5.1) |
| R16 | `sandbox-exec` is deprecated by Apple and could be removed or changed | Low–Medium | High | The canary self-test on every start detects breakage. The dedicated-user mechanism is the fallback (D11) |
| R17 | Agent tool sandboxes may allow reads outside the worktree (cross-project exposure) | Medium | High | Phase 2 verification. An outer Seatbelt wrapper or dedicated user if confirmed (security-model.md §5.2) |
| R18 | Evidence drift: what was reviewed ≠ what is merged | — (designed out) | Critical | Immutable candidate commits, evidence bound to `candidate_id`, `--match-head-commit`, G-MERGED head check |
| R19 | LangGraph checkpointer vulnerabilities: SQLi and deserialization RCE history | Medium | High | Pinned patched versions, `pip-audit` gate, `StrictJsonSerializer`, no pickle/`Store`/`BaseCache`, `graph.db` 0600 and sandbox-denied (ADR-006 §6–7) |
| R20 | LangGraph replay re-runs nodes and duplicates side effects | Medium | High | Step-completion contract (ADR-006 §3) with crash-window tests |
| R23 | LangGraph speed benefit not realised | Medium | Medium | Slice 1.0 spike with pass/fail. Revert path through the `WorkflowEngine` interface |
| R21 | Graph checkpoint and app DB diverge after a crash or restore | Low | Medium | App DB is authoritative; startup divergence check → BLOCKED, never auto-repaired |
| R22 | LangGraph API churn (frequent releases) and transitive `langchain-core` | Medium | Low–Medium | Pin versions; `WorkflowEngine` interface as the exit path (ADR-006 §6); upgrades go through Renovate PRs with full gates |
| R8 | Broad `gho_` gh token (repo + workflow on all repos) | Medium | High | Per-project fine-grained PAT in Keychain (D3). gh never exposed to agents |
| R9 | Duplicate/partial GitHub ops after crash | Medium | High | Outbox + reconciliation + `--match-head-commit` + PR body marker. Crash-point test matrix |
| R10 | Agents return non-conforming output | High | Low | Schemas. A missing `structured_output` counts as failure. One retry with a "return valid JSON" prompt, then BLOCKED |
| R11 | Manual Antigravity QA becomes the bottleneck | High | Medium | Other projects continue. Packet expiry and reminders. Opt-in reassignment (D4) |
| R12 | Independence makes progress impossible when one provider is down | Medium | Medium | Pause, not substitute (by design). The user can approve explicit reassignment per task |
| R13 | SQLite corruption / disk full | Low | High | `synchronous=FULL`, backup before migrations plus a daily backup, `quick_check` on start, restore flow |
| R14 | Merged phase breaks production despite green CI | Medium | High | Feature flags and expand/contract in plans (the challenger checks for this). Post-merge base CI check. Revert-PR flow. ATLAS does not claim deployment health without a configured verifier |
| R15 | Scope: building all 10 phases is a large effort | High | Medium | Thin vertical slices. Each phase is useful on its own (e.g. Phase 1 + 2 already give an agent status board) |

## 2. Decisions requiring user approval

| ID | Decision | Recommendation | Status |
|---|---|---|---|
| D1 | Build vs. adopt (ADR-001) | User asked for "adopt if secured, well tested, fast". Orchestrator apps were evaluated and failed the security bar. **Build ATLAS on LangGraph** (engine) with ATLAS-owned gates/outbox/GitHub/dashboard | ✅ Approved 2026-10-22 |
| D12 | Encrypt LangGraph checkpoints (`EncryptedSerializer`, key in Keychain) | Optional. State holds only IDs/enums, so the benefit is small | **Needs decision** |
| D2 | Antigravity integration mode (ADR-003) | Manual handoff with detailed runbook | ✅ Approved 2026-10-22 |
| D3 | GitHub credential | Per-project fine-grained PAT in Keychain. Until then, the existing gh login, with the scope risk acknowledged | **Needs approval** |
| D4 | When Antigravity QA is pending for long | Wait (default). Optional per-task reassignment to the non-implementing headless agent | **Needs approval** |
| D5 | Codex on public repos | Blocked by default. Explicit per-project override, logged | **Needs approval** |
| D6 | Auto-merge | Off globally. Per-project opt-in only after Phase 6 is complete | **Needs approval** |
| D7 | Plan approval mode | `supervised` (human approves each plan) until the user opts into `autonomous` per project | **Needs approval** |
| D8 | ATLAS's own repo | Private GitHub repo `vishnuknehru/atlas-orchestrator`, PR per phase | ✅ Approved 2026-10-22 |
| D9 | Residual trust in agent CLIs | Provider CLI processes themselves run as you and can read your files. ATLAS cannot sandbox them without breaking their auth (security-model.md §5.2) | **Needs acknowledgement** |
| D11 | Test-sandbox mechanism | Seatbelt profile by default (no admin setup). Optional dedicated `_atlasrunner` user for stronger isolation (one-time admin setup) | **Needs approval** |
| D10 | Models | Pin explicit models per role in config, with no `--fallback-model`. Defaults are chosen in Phase 2 from `agy models`, `claude` and `codex` docs | **Needs approval in Phase 2** |
