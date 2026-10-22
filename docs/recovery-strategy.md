# Recovery Strategy

Status: Phase 0, revision 2 (after Codex review round 1) · 2026-10-22

Principle: **"The process died" does not mean "the operation failed."** Before any retry, ATLAS reconciles against ground truth: the git repository, GitHub and the process table. **No destructive repair happens until all execution processes are confirmed gone and all work is preserved.**

## 1. Outbox pattern for external side effects

Every operation with an effect outside the DB goes through `outbox_ops`. These operations are:
- push;
- PR create, PR body update and PR close;
- branch update (`gh pr update-branch`);
- merge;
- branch delete;
- revert PR;
- CI rerun.

The local candidate commit is not an outbox op, because it is local and idempotent by tree SHA. It is still reconciled at startup (§3 step 7).

```
1. INTENT    (DB txn) insert outbox_op(status=pending, idempotency_key, intent incl. candidate_id + expected SHAs)
2. EXECUTE   (DB txn) status=in_flight, attempts+=1 → re-check the op's precondition guard (e.g. G-MERGE-PRE) → run git/gh
3. CONFIRM   read back remote/local state → (DB txn) status=confirmed, remote_ref=…, apply FSM transition
             — this txn IS the workflow step's completion txn (ADR-006 §3): result refs + outbox confirm +
               FSM transition + workflow_steps.status=completed commit together
   on error  classify → failed (retryable) | reconciling (unknown outcome) | abandoned (needs user)
```

On restart, ops with status `in_flight` or `reconciling` are **reconciled, never blindly re-executed**. Keys include the **candidate revision**. A phase has exactly one PR, but each revision has its own push and merge (ATLAS-011).

| Op | Idempotency key | Reconcile by asking | Already done | Not done |
|---|---|---|---|---|
| `push` | `<phase>:push:r<n>:<candidate_sha>` | `git ls-remote origin refs/heads/<branch>` | == candidate_sha → confirm | The intent records `mode` (`fast_forward` \| `force_with_lease`) and `expected_remote_sha` (remote value when the intent was written, or none). If the remote still == `expected_remote_sha`, replay **exactly** the recorded push: a plain push, or `--force-with-lease=refs/heads/<branch>:<expected_remote_sha>` for a rebased candidate. Any other remote value → `BLOCKED(remote_diverged)`. A force push is never derived at reconcile time; it is only replayed when it was recorded in the intent (ATLAS-103) |
| `create_pr` | `<phase>:pr` (one per phase) | `gh pr list --head <branch> --state all --json number,state,body,headRefOid`, then match `<!-- atlas:idempotency=<phase>:pr -->` | confirm number (never a second PR) | create |
| `update_pr` | `<phase>:prbody:r<n>` | PR body contains `atlas:revision=r<n>` | confirm | `gh pr edit --body-file` |
| `merge_pr` | `<phase>:merge:r<n>:<candidate_sha>` | `gh pr view N --json state,mergeCommit,headRefOid,autoMergeRequest` (+ merge-queue status, Phase 5 to verify field names) | `MERGED` → G-MERGED (head must equal candidate). Queued or auto-merge armed → `MERGE_PENDING_REMOTE` (no resubmit) | `OPEN`, not queued, head == candidate → re-run G-MERGE-PRE then retry with `--match-head-commit`. Otherwise → CI_VALIDATION |
| `cancel_remote_merge` | `<phase>:cancelmerge:r<n>` | `gh pr view N --json state,autoMergeRequest,mergeCommit,headRefOid` (+ queue membership, Phase 5 to verify) | not queued and `autoMergeRequest == null` → confirm → CI_VALIDATION (or PAUSED, see below). `MERGED` → G-MERGED + `BLOCKED(merged_after_authorisation_lapsed)` | still queued/armed → re-issue `gh pr merge N --disable-auto` / GraphQL dequeue. Bounded retries, then BLOCKED with a user alert ("a remote merge request could not be withdrawn") |
| `delete_branch` | `<phase>:delbranch` | ls-remote shows no branch | confirm | delete |
| `create_revert_pr` | `<revert_phase>:pr` | as `create_pr` | confirm | create |
| `rerun_ci` | `<phase>:rerun:<run_id>` | `gh run view <id> --json attempt` > recorded attempt | confirm | rerun once |

`gh pr merge --match-head-commit <sha>` (in `evidence/gh-pr-merge-help.txt`) means a retried merge can never merge a different head. The same help notes that on merge-queue branches `gh pr merge` *enqueues*, and that pending required checks may *arm auto-merge*. Both are therefore modelled as `MERGE_PENDING_REMOTE` (ATLAS-008).

