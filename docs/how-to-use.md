# How to use ATLAS: a step-by-step guide

> **Current Status: Phase 1 (Core Foundation) is live.**
> You can now install ATLAS (`uv sync --extra dev`), start the local orchestrator and dashboard (`uv run atlas serve`), register projects, create tasks, and pause, resume, cancel, or rebuild them.
> Each step below has an **Available:** tag such as **[P1]** naming the roadmap phase that delivers it ([implementation-roadmap.md](implementation-roadmap.md)). Steps tagged **[today]** and **[P1]** work now; agent execution (**[P2–P3]**), Colima Docker sandbox testing (**[P4]**), and GitHub PR delivery (**[P5]**) fail closed until their respective phases are completed.
> Details may change slightly during implementation. When they do, this guide is updated in the same PR.

---

## Contents
- [Part 0. ATLAS in one minute](#part-0-atlas-in-one-minute)
- [Part 1. Prepare your computer](#part-1-prepare-your-computer) (steps 1–4)
- [Part 2. Install and configure ATLAS](#part-2-install-and-configure-atlas) (steps 5–7)
- [Part 3. Add a project](#part-3-add-a-project) (steps 8–10)
- [Part 4. Give ATLAS work](#part-4-give-atlas-work) (steps 11–13)
- [Part 5. Manual QA with Antigravity](#part-5-manual-qa-with-antigravity) (step 14)
- [Part 6. Day-to-day controls](#part-6-day-to-day-controls)
- [Part 7. Troubleshooting: what each state means](#part-7-troubleshooting-what-each-state-means)
- [Part 8. What ATLAS will never do](#part-8-what-atlas-will-never-do)

---

## Part 0. ATLAS in one minute

ATLAS is a program you run on your own computer. You give it a software task for one of your GitHub repositories, for example "Add Google OAuth login". ATLAS then:

1. asks one AI agent to **plan** the work as small phases;
2. asks a **different** AI agent to **challenge** the plan;
3. waits for **you** to approve the plan;
4. for each phase: an agent **writes the code**, ATLAS **runs the tests** in a locked-down container, another agent **reviews** the code, and a QA step **checks** it;
5. opens a pull request and waits for **you** to approve the merge.

You stay in control: nothing is built before you approve the plan, and nothing is merged before you approve the merge.

### Words used in this guide
| Word | Meaning |
|---|---|
| **Agent** | An AI coding tool ATLAS can use, such as Claude Code, Codex, or a local model. You bring your own |
| **Role** | A job in the workflow: *architect* (plans), *challenger* (attacks the plan), *developer* (writes code), *reviewer* (reviews code), *qa* (checks the result) |
| **Model family** | Who made the model, e.g. Anthropic (Claude), OpenAI (GPT/Codex), Google (Gemini), Alibaba (Qwen), Meta (Llama). Reviews must come from a different family than the author |
| **Phase** | One small, separately mergeable piece of a task |
| **Candidate commit** | The exact commit that was tested and reviewed. Only this commit may be merged |
| **Sandbox** | The Docker container, in its own virtual machine, where ATLAS runs tests. Tests never run directly on your computer |
| **PAT** | A GitHub *Personal Access Token*: a password-like key that lets ATLAS work on one repository |
| **Dashboard** | ATLAS's web page. It runs only on your computer, at `127.0.0.1` |
| **BLOCKED / PAUSED** | ATLAS stopped and needs something from you. [Part 7](#part-7-troubleshooting-what-each-state-means) explains each one |

LangGraph is ATLAS's **internal** workflow engine (ADR-001, ADR-006). You never use it directly; there's nothing to install, configure or pay for.

---

## Part 1. Prepare your computer

### Step 1. Check your operating system
**Available:** [today]
ATLAS runs on **macOS** or **Linux** (D13). Windows isn't supported yet; WSL2 may come later.

Each person installs their own copy of ATLAS and uses their own accounts and models (ADR-007). There is no shared, hosted ATLAS.

### Step 2. Install the required tools
**Available:** [today]
| Tool | Why ATLAS needs it | Check it's installed |
|---|---|---|
| `git` | Works with your repositories | `git --version` |
| `gh` (GitHub CLI) | Opens pull requests and reads CI results | `gh --version` |
| Python 3.12 | ATLAS is written in Python | `python3.12 --version` |
| `uv` | Installs ATLAS's Python packages | `uv --version` |
| Node 24 | Builds the dashboard | `node --version` (should print `v24…`; on macOS, follow Homebrew's printed note to add `node@24` to your `PATH`) |
| Colima + Docker CLI | Runs the test sandbox (D11) | `colima version` and `docker --version` |
| Linux only: QEMU + KVM | Lets Colima run its virtual machine on Linux | `ls /dev/kvm` |
| A secret store | Holds your tokens safely | macOS: Keychain, built in. Linux: a Secret Service provider such as GNOME Keyring or KWallet, plus `secret-tool` |

**On macOS**, with [Homebrew](https://brew.sh) installed:
```bash
brew install git gh python@3.12 uv node@24 colima docker
```
**On Linux**, install the same tools with your distribution's package manager (for example `apt` or `dnf`), or by following each tool's official install guide.

✅ **Done when** every command in the "Check" column prints a version, with no "command not found".

### Step 3. Set up at least two agents from different model families
**Available:** [today]
ATLAS needs **at least two agents from different model families**, because a change must be reviewed by a model from a different family than the one that wrote it (ADR-005). Choose one of these setups:

| Setup | Agents you need | Notes |
|---|---|---|
| **A. Recommended** | Claude Code + Codex + Antigravity | Uses your existing subscriptions |
| **B. Fully local** | Two or more open-weight models from different families, e.g. Qwen and Llama, served by Ollama, LM Studio, vLLM or `llama-server` | No subscriptions; code stays on your machine |

**For setup A:**
1. **Install** each tool by following its official install guide:
   - Claude Code: <https://www.anthropic.com/claude-code>
   - Codex CLI: <https://github.com/openai/codex>
   - Antigravity: Google's official Antigravity download page. ATLAS never runs Antigravity; you use it yourself in Step 14.
2. **Check** each is installed: `claude --version`, `codex --version`, `agy --version`.
3. **Sign in to each one yourself**, in your terminal. ATLAS never signs in for you and never reads these logins.
   ```bash
   claude          # Claude Code: follow the sign-in prompt, then exit
   codex login     # Codex: sign in with ChatGPT (or another method Codex supports)
   agy             # Antigravity: follow its sign-in prompt, then exit
   ```

**For setup B:**
1. **Install** a local model server (Ollama, LM Studio, vLLM or `llama-server`) from its official site, and the Codex CLI (link above), which ATLAS uses to drive a local model as a developer.
2. **Download** at least two models from different families, e.g. a Qwen coder model and a Llama model. With Ollama, for example: `ollama pull <model>`.
3. **Start** the server so that it listens on `127.0.0.1` (your computer only), and check that it answers. With Ollama, for example:
   ```bash
   curl http://127.0.0.1:11434/v1/models
   ```

✅ **Done when** each CLI starts without asking you to sign in (setup A), or your local model server lists your models (setup B).

### Step 4. Create a GitHub token for each repository
**Available:** [today]
ATLAS uses one **fine-grained PAT per project**, limited to that single repository (D3). It will not use your normal `gh` login.

1. On GitHub, open **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**.
2. **Repository access:** choose *Only select repositories* and pick **one** repository.
3. **Repository permissions:** set exactly these:

   | Permission | Access |
   |---|---|
   | Contents | Read and write |
   | Pull requests | Read and write |
   | Actions | Read |
   | Checks | Read |
   | Commit statuses | Read |
   | Metadata | Read |
   | Administration | Read (lets ATLAS read branch protection) |

   **Do not grant** Workflows (write), Administration (write) or Secrets.
4. Click **Generate token**. Keep the page open, or copy the token somewhere temporary and safe: you store it in Step 9, once ATLAS has given the project its id.

ATLAS checks the token's permissions automatically when you register the project (Step 9). A stand-alone check script (`verify-github-pat.sh`, which never prints the token) is part of decision D3 but isn't published in this repository yet.

---

## Part 2. Install and configure ATLAS

### Step 5. Install and start ATLAS
**Available:** [P1]
```bash
git clone https://github.com/<owner>/atlas-orchestrator.git   # the URL of the ATLAS repository
cd atlas-orchestrator
uv sync --extra dev     # installs ATLAS and its dependencies into .venv
uv run atlas serve      # starts ATLAS on 127.0.0.1:8420 and opens the dashboard in your browser
```
- The dashboard runs only on your computer (`127.0.0.1`) and is protected by a per-install session token.
- ATLAS keeps its data in `~/.atlas/`: databases, logs and artefacts.
- **[P10]** An install script, and an optional service that starts ATLAS when you log in: `launchd` on macOS, a `systemd --user` unit on Linux.

✅ **Done when** the dashboard opens in your browser.

### Step 6. Tell ATLAS about your agents
**Available:** [P1 schema, P2 adapters, P10 CLI]
Open **Dashboard → Settings → Agents** and add each agent from Step 3. **[P10]** `atlas agents add` does the same from the terminal. ATLAS saves the list in `~/.atlas/agents.yaml`. Edit agents only through the dashboard or the `atlas agents` command; a repository can never change this file.

For each agent you choose an **adapter** (how ATLAS talks to it) and a **billing** mode.

**Adapters:**
| Adapter | Use it for | Roles it can fill |
|---|---|---|
| `claude_cli`, `codex_cli` (built-in, verified) | Claude Code; Codex with ChatGPT sign-in, an API key, or a local OSS provider | Any role |
| `manual_handoff` (built-in) | A tool you run yourself, e.g. Antigravity | QA, or any read-only role |
| `openai_compatible_http` | A local model server: Ollama, LM Studio, vLLM, `llama-server` | **Read-only roles only** (challenger, reviewer) |

Other agent CLIs aren't supported in this release. A new CLI is added only as a verified manifest in an ATLAS PR (ADR-007).

**Billing** is set per agent and never changes on its own (D15):
- `subscription`: uses the CLI's own login (Step 3).
- `local`: uses your own model server.
- `api_key`: uses an API key you store (see below). Only that agent ever receives it.

If a subscription runs out, ATLAS **pauses**. It never moves the work to a paid API agent.

**Example A, recommended (Claude + Codex + Antigravity).** This is what the dashboard saves:
```yaml
agents:
  claude:      { adapter: claude_cli, models: [<claude model>], billing: subscription }
  codex:       { adapter: codex_cli,  models: [<codex model>],  billing: subscription }
  antigravity: { adapter: manual_handoff, provider: google, declared_family_required: true }
```

**Example B, fully local (no subscriptions):**
```yaml
agents:
  local-coder:    { adapter: codex_cli, models: [qwen3-coder], model_family: alibaba,
                    billing: local, endpoint: "http://127.0.0.1:11434/v1" }
  local-reviewer: { adapter: openai_compatible_http, models: [llama-4-maverick], model_family: meta,
                    billing: local, endpoint: "http://127.0.0.1:11434/v1" }
  manual-qa:      { adapter: manual_handoff, provider: local, declared_family_required: true }
```
Model ids above are examples. Use the ones you actually run, pinned to exact versions (D10).

**Rules ATLAS enforces for every setup:**
- The reviewer, challenger and QA must come from a different provider **and** a different model family than the author (ADR-005). Two local models count as different providers, so for them only the family must differ.
- An unknown model family blocks until you set `model_family`.
- If no agent can fill a role independently, the project shows `BLOCKED(agent_config)`.
- Model servers must be on `127.0.0.1`. You can set `allow_remote_endpoint: true` for an agent, and the dashboard then warns you that your code leaves your machine.
- Codex agents, with any billing, are blocked on **public** repositories (D5).
- Results always show which model did the review. A small local model is allowed, but you can see what you're relying on.

**Only if you chose `billing: api_key`:**
- Store the key with **[P10]** `atlas secrets set <agent_id>`. Until then, use the commands in Step 9 with the service name `atlas.agent.<agent_id>`.
- HTTP agents get the key as a request header.
- A CLI agent gets it only once ATLAS has proven that the CLI doesn't pass it on to the commands it runs. Until then that agent shows `BLOCKED(api_key_not_isolated)`. For `claude_cli` and `codex_cli`, this is proven or refuted in Phase 2.

✅ **Done when** Settings → Agents lists your agents with no warnings.

### Step 7. Set up the test sandbox (one time)
**Available:** [P4]
Tests run only inside Docker containers, in a dedicated Colima virtual machine (D11). This is the same on macOS and Linux. The exact flags are confirmed in Phase 4.

1. **Reserve disk space** for the virtual machine. You need admin rights.
   - **macOS:** create a dedicated APFS volume. First find your container ID:
     ```bash
     diskutil apfs list
     ```
     Then create an 80 GB volume, replacing `<container>` with that ID:
     ```bash
     sudo diskutil apfs addVolume <container> APFS AtlasSandbox -reserve 80g -quota 80g
     ```
   - **Linux:** create a dedicated partition, or a fixed-size LVM logical volume (e.g. 80 GB), and mount it where ATLAS's Settings page says.
2. **Start the ATLAS virtual machine.** Run `atlas sandbox command` (or open **Dashboard → Settings → Test sandbox**) and run the command it prints. It is `colima start atlas` with `COLIMA_HOME` on the volume from step 1, `--mount none` (no folders shared from your computer) and `--activate=false` (your current Docker context stays as it is). ATLAS refuses to run tests if the VM can see any of your folders.
3. **Run the self-test:** `atlas sandbox selftest`, or **Run self-test** on the same page. It creates one container under the full security contract, checks the container's effective settings *before* starting it, then runs a trusted canary that asserts: no network interface except loopback, no Docker socket, user 10001, read-only source and root filesystem, no host folders visible. `atlas sandbox status` shows the read-only checks without starting anything.

✅ **Done when** Settings → Test sandbox shows the sandbox as ready. If it isn't ready, projects show `BLOCKED(no_test_sandbox)` with the reason, and no tests run: ATLAS never runs tests or dependency setup outside the sandbox. (Developer agents are different: they edit code in worktrees on your computer, inside their own CLI sandboxes. See [Part 8](#part-8-what-atlas-will-never-do).)

---

## Part 3. Add a project

### Step 8. Check that your project is supported
**Available:** [P4]
Before adding a repository, check that it fits these limits:
- [ ] **Linux toolchains only.** Projects that need macOS toolchains (Xcode, Swift for iOS/macOS, macOS-only binaries) can't be tested.
- [ ] **Tests don't write to tracked files.** Tests may write only to declared `writable_paths` and `/tmp`.
- [ ] **Tests don't need the network.** Installing dependencies may use approved package registries, through ATLAS's allowlist proxy. Tests themselves always run without network, so integration tests that call external services aren't supported yet.
- [ ] **No Git submodules, no Git LFS**, and no symlinks that point outside the repository.
- [ ] **No Dockerfile from the repository.** Tests use an approved, digest-pinned public image.
- [ ] **The repository is private.** Public repositories block Codex (D5), and the recommended setup uses Codex. A public project is therefore **blocked** until you make a new decision about an independent role assignment (ADR-005).

If a project doesn't fit, its testing is blocked. ATLAS never falls back to running tests directly on your computer.

### Step 9. Register the repository and store its token
**Available:** [P1 registration and token check, P5 token use and protection discovery]
1. Open **Dashboard → Projects → Register repository**.
2. Enter the repository as `owner/name`, e.g. `<your-github-user>/my-app`. A repository can be registered only once.
3. Open **Dashboard → Settings → GitHub credentials**, paste the token from Step 4 into the project's field and choose **Save & verify**. ATLAS writes it straight to your OS secret store under `atlas.<project_id>.github`, checks it with GitHub, and never shows it again. **Remove token** deletes it and blocks the project.

   You can also store it from a terminal instead. The command asks for the token without showing it, so it doesn't end up in your shell history:
   ```bash
   # macOS (Keychain)
   security add-generic-password -s "atlas.<project_id>.github" -a "<your-github-user>" -w

   # Linux (Secret Service)
   secret-tool store --label="ATLAS GitHub <project_id>" service "atlas.<project_id>.github" username "<your-github-user>"
   ```
   **[P10]** `atlas secrets set github <project_id>` does the same on either OS.
4. ATLAS checks the token's permissions, your access, the repository's visibility and its default branch. A missing or weak token shows `BLOCKED(github_credential)`. ATLAS never falls back to your `gh` login.
5. **[P5]** ATLAS reads the branch protection rules. If it can't, it asks you to declare the required CI checks and approvals. If you don't, ATLAS never merges on that project, and you merge on GitHub yourself ([Step 13](#step-13-follow-each-phase-and-approve-the-merge-d6)).

### Step 10. Review and approve the project settings
**Available:** [P1; tests P4]
ATLAS proposes the project's settings from what it detects. You review them and approve. Anything the repository itself suggests is treated as untrusted until you accept it. The settings are saved at `~/.atlas/projects/<id>/project.yaml`:

```yaml
# Only keys defined in the approved design are shown; unknown keys are rejected.
project:
  name: my-app
  repository: <your-github-user>/my-app
  base_branch: main
agents:                      # agent ids from Step 6; the reviewer must differ from the author (ADR-005)
  architect: claude
  challenger: codex
  developer: claude
  reviewer: codex
  qa: antigravity            # manual_handoff: you run Antigravity yourself (ADR-003)
tests:                       # [P4]
  writable_paths: [".venv", ".pytest_cache"]   # must not overlap tracked files (security-model.md §5.1.3)
ci:
  required_checks: []        # only needed if ATLAS can't read branch protection
```
For Example B, the roles would be: architect `local-coder`, challenger `local-reviewer`, developer `local-coder`, reviewer `local-reviewer`, qa `manual-qa` (you run QA with a non-Qwen model).

**Test setup** isn't a fixed set of keys yet; the names are decided in Phase 4 and added here then. You will choose:
- **[P4]** an approved, digest-pinned image, e.g. `python:3.12@sha256:…`. ATLAS lists the digest and you approve it once;
- the test commands ATLAS runs;
- an optional dependency-install step, which may use only approved registries through the allowlist proxy.

**Fixed in this release.** ATLAS rejects attempts to change these:
- `auto_merge: false` (D6): you always approve merges;
- `execution.mode: supervised` (D7): you always approve plans;
- no paid API fallback;
- no unsupported integrations.

✅ **Done when** the project appears in the Projects list without a BLOCKED badge.

---

## Part 4. Give ATLAS work

### Step 11. Create a task
**Available:** [P1 record, P3 planning]
Open **Dashboard → your project → New task** and fill in:
1. **Request:** what you want, in plain words, e.g. "Add Google OAuth login".
2. **Category:** feature, bug fix, performance, refactor, security, dependencies, UI, tests, investigation or modernisation.
3. **Priority.**
4. **Acceptance criteria:** how you'll know it's done. Each one gets an ID, and QA must answer every one.
5. Click **Start**.

**What runs today:** Start clones your repository (using the project's GitHub token from Settings), scans it, and asks the project's architect agent for an implementation plan. A different agent (the challenger, Codex by default) then attacks the plan. Blocking findings go back to the architect, who must answer each one and revise the plan. This repeats for up to 3 rounds, then the task waits for you at **Awaiting approval**. Read the plan and the challenge history on the task page, then click **Approve plan** or write what should change and click **Request changes** (the loop runs again). Approving starts the implementation: the developer agent (Claude Code by default; Codex cannot edit files under ATLAS's sandbox yet) edits a private copy of the repository (a git worktree under `~/.atlas/projects/<id>/worktrees/`), and ATLAS commits the result as a **candidate**. The task page lists the changed files, flags any that touch CI configuration, credentials or tool settings, and shows the agent's summary. The task then stops at **Executing phases**: tests, independent review, QA and GitHub delivery are not built yet, so nothing is pushed or merged. If a step cannot run (no token, agent not signed in, usage limit, invalid plan, challenger not independent of the architect), the task shows **Needs attention** with the reason. Fix the cause, then click **Retry**.

### Step 12. Review and approve the plan
**Available:** [P3]
Watch the task's **Timeline**. ATLAS:
1. **Analyses the repository** (stack, tests, CI, conventions). Read-only, by the architect agent.
2. **Writes a phased plan.** Each phase has an objective, criteria, affected files, tests, risk, rollback and dependencies.
3. **Challenges the plan.** The challenger agent attacks it, the architect answers every finding, and this repeats for up to 3 cycles.
4. **Waits for you (D7).** Read the plan, the findings and the answers, then click **Approve** or **Request changes**. Nothing is built before you approve.

### Step 13. Follow each phase and approve the merge (D6)
**Available:** [P4 on your computer, P5 on GitHub]
The task screen shows the current phase and its state. For each phase:

**On your computer [P4]:**

| # | Step | Who | What you see | What you do |
|---|---|---|---|---|
| 1 | Prepare | ATLAS | A new branch `atlas/…` in an isolated worktree | Nothing |
| 2 | Implement | Developer agent | A live log, then a **candidate commit** (`r1`) | Nothing |
| 3 | Protected-path check | ATLAS | If the change touches CI workflows, secrets, agent config or destructive DB migrations: an **approval request** | Approve or reject each kind of change |
| 4 | Test | ATLAS, in the sandbox | Test results for the candidate | Nothing |
| 5 | Code review | Reviewer agent | Findings with severity, file, line and evidence | Read them. CRITICAL and HIGH findings block until fixed and re-checked |
| 6 | Fix loop | Developer, then reviewer | New candidates `r2`, `r3`… Old evidence is marked stale | Nothing. After 3 failed attempts the phase is BLOCKED |
| 7 | QA | QA agent. In setup A: **you + Antigravity** | ⏸ **AWAITING_MANUAL** and a handoff packet | Follow [Part 5](#part-5-manual-qa-with-antigravity) |

**On GitHub [P5]:**

| # | Step | Who | What you see | What you do |
|---|---|---|---|---|
| 8 | Push + PR | ATLAS | A pull-request link | Optional: look at it on GitHub |
| 9 | CI | GitHub | Required checks on the exact candidate commit | Nothing |
| 10 | **Merge approval** | **You** | An **Approve merge** button with an evidence summary. GitHub's own required approvals still apply; an AI review never counts as one | Approve, or leave it |
| 11 | Merge + verify | ATLAS | Merge confirmed against the candidate commit | Nothing |
| 12 | Post-merge check | ATLAS | CI on the base branch after the merge | Nothing |
| 13 | Rollback decision (only if base CI fails) | **You** | **Rollback proposed**, with the failing checks | Approve a revert, or decline (recorded) |
| 14 | Next phase | ATLAS | Starts once its dependencies are complete | Nothing |

**How to read the evidence.** Every result carries a label:
| Label | Meaning | Counts for tests, CI and merge? |
|---|---|---|
| **executed** | ATLAS ran it | Yes |
| **remote-verified** | Read back from GitHub | Yes |
| **agent-reported** | An agent said so | No |
| **manual-imported** | From your Antigravity run | No |
| **proposed** | Suggested but not run | No |

**When you merge on GitHub yourself.** In some repositories ATLAS doesn't merge. It shows "merge on GitHub" and waits. This happens for:
- repositories whose branch protection ATLAS can't read and you haven't declared;
- repositories that use a **merge queue** (merge-queue support is off in this release).

Merge the PR on GitHub. A squash merge is fine: it creates a new commit, which is expected. ATLAS then checks that the PR's **head at merge time** was exactly the reviewed candidate commit, in the expected repository and base branch. It also records the resulting merge commit and confirms it's on the base branch. If the head was different, the phase is BLOCKED for your review.

**If you approve a rollback.** ATLAS creates a *revert phase* that goes through the full pipeline: tests, review, QA and your merge approval. When it completes, the original phase is marked **rolled back**. Phases that depend on it are blocked until you re-plan or cancel them. A revert doesn't undo database migrations or other external side effects; the phase's rollback notes list any manual steps.

---

## Part 5. Manual QA with Antigravity

### Step 14. Run QA yourself and import the result (D4 = wait)
**Available:** [P4 minimal, P7 full]
If your QA agent uses `manual_handoff`, ATLAS prepares a **packet** and waits for you.

**The rule for every manual QA result:** it must declare the `model` you used and its `provider`. The provider must match the agent's configured `provider`. ATLAS works out the model's family and accepts the result only if both the family and the provider are independent of the author (ADR-005). In setup B, any non-Qwen model is accepted.

The steps below are for **setup A**. There the author is Codex (OpenAI) and the reviewer is Claude (Anthropic), so QA must use a **Gemini** model. Full runbook: [antigravity-manual-handoff.md](antigravity-manual-handoff.md).

1. **Notice the badge.** The dashboard shows that QA is waiting (desktop notifications arrive in **[P9]**).
2. **Open the packet folder** and note its path. It holds `packet.json` (which names the QA worktree), `PROMPT.md`, and later your `result.json`:
   ```bash
   PKT=~/.atlas/handoffs/<project_id>/<execution_id>
   WT=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["worktree"])' "$PKT/packet.json")
   ```
3. **Go to the QA worktree:**
   ```bash
   cd "$WT"
   ```
4. **Start Antigravity there** with a **Gemini** model, in **plan mode**, and give it the prompt from `$PKT/PROMPT.md`. The runbook shows the exact command.
5. **Don't edit any source files.** Ask Antigravity for the final JSON only, and save it in the **packet folder**, not the worktree, as `$PKT/result.json`.
6. **Import the result:** Dashboard → **Import QA result**, or **[P7]**:
   ```bash
   atlas handoff import "$PKT/result.json"
   ```
7. **ATLAS checks the result:**
   - the nonce matches the packet;
   - the candidate is unchanged and the source is unmodified;
   - every acceptance criterion is answered;
   - the declared model's family is independent of the author (Gemini, in setup A);
   - the verdict is consistent.

   If anything fails, ATLAS tells you exactly why.

**Timing.** ATLAS waits for you indefinitely (D4), and your other projects keep running meanwhile. **The packet itself expires**, though: by default after 72 hours, and its QA worktree is then removed. If it has expired, click **Regenerate packet** before running Antigravity; the new packet has a new nonce. A result from an expired packet is rejected.

---

## Part 6. Day-to-day controls
**Available:** [P1 basic, P9 complete]

| Button | What it does |
|---|---|
| **Pause** | Stops new actions safely. Running work is settled, and any queued GitHub merge is withdrawn first |
| **Resume** | Continues from the last checkpoint, after re-checking every gate |
| **Cancel** | Stops the task. Open PRs are closed only if you confirm |
| **Retry** | Re-runs a failed step, within its limits |
| **View logs / diff / plan / findings** | Read-only views of the recorded evidence |
| **Approve** | Approves plans, protected-path changes and merges |

**After a restart or crash:**
- Everything comes back **paused**, and the dashboard shows a recovery summary.
- Before you press Resume, ATLAS has already stopped leftover processes and containers, saved any half-finished work, and checked GitHub for PRs or merges that happened while it was down.
- Until Phase 6 you must always press **Resume** yourself. From Phase 6 there is an optional `recovery.auto_resume` setting. It's off by default, and even then ATLAS resumes only after the checks above.

**When a task finishes [P9]**, ATLAS produces a **final report** covering:
- every phase, PR and merge commit;
- the tests executed;
- each finding and how it was resolved;
- QA results and their labels;
- waivers, and anything rolled back.

The report states plainly any step that was manual or not fully autonomous, for example Antigravity QA.

---

## Part 7. Troubleshooting: what each state means
| You see | What it means | What to do |
|---|---|---|
| `AWAITING_MANUAL` | Manual QA is waiting for you | [Step 14](#step-14-run-qa-yourself-and-import-the-result-d4--wait) |
| `AWAITING_APPROVAL` | A plan, merge or protected-path change needs you | Approve or reject it in the dashboard |
| `PAUSED (capacity)` | An agent hit its subscription limit, logged out, or its local model server isn't reachable | Wait for the limit to reset, sign in again (e.g. run `claude`, or `codex login`), or start your model server. ATLAS never switches to paid API use on its own |
| `BLOCKED(agent_config)` | No configured agent can fill a role independently | Add an agent from another model family, or change the project's roles ([Step 6](#step-6-tell-atlas-about-your-agents)) |
| `BLOCKED(api_key_not_isolated)` | An `api_key` CLI agent isn't yet proven to keep its key to itself | Use `subscription` or `local` billing for that agent for now |
| `BLOCKED(no_test_sandbox)` | Colima isn't running, your folders are visible to the VM, the image isn't approved, or the self-test failed | Follow the message in Settings → Test sandbox ([Step 7](#step-7-set-up-the-test-sandbox-one-time)) |
| `BLOCKED(github_credential)` | The PAT is missing or lacks a permission | Check Steps [4](#step-4-create-a-github-token-for-each-repository) and [9](#step-9-register-the-repository-and-store-its-token) |
| `BLOCKED(repair_budget_exhausted)` | Fixes failed 3 times | Read the findings and logs, then re-plan or cancel |
| `BLOCKED(unsupported_repo_feature)` | Submodules, Git LFS, or unsafe symlinks | Not supported yet ([Step 8](#step-8-check-that-your-project-is-supported)) |
| `BLOCKED(engine_state_divergence)` | After a crash, the internal workflow state and ATLAS's records disagree | Review, then click **Rebuild** (audited) |
| `BLOCKED(external_merge_mismatch)` | Someone merged a different commit on GitHub | Review what was merged |

---

## Part 8. What ATLAS will never do
- **Spend money you didn't choose to spend.** It uses API keys or paid API credits only for an agent you set to `billing: api_key`, and it never switches an agent's billing on its own.
- **Touch your agent logins.** It doesn't read or copy the logins your agent CLIs manage (Claude, Codex, Antigravity or others), and it doesn't use your broad `gh` login. It reads only what you stored for it: the per-project PAT, passed only to its own GitHub `git`/`gh` processes, and any per-agent API key, passed only to that agent. Neither ever reaches tests.
- **Launch Antigravity for you.**
- **Merge without your approval** (D6), or merge anything other than the reviewed and tested candidate commit.
- **Bypass GitHub's safety rules.** It won't push to `main`, force-push protected branches, bypass branch protection, or treat an AI review as a GitHub approval.
- **Run tests or dependency setup outside the Docker sandbox.** Note that developer agents (Codex, Claude) do edit and inspect code in worktrees on your computer, inside their own CLI sandboxes and ATLAS's agent wrapper. You accepted that the agent CLIs themselves run as you (D9).
- **Send data anywhere else.** Data goes only to your configured agents' providers (or your local model servers), to GitHub, and, during dependency setup only, to the package registries you approved for a project. LangGraph tracing is off.
