# Phase 0 — Responses to Codex review

Reviewer: Codex CLI 0.163.0 (ChatGPT sign-in, CLI default model), `codex exec -s read-only --ignore-rules`.
Author of the docs and these responses: Claude Code (Opus 5.5).
Raw outputs are stored verbatim next to this file. Earlier attempts failed and produced no review: `codex-phase0-r0-failed-gpt-5.5.events.jsonl` and `codex-phase0-r0b-failed-gpt-5.4.events.jsonl` (Codex 0.142.4: model not supported on ChatGPT plan).

## Round 1 — `codex-phase0-r1.json` — verdict `changes_required`, 18 findings (3 CRITICAL, 12 HIGH, 3 MEDIUM)

All 18 findings were checked against the documents and **accepted**. None were rejected.

| ID | Sev | Response | Change made |
|---|---|---|---|
| ATLAS-001 | CRITICAL | Accept. Evidence was bound to HEAD while the implementation was uncommitted | New invariant: immutable **candidate commits** created by ATLAS before testing. All evidence, approvals and merges are bound to `candidate_id`, and any change makes older evidence stale. workflow-state-machine.md §0, guards; github-integration.md §2 steps 4–7; database-design.md `candidates` |
| ATLAS-002 | HIGH | Accept | QA runs in a dedicated detached QA worktree at the candidate commit. Import validates against the candidate, not folder cleanliness. Destructive troubleshooting advice removed. antigravity-manual-handoff.md §2, §4, §8 |
| ATLAS-003 | CRITICAL | Accept. Untracked files would be lost | Snapshot via a temporary index (`add -A` → `write-tree` → `commit-tree` → `refs/atlas/snapshots/*`), verified before any reset/clean. Reset is refused if preservation fails. recovery-strategy.md §2.3 |
| ATLAS-004 | HIGH | Accept | `atlas-exec` launcher with a durable run file (launch handshake), discovery from DB + run files, and a quiesce protocol (INT→TERM→KILL plus confirmed group exit) **before** any git repair. Startup steps reordered. recovery-strategy.md §2.1–2.2, §3 |
| ATLAS-005 | CRITICAL | Accept. Env scrubbing is not isolation | Enforced test sandbox (Seatbelt profile with canary self-test, or a dedicated user). Fails closed. Agent read-scope verification and an outer-wrapper plan. Residual CLI trust made an explicit user decision (D9). security-model.md §5; risks R7, R16, R17; D11 |
| ATLAS-006 | HIGH | Accept | Codex template adds `--ignore-user-config` (help confirms auth still works) with explicit `-c` pins, plus hostile-config tests. agent-compatibility.md §3.2; security-model.md §3.2 |
| ATLAS-007 | HIGH | Accept | G-MERGE-PRE runs on every entry to MERGING **and** immediately before the merge op. Approvals are bound to candidate + policy_version + expiry. workflow-state-machine.md §3.1; database-design.md `approvals` |
| ATLAS-008 | HIGH | Accept | `MERGE_PENDING_REMOTE` state for queued or auto-merge-armed outcomes, with no resubmission. Merge-queue support ships off (the user merges and ATLAS verifies). github-integration.md §3; recovery-strategy.md §1 |
| ATLAS-009 | HIGH | Accept | G-MERGED requires the merged head == candidate, plus matching base and repo. A mismatch → `BLOCKED(external_merge_mismatch)` |
| ATLAS-010 | HIGH | Accept | `protection_status` known/user_declared/unknown. Unknown → ATLAS never merges. A missing required check counts as a failure. github-integration.md §1, §3 |
| ATLAS-011 | HIGH | Accept | Candidate revisions within a phase. One PR per phase with an `update_pr` op. Push/merge keys include the revision. Approvals are superseded on a new candidate |
| ATLAS-012 | HIGH | Accept | `projects.github_repo` UNIQUE (canonical lowercase). Lock keyed 1:1 with the repository |
| ATLAS-013 | MEDIUM | Accept | `blobs` (global, by hash) split from `artifact_refs` (project-owned). Authorisation goes via refs |
| ATLAS-014 | HIGH | Accept | `UNIQUE(id, project_id)` on parents and composite FKs on all children. Polymorphic refs replaced by explicit nullable FKs with CHECK |
| ATLAS-015 | HIGH | Accept | G-QA requires exactly-once coverage of criterion IDs, mandatory criteria `met`, and verdict/finding consistency. Blocking findings from any source are re-evaluated at G-CI |
| ATLAS-016 | MEDIUM | Accept. Confirmed in `evidence/claude-help.txt` | The schema is passed as serialized JSON text, not a path |
| ATLAS-017 | HIGH | Accept | Roadmap re-sequenced: repo locks + safe restart in Phase 1, test sandbox + minimum QA gate in Phase 4 (before GitHub delivery), plus a fail-closed rule for unbuilt capabilities |
| ATLAS-018 | MEDIUM | Accept | `ROLLBACK_IN_PROGRESS` → `ROLLED_BACK` terminal state linked to the revert phase. Task completion and dependents defined |

## Round 2 — `codex-phase0-r2.json` — verdict `changes_required`, 8 findings (2 CRITICAL, 5 HIGH, 1 MEDIUM)

Codex confirmed 14 of 18 round-1 fixes. It reopened 4 and raised 4 new defects introduced by the revisions. All 8 were checked and **accepted**.

