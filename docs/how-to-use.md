# How to use ATLAS (the LangGraph-based orchestrator)

> **Status: planned usage, not yet available.** ATLAS is at the end of Phase 0 (design only). This guide describes how ATLAS will be used according to the approved design. Each step is tagged with the roadmap phase that delivers it, e.g. **[P1]**. Commands and screens may change slightly during implementation, and this guide is updated in the same PR as any such change. Nothing here works until its phase is merged.

LangGraph is ATLAS's **internal** workflow engine (ADR-001, ADR-006). You never use LangGraph directly; there's nothing to install, configure or pay for. You interact with ATLAS through its local dashboard and a small CLI.

---

## 1. One-time setup

ATLAS runs on **macOS or Linux** (D13). Each person installs their own copy and uses their own accounts and models (ADR-007). Windows isn't supported yet; WSL2 may come later.

### 1.1 Prerequisites
| Tool | Why | Check |
|---|---|---|
| `git`, `gh` | Repository and GitHub operations | `gh --version` |
| Python 3.12, `uv`, Node 24 | ATLAS itself | `python3.12 --version` |
| Colima + Docker CLI | Test sandbox (D11). On Linux, also QEMU + KVM | `colima version` |
| A secret store | macOS Keychain (built in) or, on Linux, a Secret Service provider such as GNOME Keyring or KWallet | — |
| **At least two agents from different model families** | They fill the roles. Reviews must come from a different family than the author (ADR-005). See §1.6 | per agent |

ATLAS never signs in to agent tools for you, and never reads the logins those CLIs manage. ATLAS reads only two kinds of credential, both from your OS secret store:
- the per-project GitHub PAT (§1.3), passed only to its own `git`/`gh` processes;
- an API key, but only for an agent you set to `billing: api_key` (§1.5), passed only to that agent's process.

If an agent logs out, ATLAS pauses it and tells you which command to run.

### 1.2 Install and start ATLAS **[P1]**
```bash
git clone https://github.com/<owner>/atlas-orchestrator.git   # the URL of the ATLAS repository
cd atlas-orchestrator && uv sync          # exact install steps finalised in Phase 1 / Phase 10
atlas serve                               # starts ATLAS on 127.0.0.1 and opens the dashboard
```
- The dashboard runs only on your machine (`127.0.0.1`), protected by a per-install session token.
- Data lives in `~/.atlas/` (databases, logs, artefacts). Phase 10 adds an optional login service: `launchd` on macOS, a `systemd --user` unit on Linux.

### 1.3 GitHub token per project (D3) **[P1 checks, P5 use]**
Store your PAT in the OS secret store under ATLAS's name for the project. Both commands prompt for the token without echoing it:
```bash
# macOS (Keychain)
security add-generic-password -s "atlas.<project_id>.github" -a "<your-github-user>" -w
# Linux (Secret Service)
secret-tool store --label="ATLAS GitHub <project_id>" service "atlas.<project_id>.github" username "<your-github-user>"
```
**[P10]** `atlas secrets set github <project_id>` does the same on either OS.
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
Tests run only inside Docker containers on a dedicated Colima VM, on both macOS and Linux. One-time steps (exact flags confirmed in Phase 4):
1. **Reserve disk space** for the VM (admin rights needed):
   - **macOS:** a dedicated APFS volume (find your container ID with `diskutil apfs list`):
     ```bash
     sudo diskutil apfs addVolume <container> APFS AtlasSandbox -reserve 80g -quota 80g
     ```
   - **Linux:** a dedicated partition or fixed-size LVM logical volume (e.g. 80 GB), mounted where ATLAS's Settings page says.
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
- **No fallback to running tests directly on your machine.** If a project doesn't fit these rules, its testing is blocked (fail closed).

## 1.6 Configure your agents **[P1 schema, P2 adapters, P10 CLI]**
You list the agents you have in `~/.atlas/agents.yaml`, through the dashboard (Settings → Agents) or **[P10]** `atlas agents add`. Projects then assign roles to those agents. The full design is in ADR-007.

| Adapter | Use it for | Roles |
|---|---|---|
| `claude_cli`, `codex_cli` (built-in, verified) | Claude Code, or Codex with ChatGPT sign-in, an API key or a local OSS provider | Any role the manifest allows |
| `manual_handoff` (built-in) | A tool you run yourself, e.g. Antigravity | QA, or any read-only role |
| `openai_compatible_http` | A local model server (Ollama, LM Studio, vLLM, `llama-server`) | **Read-only roles only** (challenger, reviewer) |
| A manifest you add yourself | Another agent CLI | Read-only roles until it becomes `verified` through an ATLAS PR |

**Billing** is set per agent and never changes on its own:
- `subscription`: the CLI's own login.
- `local`: your own model server.
- `api_key`: you store the key with **[P10]** `atlas secrets set <agent_id>` (until then, the OS commands in §1.3 with service `atlas.agent.<agent_id>`). Only that agent's process receives it.

If a subscription runs out, ATLAS pauses. It never moves the work to a paid API agent.

