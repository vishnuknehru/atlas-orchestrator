# How to use ATLAS (the LangGraph-based orchestrator)

> **Status: planned usage, not yet available.** ATLAS is at the end of Phase 0 (design only). This guide describes how ATLAS will be used according to the approved design. Each step is tagged with the roadmap phase that delivers it, e.g. **[P1]**. Commands and screens may change slightly during implementation, and this guide is updated in the same PR as any such change. Nothing here works until its phase is merged.

LangGraph is ATLAS's **internal** workflow engine (ADR-001, ADR-006). You never use LangGraph directly; there's nothing to install, configure or pay for. You interact with ATLAS through its local dashboard and a small CLI.

---

## 1. One-time setup

### 1.1 Tools (already present on this Mac, verified 2026-10-22)
| Tool | Why | Check |
|---|---|---|
| Claude Code (`claude`) | Architect, reviewer | `claude --version`; signed in with your Claude subscription |
| OpenAI Codex (`codex`) | Developer, challenger | `codex login status` → "Logged in using ChatGPT" |
| Antigravity (`agy` / IDE) | QA, **run by you** (manual handoff) | `agy models` |
| `git`, `gh` | Repository and GitHub operations | `gh auth status` |
| Python 3.12, `uv`, Node 24 | ATLAS itself | `python3.12 --version` |
| Colima + Docker CLI | Test sandbox (D11) | `colima version` |

ATLAS never signs in to these agent tools for you, and never reads their credentials (Claude, Codex, Antigravity). The **only** credential ATLAS reads is the per-project GitHub PAT you store for it (§1.3), and it passes that only to its own `git`/`gh` processes. If one of them logs out, ATLAS pauses that agent and tells you which command to run.

### 1.2 Install and start ATLAS **[P1]**
```bash
git clone https://github.com/vishnuknehru/atlas-orchestrator.git
cd atlas-orchestrator && uv sync          # exact install steps finalised in Phase 1 / Phase 10
atlas serve                               # starts ATLAS on 127.0.0.1 and opens the dashboard
```
- The dashboard runs only on your machine (`127.0.0.1`), protected by a per-install session token.
- Data lives in `~/.atlas/` (databases, logs, artefacts). Phase 10 adds an optional `launchd` service so ATLAS starts at login.

### 1.3 GitHub token per project (D3) **[P1 checks, P5 use]**
Store your PAT in the macOS Keychain under ATLAS's name for the project. The `-w` with no value makes `security` prompt for it without echoing:
```bash
security add-generic-password -s "atlas.<project_id>.github" -a "<your-github-user>" -w
```
- Required permissions on that repository:
  - **Contents:** read/write
  - **Pull requests:** read/write
  - **Actions:** read
  - **Checks:** read
  - **Commit statuses:** read
  - **Metadata:** read
  - **Administration:** read
- **Do not grant:** Workflows write, Administration write, Secrets.
- ATLAS probes the token before first use and refuses to fall back to your `gh` login. A missing or weak token gives `BLOCKED(github_credential)`.
- You can check a token yourself at any time with the verification script (it never prints the token).

### 1.4 Test sandbox (D11) **[P4]**
Tests run only inside Docker containers on a dedicated Colima VM. One-time steps (exact flags confirmed in Phase 4):
1. **Reserve disk space** with a dedicated APFS volume (admin rights needed; find your container ID with `diskutil apfs list`):
   ```bash
   sudo diskutil apfs addVolume <container> APFS AtlasSandbox -reserve 80g -quota 80g
   ```
2. **Start the ATLAS VM** with its data on that volume and **no host folders shared**. ATLAS shows the exact command in the dashboard (Settings → Test sandbox), and refuses to run tests if the VM can see any host folder.
3. **Approve test images.** For each project, choose a pinned image, e.g. `python:3.12@sha256:…`. ATLAS lists the digest; you approve it once.

If the sandbox isn't ready, projects show `BLOCKED(no_test_sandbox)` with the reason. Nothing runs unsandboxed.

---

## 1.5 What projects can be tested in this release **[P4]**
Check these before registering a project:
- **Linux toolchains only.** Projects that need macOS toolchains (Xcode, Swift for iOS/macOS, macOS-only binaries) can't be tested; testing is blocked.
- **Tests must not write to tracked files.** Tests may write only to declared `writable_paths` and `/tmp`.
- **No network during tests.** Dependency *installation* may use approved registries through ATLAS's allowlist proxy. Tests themselves always run with no network, so integration tests that call external services aren't supported yet.
- **No Git submodules, no Git LFS**, and no symlinks that point outside the repo.
- **No repository Dockerfile builds.** Use an approved, digest-pinned public image.
- **No fallback to running tests on your Mac.** If a project doesn't fit these rules, its testing is blocked (fail closed).

## 2. Register a project **[P1 record; P5 protection discovery; P4 test config]**
Dashboard → **Projects → Register repository**:
1. Enter `owner/name`, e.g. `vishnuknehru/my-app`. ATLAS checks access, visibility and default branch.
   - Public repositories: Codex is blocked (D5), and the default roles use Codex. A public project is therefore **blocked** until you make a new decision about a valid independent role assignment (ADR-005). You currently work only on private repos.
   - A repository can be registered only once.
