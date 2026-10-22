# Git and GitHub Integration

Status: Phase 0, revision 2 (after Codex review round 1) · 2026-10-22 · Tools: git 2.50.1, gh 2.97.0 (`evidence/versions.txt`)

## 1. Repository registration
1. The user enters `owner/name`, which is canonicalised to lowercase. **A repository can be registered only once** (`UNIQUE(github_repo)`), so all locks and merges for one repo share one identity (ATLAS-012).
2. ATLAS checks access with `gh repo view owner/name --json nameWithOwner,visibility,defaultBranchRef,viewerPermission,isArchived`. It records visibility; a public repo blocks Codex by default. PRs need WRITE.
3. **Protection discovery** produces `protection_status ∈ {known, unknown}` (ATLAS-010):
   - `known` when `gh api repos/{o}/{r}/rules/branches/{base}` and/or `.../branches/{base}/protection` succeed. ATLAS records the required check names (with app/source where available), the required review count, code-owner review, and merge-queue presence.
   - `unknown` when neither can be read, which is common without admin rights on classic protection. The user may then supply a **trusted policy** in ATLAS project config (`ci.required_checks: [...]`, `ci.required_approvals: n`). This counts as `known (user-declared)`.
   - If still unknown, ATLAS **never merges**. It opens the PR, reports CI status, and waits for the user to merge in GitHub. ATLAS then verifies the merged head (G-MERGED).
   - Re-checked before each merge. A change in protection invalidates prior merge approvals, because the policy version changes.
4. **Merge queue:** if the base branch uses a merge queue, ATLAS records `merge_queue: true`. Its merge op then expects a *queued* outcome (see §3).
5. Clone: `git clone --filter=blob:none <url> ~/.atlas/projects/<id>/repo`. The primary clone is never edited.

## 2. Per-phase flow

All commands are argv lists, never shell strings.

| # | Step | Command | Notes |
|---|---|---|---|
| 1 | Refresh base | `git -C repo fetch origin <base> --prune` | `base_sha = origin/<base>` |
| 2 | Worktree + branch | `git -C repo worktree add -b atlas/<slug> <wt> <base_sha>` | Branch name generated and regex-validated by ATLAS |
| 3 | Implement | developer agent, cwd=`<wt>` | The agent must not commit. Any agent-created commit is detected (HEAD moved) and folded into the candidate via `reset --soft`, after snapshotting |
| 4 | **Candidate commit** | diff guards on `git add -A` index, then `git -C wt -c core.hooksPath=/dev/null -c user.name=… -c user.email=… commit -m "<phase title> (r<n>)" --trailer "Atlas-Phase: <id>" --trailer "Atlas-Candidate: r<n>"` | Local only. Creates `candidate(r<n>, commit_sha, tree_sha)`. ATLAS then verifies that `git status --porcelain` is empty |
| 5 | Test | project test commands in hardened Docker containers on the Colima `atlas` VM, with the candidate tree (built from git objects) mounted **read-only** (security-model.md §5.1) | `executed` evidence bound to the candidate, valid only if the pre/post in-VM manifest attestation equals the candidate manifest (workflow-state-machine.md §0) |
| 6 | Review / QA | agents on `git diff <base_sha>..<candidate_sha>` | Evidence bound to the candidate |
| 7 | Fix loop | developer edits → new candidate commit `r<n+1>` on top | Older evidence goes stale. The branch accumulates revision commits; squash merge collapses them |
| 8 | Push (outbox) | `git -C wt push origin <candidate_sha>:refs/heads/atlas/<slug>` | Normally fast-forward. A rebase (conflict repair) produces a new candidate whose outbox intent records `mode=force_with_lease` and `expected_remote_sha`, and is pushed with `--force-with-lease=refs/heads/atlas/<slug>:<expected_remote_sha>`. Only on ATLAS's own branch, and logged. Recovery replays exactly the recorded mode (recovery-strategy.md §1) |
| 9 | PR (outbox, once per phase) | `gh pr create --repo o/r --base <base> --head atlas/<slug> --title <generated> --body-file <file>` | The body has a hidden `<!-- atlas:idempotency=<phase>:pr -->` marker and `atlas:revision=r<n>`. Later revisions update the same PR (`gh pr edit --body-file`) |
| 10 | CI | poll `gh pr checks <n> --json …` + `gh pr view <n> --json headRefOid,mergeable,mergeStateStatus,reviewDecision,statusCheckRollup,autoMergeRequest` | 30 s for the first 10 min, then 2 min. Results are accepted only when `headRefOid == candidate_sha` |
| 11 | Approvals | GitHub `reviewDecision` per known policy | AI reviews never count as GitHub approvals, and ATLAS never submits GitHub reviews |
| 12 | Merge (outbox) | `gh pr merge <n> --squash --match-head-commit <candidate_sha>` | Only after the user approves the merge in ATLAS (D6) **and** G-MERGE-PRE passes again right before execution. Never `--admin`, never `--auto` |
| 13 | Confirm | `gh pr view <n> --json state,mergeCommit,headRefOid,baseRefName` | G-MERGED: merged head == candidate, base and repo match. A queued merge → `MERGE_PENDING_REMOTE` |
| 14 | Update local | `git -C repo fetch origin <base>` and verify `mergeCommit` is reachable | |
| 15 | Cleanup | `git -C repo worktree remove <wt>`; delete the remote branch (outbox); keep the local branch and snapshot refs for 7 days | |
| 16 | Checkpoint | → POST_MERGE_VERIFICATION → COMPLETED | |