## 2. Agent and subprocess executions

### 2.1 Durable launch handshake (ATLAS-004)
Every subprocess (agent, test, git, gh) is started through a small ATLAS launcher, `atlas-exec`. It is a Python entry point run as the direct child.

1. **(DB txn)** Insert an `executions` row with status `launching`, a fresh `execution_id`, and the current supervisor `instance_generation`.
2. Spawn `atlas-exec --execution-id <id> --generation <g> -- <argv…>` with `start_new_session=True` (new process group and session). An **ack pipe** is inherited: atlas-exec holds the read end, and the supervisor holds the only write end.
3. `atlas-exec` writes `~/.atlas/run/<execution_id>.json` atomically (temp + rename + fsync) with `{pid, pgid, start_time, argv_hash, generation}`. It then **blocks reading the ack pipe**.
4. The supervisor reads the run file, commits status `running` with pid, pgid and start_time, and writes `GO <execution_id> <generation>` to the pipe.
5. `atlas-exec` checks the token and only then execs the target. **If the pipe reaches EOF**, which happens when the supervisor died and the kernel closed its write end, or if the token mismatches, atlas-exec deletes its run file and exits **without ever exec-ing the target** (ATLAS-004 r2).

As a result, a launcher that loses its supervisor can never start work. Recovery treats `launching` rows as follows:
- If there is no run file and no live process, the row becomes `interrupted`.
- If a run file exists and its launcher is still alive (it is blocked on a pipe that has already hit EOF, so it will exit), recovery waits for its exit like any other process group (§2.2).

Recovery also discovers `running` rows and every run file in `~/.atlas/run/`. Liveness is checked with pid **plus** start time. **All `launching` and `running` executions are settled before any git repair** (§3 step 3).

### 2.2 Quiesce protocol (before any git repair or worktree reset)
For each live execution process group:
1. Send SIGINT to the group and wait up to 10 s.
2. Send SIGTERM to the group and wait up to 10 s.
3. Send SIGKILL to the group.
4. Confirm that no process with that pgid remains (`os.killpg(pgid, 0)` raises `ProcessLookupError`).

If a group still cannot be confirmed gone, the phase goes to `BLOCKED(process_not_terminated)` and **no** repair runs on its worktree. Orphans are never adopted, because their output pipes are lost. The execution is recorded as `interrupted`.

### 2.3 Preserving work before reset (ATLAS-003, revised in round 2)
Before *any* `reset` or `clean` of a worktree, ATLAS preserves **three** things: the working tree, the real index (including staged-only content and conflict stages), and HEAD.

```
# (a) raw index file — byte copy, preserves staged blobs AND conflict stages 1-3
cp "$(git -C <wt> rev-parse --git-path index)" ~/.atlas/snapshots/<execution_id>/index
git -C <wt> ls-files -s -z > ~/.atlas/snapshots/<execution_id>/index-stages.txt   # manifest for verification
#     staged blobs may be unreachable from any ref (gc could prune them); pin every index entry, all stages:
#     build a flat tree with one entry per index row, named s<stage>-<row>, via `git mktree`, then
git -C <wt> update-ref refs/atlas/snapshots/<execution_id>/index-blobs $(git -C <wt> commit-tree <mktree-sha> -m "atlas index blobs")
# (b) staged tree as a commit, when the index has no conflicts
staged=$(git -C <wt> write-tree) && git -C <wt> update-ref refs/atlas/snapshots/<execution_id>/staged \
        $(git -C <wt> commit-tree $staged -p HEAD -m "atlas staged snapshot")
# (c) working tree incl. untracked (not ignored), via a temporary index
GIT_INDEX_FILE=<tmp-index> git -C <wt> add -A
wtree=$(GIT_INDEX_FILE=<tmp-index> git -C <wt> write-tree)
git -C <wt> update-ref refs/atlas/snapshots/<execution_id>/worktree $(git -C <wt> commit-tree $wtree -p HEAD -m "atlas worktree snapshot")
# (d) HEAD
git -C <wt> rev-parse HEAD > ~/.atlas/snapshots/<execution_id>/HEAD
```

**Verification before reset:**
- The `ls-files -s` output of the copied index equals the manifest, and every listed blob exists (`git cat-file -e`).
- The worktree snapshot matches the working tree (`git diff --quiet <worktree-snap>` and the untracked list matches).
- A **restore dry-run**: in a scratch worktree, restore both the staged state (copied index) and the working state, and compare.

Only if all of these pass does ATLAS reset and `git clean -fd`, scoped to the worktree. Otherwise the reset is refused and the phase goes to `BLOCKED(preservation_failed)`.