2. **[P5]** ATLAS reads branch protection. If it can't, it asks you to declare the required CI checks and approvals; otherwise ATLAS will never merge on that project, and you merge on GitHub yourself (§4).
3. Review the **project config**. ATLAS proposes it from what it detects, and you approve it; anything the repository itself suggests is treated as untrusted until you accept it. The file is stored at `~/.atlas/projects/<id>/project.yaml`:

```yaml
# Only keys defined in the approved design are shown; unknown keys are rejected.
project:
  name: my-app
  repository: vishnuknehru/my-app
  base_branch: main
agents:                      # defaults; reviewer must differ from the author (ADR-005)
  architect: claude
  challenger: codex
  developer: codex
  reviewer: claude
  qa: antigravity_manual     # you run Antigravity yourself (ADR-003)
tests:                       # [P4]
  writable_paths: [".venv", ".pytest_cache"]   # approved key (security-model.md §5.1.3); must not overlap tracked files
ci:
  required_checks: []        # only needed if ATLAS can't read branch protection
```

**Test setup is not yet a defined config schema.** The design specifies the *behaviour*, but the exact key names are decided in Phase 4 and will be added to this guide then:
- an approved, digest-pinned image;
- the test commands ATLAS runs;
- an optional dependency-install step that may use only approved registries through the allowlist proxy;
- no network during tests.

Fixed in this release, and rejected if you try to change them:
- `auto_merge: false` (D6);
- `execution.mode: supervised` (D7);
- no paid API fallback;
- no unsupported integrations.

---

## 3. Submit work **[P1 record, P3 planning]**
Dashboard → project → **New task**:
- **Request:** e.g. "Add Google OAuth login".
- **Category:** feature, bug fix, performance, refactor, security, dependencies, UI, tests, investigation or modernisation.
- **Priority** and **acceptance criteria** (each gets an ID that QA must answer).
- **Start.**

What ATLAS does next, all visible on the task's **Timeline**:
1. **Repository analysis:** stack, tests, CI and conventions. Read-only, by Claude.
2. **Phased plan:** small, independently mergeable phases. Each has an objective, criteria, affected files, tests, risk, rollback and dependencies.
3. **Plan challenge:** Codex attacks the plan, Claude answers every finding, and this repeats up to 3 cycles.
4. **Your approval (D7):** you review the plan, the findings and the answers, then **Approve** or **Request changes**. Nothing is implemented before you approve.

---

## 4. Each phase, step by step **[P2–P5]**
The task screen shows the current phase and its state. For each phase:

| Step | Who | What you see | What you do |
|---|---|---|---|
| Prepare | ATLAS | New branch `atlas/…` in an isolated worktree | — |
| Implement | Codex | Live log; then a **candidate commit** (`r1`) | — |
| Protected-path check | ATLAS | If the change touches CI workflows, secrets, agent config or destructive DB migrations: **approval request** | Approve or reject each violation kind |
| Test | ATLAS (Docker) | Tests **executed** on the candidate, with results | — |
| Code review | Claude | Findings with severity, file, line, evidence | Read. CRITICAL and HIGH block until fixed and re-verified |
| Fix loop | Codex → Claude | New candidate `r2`… Old evidence is marked stale | — (stops after 3 attempts → BLOCKED) |
| QA | **You + Antigravity** | ⏸ **AWAITING_MANUAL** + a handoff packet | See §5 |
| Push + PR | ATLAS | PR link | Optional: review on GitHub |
| CI | GitHub | Required checks on the exact candidate commit | — |
| **Merge** | **You (D6)** | **Approve merge** button, with the evidence summary. GitHub's own required approvals still apply; an AI review never counts | Approve, or leave it |
| Merge + verify | ATLAS | Merge confirmed against the candidate commit | — |
| Post-merge check | ATLAS | Base-branch CI after the merge | — |
| Rollback decision (only if base CI is red) | **You** | **Rollback proposed** with the failing checks | Approve a revert, or decline (recorded) |
| Next phase | ATLAS | Starts when its dependencies are complete | — |

**When you merge on GitHub yourself:** in some repos ATLAS doesn't merge; it shows "merge on GitHub" and waits:
- repos whose branch protection ATLAS can't read and you haven't declared;
- repos that use a **merge queue** (merge-queue support is off in this release).

You merge the PR on GitHub. Squash merge is fine: it creates a new commit, which is expected. ATLAS then checks that the PR's **head at merge time** was exactly the reviewed candidate commit, in the expected repository and base branch. It also records the resulting merge commit and confirms it is on the base branch. If the head was different, the phase is BLOCKED for your review.

**If you approve a rollback:** ATLAS creates a *revert phase* that goes through the full pipeline (tests, review, QA, your merge approval). When that phase completes, the original phase is marked **rolled back**. Phases that depend on it are blocked until you re-plan or cancel them. A revert doesn't undo database migrations or other external side effects; the phase's rollback notes list any manual steps.