Flags confirmed in `evidence/gh-pr-merge-help.txt`: `--match-head-commit`, `--squash`, `--admin`, `--auto`. The JSON fields used above (`autoMergeRequest`, `statusCheckRollup`, merge-queue fields) and the rules API response shape will be confirmed against real `gh` output in Phase 5 and stored as fixtures.

## 3. Merge eligibility (G-CI / G-MERGE-PRE)

Merging is allowed only if **all** of the following hold:
- PR `state == OPEN`, `isDraft == false`, `headRefOid == current candidate commit SHA`, and the base equals the phase base branch.
- `protection_status` is known (§1.3).
- Every required check from the known policy is **present** and `SUCCESS` on that SHA. A missing required check counts as a failure. Extra non-required checks must not be failing.
- The known review requirement is satisfied: `reviewDecision == APPROVED` when reviews are required.
- `mergeable == MERGEABLE`, and `mergeStateStatus` ∈ {`CLEAN`, `HAS_HOOKS`}.
- ATLAS gates G-TEST, G-REV and G-QA hold for the same candidate, and no blocking finding from any source is open.
- Merge authorisation: a user approval bound to this candidate + policy version and not expired. `auto_merge: true` is **rejected by the config schema in this release** (D6, ATLAS-D11).
- The repo lock is held and no other ATLAS merge op is in flight for this repository.

**Merge queues and implicit auto-merge (ATLAS-008).** `gh` enqueues on merge-queue branches, and may arm auto-merge when checks are pending. ATLAS handles this as follows:
- It only calls merge when the checks are already green.
- If the result is "queued" or `autoMergeRequest != null`, ATLAS moves to `MERGE_PENDING_REMOTE` and polls. It does not resubmit.
- If the PR gets dequeued, auto-merge is disabled, or the head changes, ATLAS returns to CI_VALIDATION.
- Merge-group CI failures go to FIXING or BLOCKED.

