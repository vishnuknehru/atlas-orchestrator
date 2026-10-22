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
| R7 | Test execution runs repo code that could read credentials, ATLAS state or other projects | Medium | Critical | Docker on a Colima VM with **no host mounts**. Source read-only. Contract checked before start; canary and attestation on every execution; fail closed (security-model.md §5.1) |
| R16 | Container/runtime escape into the ATLAS Colima VM | Low | High | The VM has no host mounts. Volumes are per execution and deleted after the run. The host is reachable only via the VM gateway, where only the token-protected localhost dashboard listens. Accepted residual (security-model.md §5.1.6) |
| R25 | Agents reach the Docker socket directly and bypass the container contract | Medium | High | Outer Seatbelt wrapper around agent processes denies the socket. Phase 2 spike test. Shell-capable agent roles are BLOCKED if the wrapper can't be applied (security-model.md §5.1.9) |
| R24 | Projects needing macOS toolchains cannot be tested in Linux containers | Medium (project-dependent) | Medium | Fail closed (`BLOCKED(no_test_sandbox)`). The Seatbelt fallback stays on file and needs a separate user decision per project |
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
| R11 | Manual Antigravity QA becomes the bottleneck | High | Medium | Other projects continue. Packet expiry and reminders. No reassignment (D4 = wait) |
| R12 | Independence makes progress impossible when one provider is down | Medium | Medium | Pause, not substitute (by design). Mandatory reviews wait for the provider to return |
| R13 | SQLite corruption / disk full | Low | High | `synchronous=FULL`, backup before migrations plus a daily backup, `quick_check` on start, restore flow |
| R14 | Merged phase breaks production despite green CI | Medium | High | Feature flags and expand/contract in plans (the challenger checks for this). Post-merge base CI check. Revert-PR flow. ATLAS does not claim deployment health without a configured verifier |
| R15 | Scope: building all 10 phases is a large effort | High | Medium | Thin vertical slices. Each phase is useful on its own (e.g. Phase 1 + 2 already give an agent status board) |

## 2. Decisions requiring user approval

| ID | Decision | Recommendation | Status |
|---|---|---|---|
| D1 | Build vs. adopt (ADR-001) | User asked for "adopt if secured, well tested, fast". Orchestrator apps were evaluated and failed the security bar. **Build ATLAS on LangGraph** (engine) with ATLAS-owned gates/outbox/GitHub/dashboard | ✅ Approved 2026-10-22 |
| D2 | Antigravity integration mode (ADR-003) | Manual handoff with detailed runbook | ✅ Approved 2026-10-22 |
| D3 | GitHub credential | Use the user's existing PAT. **Verification pending:** the user runs the provided script (`verify-github-pat.sh`), which never prints the token, and shares the output. Required: Contents RW, Pull requests RW, Actions R, Checks R, Commit statuses R, Metadata R, Administration R (protection). Not allowed: Workflows W, Administration W, Secrets | ⏳ Decided 2026-10-22; permissions not yet verified |
| D4 | While Antigravity QA is pending | **(a) Wait.** The phase stays AWAITING_MANUAL, other projects continue, reminders only. No reassignment of any kind, automatic or manual. Changing that would need a new decision | ✅ Approved 2026-10-22 |
| D5 | Codex on public repos | **Blocked by default.** The user currently works only on private personal projects. Revisit if a public repo is ever registered | ✅ Approved 2026-10-22 |
| D6 | Auto-merge | **Off.** Every merge needs the user's approval in ATLAS, plus GitHub's own requirements | ✅ Approved 2026-10-22 (revisit later) |
| D7 | Plan approval mode | **Supervised.** The user approves every plan before implementation | ✅ Approved 2026-10-22 |
| D8 | ATLAS's own repo | Private GitHub repo `vishnuknehru/atlas-orchestrator`, PR per phase | ✅ Approved 2026-10-22 |
| D9 | Residual trust in agent CLIs | Provider CLI processes run as the user and can read the user's files. ATLAS cannot sandbox them without breaking their auth (security-model.md §5.2) | ✅ Acknowledged 2026-10-22 |
| D10 | Models | Pin explicit models per role in config, with no `--fallback-model`. Defaults are chosen in Phase 2 from `agy models`, `claude` and `codex` docs | **Needs approval in Phase 2** |
| D11 | Test-sandbox mechanism | **Docker containers on Colima** (security-model.md §5.1). One-time user setup: a dedicated APFS volume with reserve = quota for the Colima `atlas` VM, plus `colima start --profile atlas` with no host mounts. The Seatbelt and dedicated-user options are kept on file, not enabled | ✅ Approved 2026-10-22 |
| D12 | Encrypt LangGraph checkpoints | **No.** `graph.db` holds only IDs, states and counters; FileVault is on (verified 2026-10-22). Revisit if checkpoint contents grow | ✅ Approved 2026-10-22 |