Evidence labels are shown everywhere:
- **executed:** ATLAS ran it;
- **remote-verified:** read back from GitHub;
- **agent-reported:** an agent said so;
- **manual-imported:** from your Antigravity run;
- **proposed:** suggested but not run.

Only *executed* and *remote-verified* evidence count for tests, CI and merge.

---

## 5. The Antigravity QA step (manual, D4 = wait) **[P4 minimal, P7 full]**
Full runbook: [antigravity-manual-handoff.md](antigravity-manual-handoff.md). In short:
1. A dashboard badge shows that QA is waiting (macOS notifications arrive in **[P9]**). Open the packet: `~/.atlas/handoffs/<project>/<execution>/`.
2. In your terminal, `cd` into the QA worktree named in `packet.json`. Start Antigravity there with a **Gemini** model, in plan mode, using the prepared `PROMPT.md`.
3. Don't edit source files. Ask Antigravity for the final JSON only, and save it as `result.json`.
4. Import it: Dashboard → **Import Antigravity result**, or **[P7]** `atlas handoff import <path>/result.json`.
5. ATLAS validates it:
   - the nonce matches;
   - the candidate is unchanged and the source is unmodified;
   - every acceptance criterion is answered;
   - the model is Gemini;
   - the verdict is consistent.

   If anything fails, it tells you exactly why.

ATLAS waits for you indefinitely (D4), and other projects keep running in the meantime. **The packet itself expires**, though: by default after 72 hours, at which point its QA worktree is removed. If it has expired, click **Regenerate packet** before running Antigravity; the new packet has a new nonce. A result from an expired packet is rejected on import.

---

## 6. Controls **[P1 basic, P9 complete]**
| Control | Effect |
|---|---|
| **Pause** | Stops new actions safely. Running work is settled, and any queued GitHub merge is withdrawn first |
| **Resume** | Continues from the last checkpoint after re-checking every gate |
| **Cancel** | Stops the task. Open PRs are closed only with your confirmation |
| **Retry** | Re-runs a failed step within its limits |
| **View logs / diff / plan / findings** | Read-only views of the recorded evidence |
| **Approve** | Plans, protected-path changes and merges |

**After a restart or crash:**
- Everything comes back **paused**, and the dashboard shows a recovery summary. Until Phase 6, you must always press Resume. From Phase 6 an optional `recovery.auto_resume` setting exists; it's off by default, and resume still happens only after the checks below.
- Before you press Resume, ATLAS has already finished several checks: it has stopped leftover processes and containers, saved any half-finished work, and checked GitHub for PRs or merges that happened while it was down.

---

## 7. When things stop: what each state means
| State | Meaning | What to do |
|---|---|---|
| `AWAITING_MANUAL` | Antigravity QA is waiting for you | §5 |
| `AWAITING_APPROVAL` | A plan, merge or protected-path change needs you | Approve or reject in the dashboard |
| `PAUSED (capacity)` | An agent hit its subscription limit or logged out | Wait for the limit to reset, or sign in again (`claude`, `codex login`). ATLAS never switches to paid API use |
| `BLOCKED(no_test_sandbox)` | Colima isn't running, host folders are visible, the image isn't approved, or the self-test failed | Follow the message in Settings → Test sandbox |
| `BLOCKED(github_credential)` | PAT missing or under-privileged | §1.3 |
| `BLOCKED(repair_budget_exhausted)` | Fixes failed 3 times | Read the findings and logs; re-plan or cancel |
| `BLOCKED(unsupported_repo_feature)` | Submodules, Git LFS, or unsafe symlinks | Not supported yet |
| `BLOCKED(engine_state_divergence)` | Internal workflow state and records disagree after a crash | Review, then **Rebuild** (audited) |
| `BLOCKED(external_merge_mismatch)` | Someone merged a different commit on GitHub | Review what was merged |

---

## 8. When a task finishes **[P9]**
ATLAS produces a **final report** covering:
- every phase, PR and merge commit;
- tests executed;
- findings and how each was resolved;
- QA results and their labels;
- waivers, and anything rolled back.

It states plainly any step that was manual or not fully autonomous, for example Antigravity QA.

## 9. What ATLAS will not do
- It won't use API keys or paid API credits, or switch billing.
- It won't read or copy your Claude, Codex or Antigravity credentials, and it doesn't use your broad `gh` login. It reads only the per-project PAT you stored for it, and passes that only to its own GitHub `git`/`gh` subprocesses, never to agents or tests.
- It won't launch Antigravity for you.
- It won't merge without your approval (D6), or merge anything other than the reviewed and tested candidate commit.
- It won't push to `main`, force-push protected branches, bypass branch protection, or treat an AI review as a GitHub approval.
- It won't run tests or dependency setup outside the Docker sandbox. Developer agents (Codex, Claude) do edit and inspect code in host worktrees, inside their own CLI sandboxes and ATLAS's agent wrapper. You accepted that the agent CLIs themselves run as you (D9).
- It won't send data anywhere except the agent CLIs' own providers, GitHub, and (during dependency setup only) the package registries you approved for a project. LangGraph tracing is off.