| ID | Sev | Response | Change made |
|---|---|---|---|
| ATLAS-001 (reopened) | CRITICAL | Accept. Tests could mutate source in a writable worktree | Tests run in a fresh detached **verification worktree** at the candidate. Source-integrity check before and after; mutated runs are recorded `invalid_source_mutated` and never satisfy G-TEST. `test_runs.integrity` column. workflow-state-machine.md §0, G-TEST; security-model.md §5.1 |
| ATLAS-003 (reopened) | CRITICAL | Accept. Staged-only content and conflict stages would be lost | Byte copy of the real index + `ls-files -s` manifest + pinned index blobs + staged-tree ref + worktree ref + HEAD. Restore dry-run before any reset. recovery-strategy.md §2.3 |
| ATLAS-004 (reopened) | HIGH | Accept. An unacknowledged launcher could exec after supervisor death | Ack pipe: the launcher blocks until `GO <id> <generation>`. EOF (supervisor dead) → exit without exec. Crash matrix rows corrected. recovery-strategy.md §2.1, §5 |
| ATLAS-014 (reopened) | HIGH | Accept | Composite relationship FKs (plan↔task, execution↔phase↔candidate, test_run↔candidate↔execution, handoff↔candidate↔phase). Missing execution FKs added. Transactional validators and negative tests for nullable paths. database-design.md §3–4 |
| ATLAS-101 | HIGH | Accept. Accepting QA on modified source was wrong | Import now **rejects** QA results whose worktree source differs from the candidate. Troubleshooting updated. antigravity-manual-handoff.md §2, §4, §8 |
| ATLAS-102 | HIGH | Accept | The candidate is created first with `guard_status=held`. Approval binds to that `candidate_id` + policy_version. New transitions CANDIDATE_CREATED→AWAITING_APPROVAL→TESTING with kind-specific exits |
| ATLAS-103 | HIGH | Accept | The push intent records `mode` and `expected_remote_sha`. Recovery replays exactly the recorded push (incl. force-with-lease) only while the remote still matches |
| ATLAS-104 | MEDIUM | Accept. `-m1` fails on squash commits | `git revert <sha>` for single-parent (squash) commits. `-m 1` only after validating a multi-parent merge |

## Round 3 (final configured cycle) — `codex-phase0-r3.json` — verdict `changes_required`, 4 HIGH findings

Codex considers all other round-1 and round-2 findings resolved at the architecture level. The 4 below were accepted and fixed **after** round 3. **These fixes have not yet been independently re-verified by Codex**, because the 3-cycle limit is reached and the decision on further review is escalated to the user.

| ID | Sev | Response | Change made |
|---|---|---|---|
| ATLAS-008 (reopened) | HIGH | Accept. A queued or armed merge could complete after ATLAS authorisation lapsed | G-MERGE-PRE re-evaluated on every MERGE_PENDING_REMOTE poll. New `CANCELLING_REMOTE_MERGE` state + outbox `cancel_remote_merge` (`gh pr merge --disable-auto`; GraphQL `dequeuePullRequest`, to verify). No new merge intent until cancellation is confirmed. A merge that lands before cancellation is recorded truthfully and BLOCKED. Queue support stays off until these paths are tested |
| ATLAS-014 (reopened) | HIGH | Accept | `review_rounds` and `handoffs` reference executions via `(execution_id, candidate_id, project_id)`. Plan-review validator. Negative tests with fully populated mismatched references |
| ATLAS-201 | HIGH | Accept. One approval kind could clear all violations | `candidate_guard_violations` stores the full set. G-DIFF requires a matching approval **per violation kind**. Re-checked at G-MERGE-PRE |
| ATLAS-202 | HIGH | Accept. The profile denied the worktrees it needed | Verification worktrees live under `~/.atlas/sandboxes/<execution_id>/wt`. Explicit SBPL rule order. Read-only allowance limited to this worktree's git metadata. Self-test proves both access and isolation. Directory layout updated in architecture.md |

## Round 4 (verification-only, user-approved beyond the 3-cycle limit) — `codex-phase0-r4.json` — verdict `changes_required`, 1 HIGH

Codex verified that **ATLAS-014, ATLAS-201 and ATLAS-202 are resolved**, and found no new CRITICAL or HIGH defects in the round-3 fixes.

| ID | Sev | Response | Change made (after round 4; **not independently re-verified**) |
|---|---|---|---|
| ATLAS-008 (still open) | HIGH | Accept. Cancellation wasn't wired into restart or pause | `cancel_remote_merge` row added to the outbox recovery table. Startup step 10a withdraws every queued or armed request (and completes interrupted cancellations) **before** pausing phases or releasing locks. Pausing a phase in MERGE_PENDING_REMOTE first confirms the cancellation, so a paused phase never has a live remote merge request. Three new crash-matrix rows. Queue support stays off until these pass fault injection |

**Scope note:** this path only applies when GitHub holds a queued or armed merge. With the shipped defaults (`merge_queue_support: false`, merge called only when checks are already green, never `--auto`), that happens only in a race.

## Round 5 (verification of ATLAS-008 only, user-approved) — `codex-phase0-r5.json` — verdict **`approved`**

Codex: "ATLAS-008 is resolved at the documented architecture level … No open HIGH or CRITICAL finding remains within this verification scope. This verifies the specification, not implementation or test execution."

## Final status
- 30 findings in total across rounds 1–4 (counting reopened findings again). **All accepted, none rejected.**
- No open CRITICAL or HIGH findings, according to the independent reviewer.
- MEDIUM and LOW findings were all addressed in rounds 1–2 (ATLAS-013, -016, -018, -104).
- What was reviewed is **architecture documents only**. Every claim of the form "to verify in Phase N" is still unverified and is tracked in the roadmap.