**Example A: Claude + Codex + Antigravity** (the recommended profile)
```yaml
agents:
  claude:      { adapter: claude_cli, models: [<claude model>], billing: subscription }
  codex:       { adapter: codex_cli,  models: [<codex model>],  billing: subscription }
  antigravity: { adapter: manual_handoff, declared_family_required: true }
# project roles: architect claude · challenger codex · developer codex · reviewer claude · qa antigravity
```

**Example B: fully local, open-weight models** (no subscriptions)
```yaml
agents:
  local-coder:    { adapter: codex_cli, models: [qwen3-coder], model_family: alibaba,
                    billing: local, endpoint: "http://127.0.0.1:11434/v1" }
  local-reviewer: { adapter: openai_compatible_http, models: [llama-4-maverick], model_family: meta,
                    billing: local, endpoint: "http://127.0.0.1:11434/v1" }
  manual-qa:      { adapter: manual_handoff, declared_family_required: true }
# project roles: architect local-coder · challenger local-reviewer · developer local-coder
#                reviewer local-reviewer · qa manual-qa (you run QA with a non-Qwen model)
```

Rules ATLAS enforces for any profile:
- The reviewer, challenger and QA must be a different agent **and** a different model family from the author (ADR-005).
- Unknown families block until you declare `model_family`.
- A profile that can't satisfy a role shows `BLOCKED(agent_config)`.
- Model servers must be on `127.0.0.1` unless you set `allow_remote_endpoint: true` for that agent. The dashboard then warns that code leaves your machine.
- Gate results show which model reviewed the change. A small local model is allowed, but you can see what you're relying on.
- Model ids above are examples. Pin the ones you actually run (D10).

## 2. Register a project **[P1 record; P5 protection discovery; P4 test config]**
Dashboard → **Projects → Register repository**:
1. Enter `owner/name`, e.g. `<your-github-user>/my-app`. ATLAS checks access, visibility and default branch.
   - Public repositories: Codex is blocked (D5), and the default roles use Codex. A public project is therefore **blocked** until you make a new decision about a valid independent role assignment (ADR-005). You currently work only on private repos.
   - A repository can be registered only once.
2. **[P5]** ATLAS reads branch protection. If it can't, it asks you to declare the required CI checks and approvals; otherwise ATLAS will never merge on that project, and you merge on GitHub yourself (§4).
3. Review the **project config**. ATLAS proposes it from what it detects, and you approve it; anything the repository itself suggests is treated as untrusted until you accept it. The file is stored at `~/.atlas/projects/<id>/project.yaml`:

```yaml
# Only keys defined in the approved design are shown; unknown keys are rejected.
project:
  name: my-app
  repository: <your-github-user>/my-app
  base_branch: main
agents:                      # ids from your agents.yaml (§1.6); reviewer must differ from the author (ADR-005)
  architect: claude
  challenger: codex
  developer: codex
  reviewer: claude
  qa: antigravity            # manual_handoff: you run Antigravity yourself (ADR-003)
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
| Implement | Developer agent (e.g. Codex) | Live log; then a **candidate commit** (`r1`) | — |
| Protected-path check | ATLAS | If the change touches CI workflows, secrets, agent config or destructive DB migrations: **approval request** | Approve or reject each violation kind |
| Test | ATLAS (Docker) | Tests **executed** on the candidate, with results | — |
| Code review | Reviewer agent (e.g. Claude) | Findings with severity, file, line, evidence | Read. CRITICAL and HIGH block until fixed and re-verified |
| Fix loop | Developer → reviewer | New candidate `r2`… Old evidence is marked stale | — (stops after 3 attempts → BLOCKED) |
| QA | QA agent; with the recommended profile, **you + Antigravity** | ⏸ **AWAITING_MANUAL** + a handoff packet | See §5 |
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
1. A dashboard badge shows that QA is waiting (desktop notifications arrive in **[P9]**). Open the packet: `~/.atlas/handoffs/<project>/<execution>/`.
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
| `PAUSED (capacity)` | An agent hit its subscription limit, logged out, or its local model server isn't reachable | Wait for the limit to reset, sign in again (e.g. `claude`, `codex login`), or start your model server. ATLAS never switches to paid API use on its own |
| `BLOCKED(agent_config)` | No configured agent can fill a role independently | Add an agent from another model family, or change the project's roles (§1.6) |
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
- It won't use API keys or paid API credits unless you set that agent to `billing: api_key`, and it never switches an agent's billing on its own.
- It won't read or copy the logins your agent CLIs manage (Claude, Codex, Antigravity, others), and it doesn't use your broad `gh` login. It reads only what you stored for it: the per-project PAT, passed only to its own GitHub `git`/`gh` subprocesses, and any per-agent API key, passed only to that agent. Neither ever reaches tests.
- It won't launch Antigravity for you.
- It won't merge without your approval (D6), or merge anything other than the reviewed and tested candidate commit.
- It won't push to `main`, force-push protected branches, bypass branch protection, or treat an AI review as a GitHub approval.
- It won't run tests or dependency setup outside the Docker sandbox. Developer agents (Codex, Claude) do edit and inspect code in host worktrees, inside their own CLI sandboxes and ATLAS's agent wrapper. You accepted that the agent CLIs themselves run as you (D9).
- It won't send data anywhere except your configured agents' providers (or your local model servers), GitHub, and (during dependency setup only) the package registries you approved for a project. LangGraph tracing is off.