**Authorisation while pending (ATLAS-008 r3).** A queued or armed merge is merely *authorised to proceed*. While in `MERGE_PENDING_REMOTE`, ATLAS re-evaluates G-MERGE-PRE on every poll. If the authorisation lapses, ATLAS moves to `CANCELLING_REMOTE_MERGE` and issues an outbox `cancel_remote_merge` op. Lapses include: the user's merge approval expired or was revoked, policy version changed, new blocking finding, or protection changed. This handles GitHub-side queued or implicitly armed merges. ATLAS's own auto-merge doesn't exist in this release (D6).
- An armed auto-merge is cancelled with `gh pr merge <n> --disable-auto` (flag present in `evidence/gh-pr-merge-help.txt`).
- A merge-queue entry is cancelled with the GraphQL `dequeuePullRequest` mutation via `gh api graphql`. *To verify in Phase 5.*

The op is reconciled by re-reading `autoMergeRequest` and queue state. No new merge intent for the phase can be created until the cancellation is confirmed. If GitHub merged before the cancellation took effect, the merge is recorded truthfully (G-MERGED) and the phase is BLOCKED for user review. It is not reported as gated delivery.

Phase 5 ships merge-queue support behind `merge_queue_support: false`. It can be enabled only after the cancellation paths above, including the restart and pause scenarios in recovery-strategy.md §3 step 10a and §5, pass the fake-gh fault-injection suite. Until it is enabled, merge-queue repos are treated like unknown protection: the user merges and ATLAS verifies.

`BEHIND` or `DIRTY`: rebase in the worktree (preserve first). That produces a **new candidate**, which re-runs tests, review and QA. For a conflict-free rebase, the user may waive re-review only (not tests or CI), with the waiver bound to the new candidate.

## 4. Merge conflicts
- Rebase onto the new base. If it is clean, a new candidate goes through full gates (except an optional review waiver as above).
- If there are conflicts: abort, then the developer agent gets a "resolve conflicts" task. The result is a new candidate with full gates. Exhausting the budget leads to `BLOCKED(merge_conflict)`.

## 5. CI failures
- `gh run view <id> --log-failed` output is captured as an untrusted artefact. The failure is classified as `phase_caused`, `flaky_or_infra` or `unknown`.
  - `flaky_or_infra`: rerun once (outbox `rerun_ci`).
  - `phase_caused`: go to FIXING, which counts toward the budget.
  - otherwise: BLOCKED.

## 6. Things ATLAS never does
- It never pushes to the base branch, force-pushes protected branches, or uses `--admin` or `--auto`.
- It never edits branch protection or rulesets, or dismisses reviews.
- It never treats an AI review as a GitHub approval, or merges with unknown protection.
- It never treats a merged PR whose head ≠ candidate as gated delivery.

## 7. Credentials
- **D3:** each project uses the user's PAT, which ATLAS reads from the OS secret store — macOS Keychain or Linux Secret Service (`atlas.<project_id>.github`, stored by the user; ADR-007 §6). Before it is ever used, ATLAS checks it with the read-only permission probe (`verify-github-pat` logic). **There is no fallback** to the broad `gh` keyring login (`gho_`, scopes `repo`/`workflow`/`read:org`/`gist`). A missing or under-privileged PAT gives `BLOCKED(github_credential)` (ATLAS-D12).
- The PAT is injected as `GH_TOKEN` only into gh/git subprocesses started by the GitHub service. Git push uses `gh auth git-credential`, so tokens never land in git config or URLs.
- These subprocesses run outside the test sandbox. Tests and agents never receive the token (security-model.md §3.3).

## 8. Testing strategy
- **Unit:** the policy engine against recorded `gh --json` fixtures, covering:
  - green, pending, missing-required and failed checks;
  - BEHIND, DIRTY, unknown protection, queued, auto-merge armed, review required;
  - head mismatch and approval staleness.
- **Integration (offline):** a local bare repo as origin plus a **fake `gh`** on `PATH`. The fake is a Python script with a JSON state file that implements the used subset of `pr create/list/view/edit/checks/merge` and `run view/rerun`. It supports faults: fail after side effect, delay, queue, external head push.
- **Live smoke (opt-in, Phase 10):** a throwaway private sandbox repo. It never runs against real project repos.