Ignored files are not preserved, since they are build output by definition. The UI offers "restore snapshot", which restores both the index and the working tree. Snapshot refs and files are local only, never pushed, and retained for 30 days.

### 2.4 Resume decision
- Read-only roles (planner, reviewer, challenger, QA) are re-run from scratch. The cost is quota only, and retry counts apply.
- Write roles (developer, fixer): preserve (§2.3), reset to the last candidate, then re-run. Optional (Phase 6): resume the provider session with the snapshot diff as context. Any output still becomes a *new candidate* and goes through the full gates.
- A run counts as succeeded only when the exit code is 0, the final result was captured and parsed, and the process group is confirmed gone.

## 3. Startup sequence (order matters)

```
1.  Open DB, PRAGMA quick_check → on failure: no scheduler; offer restore from latest backup
2.  Acquire instance lock (flock on ~/.atlas/atlas.lock) → prevents two ATLAS processes
3.  Discover executions (DB launching/running + ~/.atlas/run/*.json); QUIESCE all live groups (§2.2)
4.  Probe agents (version/help hash/auth) → update agents table; nothing scheduled
5.  For each project: git fetch (if network); `git worktree prune`; list worktrees vs phases
6.  Repair interrupted git state in worktrees (only now that no writer is alive):
       stale index.lock → remove; in-progress merge/rebase/cherry-pick → preserve (§2.3) then abort
7.  Reconcile candidates: if a candidate commit exists on the branch but not in DB (crash after commit) → record it
       by tree SHA; if DB has a candidate whose commit is missing → BLOCKED(candidate_missing)
8.  Reconcile executions → interrupted; preserve worktrees with changes beyond the last candidate (§2.3)
9.  Reconcile outbox ops (§1)
10. Reconcile phases in PR_OPEN / CI_VALIDATION / MERGING / MERGE_PENDING_REMOTE / CANCELLING_REMOTE_MERGE against GitHub
       (a human may have merged/closed; apply G-MERGED strictly)
10a. WITHDRAW REMOTE MERGE REQUESTS (ATLAS-008 r4): for every PR found queued or with auto-merge armed — whatever the
       phase state — and for every in-flight cancel_remote_merge: run/complete cancel_remote_merge and confirm it,
       because step 12 is about to pause the phase and a paused phase must not have a live remote merge request.
       Not confirmable → BLOCKED(remote_merge_not_withdrawn) + prominent alert; its repo lock is NOT released.
11. (reserved: lock release moved to 12a)
11a. Resolve unfinished workflow steps (ADR-006 §5.1) BEFORE any overlay: succeeded execution / confirmed op →
       complete the step from its recorded result via its normal transition (no re-run); known failure → abandon
       with retry disposition (writers: preserve + reset first, §2.3); unknown outcome → BLOCKED(effect_outcome_unknown)
12. Mark every in-progress phase PAUSED(resume_to=<its state after 11a>) unless config `recovery.auto_resume: true`.
       Recovery records facts (e.g. "remote merge withdrawn") but never applies non-overlay transitions; on Resume the
       graph node consumes the fact (await_remote_merge → CI_VALIDATION, never MERGING)
12a. Release repo_locks held by dead instances only after their phase is PAUSED/BLOCKED/terminal and step 10a confirmed
13. LangGraph thread classification (ADR-006 §5.2) from checkpoint {entity_version, current_step_id, generation} and
       the app transition history since that version: consistent / compatible (own-step completion and recovery/user
       overlay transitions only) / incompatible → BLOCKED(engine_state_divergence) with rebuild_target;
       only exit is the audited user-triggered rebuild (ADR-006 §5.3)
14. Audit "recovery_completed"; UI banner; start scheduler. No graph thread is resumed until steps 1–13 are complete,
       and (until Phase 6) not until the user presses Resume
```

**LangGraph replay:** resuming a thread re-runs the interrupted node from its start. Nodes are replay-safe by construction (ADR-006 §3): they use a step key, check the app DB first, and send every external effect through the outbox. Graph checkpoints are therefore never trusted as evidence that an external effect happened. Only §1 reconciliation decides that.

The dashboard is read-only until step 13. Until Phase 6, `auto_resume` is forced to `false`: after any restart, everything waits for the user to press Resume (minimal safe restart, ATLAS-017).

## 4. Capacity handling

States: AVAILABLE, BUSY, RATE_LIMITED, QUOTA_EXHAUSTED, AUTHENTICATION_REQUIRED, PROVIDER_UNAVAILABLE, UNKNOWN, MANUAL_ONLY.

