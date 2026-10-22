# Responses to Codex review of the portability amendment (ADR-007)

## Round 1 (codex-portability-p1.json): changes_required

| ID | Sev | Response | Where |
|---|---|---|---|
| P01 | CRITICAL | **Accepted.** An env key would be inherited by commands the agent launches. HTTP agents get the key only as a header. A CLI agent may receive a key only if its manifest has `credential_isolation: verified`, proven by a fixture showing agent-launched commands don't inherit it; otherwise `BLOCKED(api_key_not_isolated)`. Proving this for claude_cli and codex_cli is a Phase 2 spike item | ADR-007 §5, security-model §3.3, agent-compatibility §3.2/§6.1, roadmap 2.0b |
| P02 | HIGH | **Accepted.** User-added (unverified) manifests are not loaded in this release. Only verified manifests from the ATLAS repo; the HTTP reviewer is the extension path | ADR-007 §3, security-model §2/§3.11, how-to-use §1.6 |
| P03 | HIGH | **Accepted.** Connect to the validated address, ignore proxy env, reject redirects, re-validate per request. Local CLI providers: endpoint checked before launch and pinned on argv; residual risk stated | ADR-007 §4, security-model §3.11 |
| P04 | HIGH | **Accepted.** Every loaded secret is registered with the redactor before use and redacted by exact value; tests cover non-pattern tokens | ADR-007 §5, security-model §3.3, roadmap 2.3 |
| P05 | MEDIUM | **Accepted.** agent_id, derived provider (anthropic/openai/google/local/remote host) and model_family are separate. Aliases never satisfy the provider check. The explicit exception: two `local` agents satisfy it, so independence rests on family | ADR-007 §2, ADR-005, workflow-state-machine G-IND |
| P06 | MEDIUM | **Accepted.** Partition or thick LV only; thin/sparse/loop backing rejected after inspecting the backing device | security-model §5.1.8, ADR-007 §6 |
| P07 | MEDIUM | **Accepted.** D5 left unchanged: all Codex agents are blocked on public repos whatever the billing. Per-billing Codex table: `--oss` never for subscription/api_key, required for local | agent-compatibility §3.2/§6.2, ADR-004, ADR-007 §3 |
| P08 | MEDIUM | **Partly accepted.** The cited "on behalf of their users" sentence is still on the page (retrieved 2026-10-22). Dated excerpts saved; ADR-004 now cites the explicit permission for end users signing in with their own subscription, the Commercial Terms conditions for hosted use, and the "ordinary, individual usage" note | evidence/claude-legal-compliance-2026-10-22.md, ADR-004 |
| P09 | MEDIUM | **Accepted.** Minimum QA gate is provider-neutral; Antigravity is one manual-handoff profile; D4 kept | roadmap Phase 4, architecture §9, ADR-007 |

## Round 2 (codex-portability-p2.json): changes_required; P01–P04, P06, P07 resolved

| ID | Sev | Response | Where |
|---|---|---|---|
| P05 | MEDIUM | **Accepted.** Derivation precedence: `local` only for billing local + validated loopback (local + remote endpoint = config error); remote endpoints need a canonical declared provider, never the hostname; undeclared = `unknown`, which matches every provider (fail closed) | ADR-007 §2, ADR-005 |
| P08 | MEDIUM | **Accepted.** ADR-004 assesses distributed self-hosted ATLAS against each product-integration condition, and records the open applicability question as R28, to be confirmed with Anthropic before any public release | ADR-004, risks R28 |
| P09 | MEDIUM | **Accepted.** how-to-use §5 defines a provider-neutral manual-handoff import contract (declared model → family → ADR-005); Gemini is scoped to the Antigravity profile | how-to-use §5 |
| P11 | MEDIUM | **Accepted.** New append-only `agent_revisions`; `executions` snapshot agent_revision/provider/model_family; `review_rounds` store immutable reviewer/author provider and family; past gates use snapshots | database-design.md, ADR-005 |
| P12 | MEDIUM | **Accepted.** Codex pinning policy is billing-specific: no provider override for subscription/api_key; exactly the configured local provider and endpoint for local; per-mode hostile-config fixtures | security-model §3.2 |

## Round 3 (codex-portability-p3.json): changes_required; P05, P08, P09, P12 resolved

| ID | Sev | Response | Where |
|---|---|---|---|
| P11 | MEDIUM | **Accepted.** `plans` snapshot author_provider, author_family and author_agent_revision at authoring, with authoring_execution_id as provenance. review_rounds copy author identity from plans or the authoring execution, never from the current registry | database-design.md |
| P21 | MEDIUM | **Accepted.** Rule 4: a manual_handoff agent has a configured canonical provider or `local` (a recorded user declaration). The result must declare model + provider; a mismatch is rejected; missing = `unknown` (fail closed). Example profiles and the Antigravity result schema updated | ADR-007 §2, how-to-use §1.6/§5, antigravity-manual-handoff.md |