| Signal | New state | Behaviour |
|---|---|---|
| Claude `api_retry` `error=rate_limit` while the CLI retries | RATE_LIMITED (informational) | No parallel calls. Let the execution timeout bound the CLI's own retries |
| Run ends with a rate/usage-limit failure | RATE_LIMITED or QUOTA_EXHAUSTED | Stop assigning work to this provider. Mark the execution `failed(capacity)`; it is not counted against the repair budget. Phase → PAUSED(resume_to) |
| Auth failure / `codex login status` not logged in | AUTHENTICATION_REQUIRED | Pause the provider. The UI tells the user to sign in themselves. Probe-only until fixed |
| Codex `turn.failed` "model … not supported when using Codex with a ChatGPT account" | PROVIDER_UNAVAILABLE(model_not_entitled) | No retry and no model substitution. The user picks a model in config |
| Overloaded / 5xx / network | PROVIDER_UNAVAILABLE | Backoff 1, 2, 4, 8, 16, 30 min (cap), ±20% jitter, max 8 → then UNKNOWN + notice |
| Unrecognised failure | UNKNOWN | Backoff starting at 15 min, cap 2 h |

- **Probe, don't hammer.** Re-availability is checked with the cheapest supported probe. A real task is retried only after a probe passes and the backoff has elapsed. At most one probe per provider per interval.
- **No fabricated quotas.** The UI shows state, last signal and timestamp only.
- **No substitution by default.** When `allow_agent_substitution: true`, G-IND still applies and the substitution is shown in the UI.
- **Whole-system outage.** The dashboard, history, config, handoff import, GitHub polling and recovery all keep working without agents.

## 5. Crash-point matrix (fault-injection tests)

The harness injects `os._exit(1)` at named points (`ATLAS_FAULT=<point>`), restarts ATLAS against a local bare repo and a fake gh, and asserts the outcome.

| Crash point | Expected after restart |
|---|---|
| supervisor dies after spawn, before the launcher writes its run file | Launcher sees EOF on the ack pipe and exits without exec. The row becomes `interrupted`. The test asserts the target never ran (marker file absent) |
| supervisor dies after the run file, before ack | Same: no exec. Run file removed or settled by recovery |
| supervisor dies after ack (target running) | Process discovered via run file and quiesced before git repair |
| developer worktree with staged-only changes ≠ working tree, plus a merge conflict | Copied index and stages manifest restore exactly, verified by dry-run before reset |
| developer agent mid-run with new untracked files | Group killed and confirmed gone. Snapshot ref contains the untracked files. Worktree reset to the last candidate |
| after candidate commit, before DB record | Candidate recovered by tree SHA. No duplicate commit |
| after INTENT, before EXECUTE (any op) | Op executes once |
| push done, before CONFIRM | Detected via ls-remote. No re-push |
| create_pr done, before CONFIRM | PR found by marker. Exactly 1 PR |
| merge done (or queued), before CONFIRM | MERGED (head verified) or MERGE_PENDING_REMOTE. No second merge call |
| merge errored but GitHub merged | As above |
| crash while MERGE_PENDING_REMOTE (queued/armed), approval expires before restart | Startup step 10a withdraws the request and confirms it, recording the fact. The phase becomes PAUSED(resume_to=MERGE_PENDING_REMOTE); on Resume the await node consumes "withdrawn" → CI_VALIDATION. No merge after the authorisation lapsed |
| crash during `cancel_remote_merge` | Completed on restart before pausing. If GitHub already merged → BLOCKED(merged_after_authorisation_lapsed) |
| user pauses a phase in MERGE_PENDING_REMOTE | Cancellation confirmed before PAUSED is entered |
| human pushed a new head then merged while ATLAS was down | BLOCKED(external_merge_mismatch) |
| during FSM transition txn | Old or new state, never both. Transition row present iff state changed |
| during handoff import | Idempotent by nonce |
| DB corruption (simulated) | Scheduler refuses to start. Restore offered |

## 6. Rollback
- **Before merge:** the phase branch is abandoned but kept, and main is untouched. Dependents → BLOCKED.
- **After merge:** a `revert` phase runs the full pipeline on a new branch. ATLAS inspects the parent count of the delivered commit first. A squash merge, which is the default, has one parent, so the revert is `git revert --no-edit <merge_sha>`. Only a validated multi-parent merge commit uses `-m 1`, after confirming parent 1 is the base branch (ATLAS-104). The original phase becomes ROLLED_BACK when the revert completes. ATLAS does **not** claim that DB migrations or external side effects were reversed. The phase's `rollback_strategy` and any manual steps are listed as outstanding.
