# ADR-007: Portable, self-hosted ATLAS with user-configured agents

- Status: Proposed
- Date: 2026-10-22
- Decisions: D13 (platforms), D14 (local and open-weight models), D15 (API-key billing)

## Context
The Phase 0 design matched one machine:
- agent roles were fixed to `claude | codex | antigravity` (agent-compatibility.md §5);
- the platform was macOS only: Keychain, Colima on an APFS volume, Seatbelt and `launchd`;
- examples used the author's GitHub account.

Other people should be able to run ATLAS with the agents and models *they* have. That includes open-weight models served on their own machine.

## Decision

### 1. Distribution model
- ATLAS is **self-hosted and single-operator**. Each person installs their own copy and runs it on their own machine with their own accounts.
- A hosted or multi-user ATLAS that runs work through one person's subscriptions is still a **non-goal**. The provider terms cited in ADR-004 forbid it.
- No account names, repository names or paths are hard-coded. Docs use placeholders such as `<owner>/<repo>`.

### 2. Agent registry
Agents are configured by the user, not hard-coded. `~/.atlas/agents.yaml` is global, non-secret, schema-validated and rejects unknown keys:

```yaml
agents:
  claude:                         # registry id, referenced by project roles
    adapter: claude_cli           # a vetted adapter manifest (§3); provider derived: anthropic
    models: [claude-opus-5-5]     # pinned explicitly (D10); no fallback models
    billing: subscription         # subscription | api_key | local   (D15)
  codex:
    adapter: codex_cli
    models: [<model id>]
    billing: subscription
  antigravity:
    adapter: manual_handoff
    provider: google                  # canonical provider, or local; required for manual_handoff (rule 4 below)
    declared_family_required: true    # the user's imported result must name its model
  local-coder:
    adapter: codex_cli                # same vetted adapter, pointed at a local OSS provider
    models: [qwen3-coder]
    model_family: alibaba             # required for any model not in the built-in family map
    billing: local
    endpoint: http://127.0.0.1:11434/v1
  local-reviewer:
    adapter: openai_compatible_http   # read-only roles only (§4)
    models: [llama-4-maverick]
    model_family: meta
    billing: local
    endpoint: http://127.0.0.1:11434/v1
```

Project roles refer to registry ids:

```yaml
agents: { architect: claude, challenger: codex, developer: codex, reviewer: claude, qa: antigravity }
```

- **The roles above are a recommended profile, not built-in rules.** The policy engine checks every assignment against the adapter's allowed roles and against ADR-005 independence.
- A project whose roles can't be satisfied is shown as `BLOCKED(agent_config)`, with the reason.
- Each agent has three separate identities, all snapshotted with every execution and review (database-design.md): `agent_id` (the registry name), `provider` (who runs inference) and `model_family` (the model's lineage, ADR-005). The provider is derived in this order, and the first rule that matches wins:
  1. `billing: local` **and** an endpoint that passes the loopback check (§4) → `local`. `billing: local` with a non-loopback endpoint is a config error, rejected at validation.
  2. A remote endpoint (`allow_remote_endpoint: true`) → the user must pick `provider` from ATLAS's fixed list of canonical providers (e.g. `anthropic`, `openai`, `google`, `mistral`, `deepseek`, `together`, `groq`, `openrouter`). The hostname is never used as the identity, because two hostnames can belong to one operator. A remote endpoint with no declared provider is `unknown`, which counts as equal to every provider, so independence fails closed.
  3. A hosted CLI with subscription or api_key billing → the manifest's provider (`claude_cli` → `anthropic`, `codex_cli` → `openai`).
  4. A `manual_handoff` agent → the `provider` the user configures: a canonical provider (Antigravity → `google`) or `local` for a model the user runs on their own machine. ATLAS can't verify a manual run, so this is a recorded user declaration, like the model family.
     - The imported result must declare `model` and `provider`. A provider that differs from the agent's configured one is rejected.
     - With no configured provider, it is `unknown`, which fails independence closed.
     - Results keep the `manual_imported` evidence label.
- `agents.yaml` is edited only through the dashboard or `atlas agents …` (Phase 10). Like project config, a repository can never supply or change it.

### 3. Adapter manifests (declarative, vetted)
Each adapter `kind` is ATLAS code: `cli`, `openai_compatible_http` or `manual_handoff`. A **manifest** fills in the per-tool details for a `cli` adapter:
- the argv template for each role, with no shell, using the same placeholder substitution and allowlisted values as the existing templates;
- the output format and the parser to use (`stream-json`, `jsonl-events`, a final JSON file);
- regex fixtures that map exit codes and stderr to capacity states;
- the auth probe, the version probe and the flags the capability probe must find;
- the env variables the tool needs. They come from the allowlist plus its own credential (§5), never the user's whole environment;
- the config-isolation flags, for example Codex `--ignore-user-config` and Claude `--setting-sources user`. Each must have a hostile-config fixture test (security-model.md §3.2).

**Only verified manifests load in this release.** A manifest is `verified` when it ships in the ATLAS repo with recorded fixtures and passes the hostile-repo and hostile-config tests. ATLAS refuses to load a user-added manifest: being assigned a read-only role doesn't make an unfamiliar CLI read-only, because the CLI could still run repository hooks or write to the worktree. A local copy of ATLAS can add a manifest only through a reviewed change to ATLAS itself. User-added manifests would need an independently enforced read-only filesystem and execution boundary, which is deferred. Until then, a user who wants another model without a vetted CLI uses the `openai_compatible_http` reviewer (§4).
- There is no "run this command" or free-form shell adapter.
- Built-in manifests in the first release: `claude_cli`, `codex_cli`, `manual_handoff`. Candidates added through the normal phase process include OpenCode, Aider, Goose and Gemini CLI. Each needs its own terms check, like ADR-003 and ADR-004.
- **D5 is unchanged.** Every agent using the `codex_cli` manifest stays blocked on public repositories, whatever its billing. Narrowing D5 to ChatGPT sign-in would be a new user decision.

### 4. Local and open-weight models (D14)
- **Write roles (developer, fixer)** use an *agent CLI* that supports local models, for example Codex with a local or OSS provider, or OpenCode. ATLAS does **not** implement its own tool-calling agent loop. That would be a new agent with a large attack surface, outside ATLAS's job.
- **Read-only roles (challenger, reviewer)** may use `openai_compatible_http`. ATLAS POSTs one chat-completion request:
  - the request has a structured-output schema;
  - the context is ATLAS-assembled: the candidate diff and the plan, fenced as `UNTRUSTED`;
  - there are no tools and no function calling;
  - the reply is validated against the findings schema, just like CLI output. The evidence label is `agent_reported`.
- **Endpoints are loopback-only by default** (`127.0.0.1`, `::1`, or a Unix socket). Any other host needs `allow_remote_endpoint: true` on that agent. The dashboard then shows that source code leaves the machine. The HTTP client:
  - resolves the host once and **connects to that validated address**; TLS still verifies the configured hostname;
  - **ignores proxy environment variables** (`HTTP(S)_PROXY`, `ALL_PROXY`, `NO_PROXY`);
  - **rejects redirects**;
  - re-validates the address on every request.
- **Local CLI providers** (e.g. Codex `--oss`) resolve their own endpoint, so ATLAS can't pin it. Before each launch, ATLAS checks that the configured provider endpoint is loopback, and the manifest pins that endpoint on the command line. Proxy variables are not passed to the agent (they are not on the env allowlist). Residual risk: the CLI's own network behaviour is trusted as published, like any verified CLI.
- Local models have no quota; their capacity states come from HTTP status and timeouts.
- A local model may be much weaker than a frontier model. Gate results show the reviewer's model, so the user can judge.

### 5. Billing and credentials (D15)
- `billing: subscription` keeps ADR-004 unchanged. The CLI owns its login, and ATLAS strips every provider API-key variable.
- `billing: api_key` is an **explicit, per-agent opt-in**:
  - the user stores the key with `atlas secrets set <agent_id>`;
  - ATLAS puts it in the OS secret store (§6) under `atlas.agent.<agent_id>`.
  - **Where the key may go.** An env variable passed to a CLI is inherited by every command the CLI runs, including commands steered by repository content. So:
    - For `openai_compatible_http` agents, ATLAS sends the key as an HTTP header. No subprocess ever sees it.
    - For a `cli` agent, the key is allowed only if the manifest declares `credential_isolation: verified`. That means a fixture test proves the CLI removes the key from the environment of every tool or shell command it launches: an agent-launched `env` dump must not contain it. The manifest names the one variable that receives the key.
    - If the manifest doesn't declare this, every role of that agent is `BLOCKED(api_key_not_isolated)`. A credential broker or runner design that would lift this is deferred.
    - Proving this for `claude_cli` and `codex_cli` is a Phase 2 spike item. Until then, both use `subscription` or `local` billing only.
  - Every other agent and every test, git and gh process still has all key variables stripped.
- **Exact-value redaction.** Every secret ATLAS loads (PAT, API key, bearer token) is registered with the redactor **before** any launch or request that uses it. The redactor removes the exact value, in addition to known patterns such as `sk-…`, from all stdout, stderr, events, error messages and artefacts. Tests cover tokens that match no known pattern.
- `billing: local` passes no credential. An optional bearer token for a local server is handled like an HTTP API key (header only, redacted).
- **There is never a silent switch between billing modes.**
  - A subscription agent that runs out of quota pauses, as before.
  - ATLAS never retries a subscription agent's work with an api_key agent. `allow_paid_api_fallback` stays locked to `false`.
  - Moving a role to another agent is a config change the user makes, recorded in `audit_log`. It is not a fallback.
- This changes ADR-004: ATLAS now *stores* API keys the user gives it. It still never reads, copies or proxies the subscription credentials that the CLIs manage.

### 6. Platforms (D13): macOS and Linux
Platform-specific code sits behind small interfaces chosen at startup:

| Interface | macOS | Linux | Notes |
|---|---|---|---|
| `SecretStore` | Keychain | Secret Service (GNOME Keyring / KWallet) via `keyring` | No plaintext-file fallback. If there's no backend, credential-needing operations are `BLOCKED(no_secret_store)` |
| `TestSandbox` | Colima VM (`vz` / QEMU) | Colima / Lima VM on QEMU + KVM | **Same VM boundary on both.** No host mounts, explicit socket, everything in security-model.md §5.1 applies unchanged. Running Docker natively on the Linux host is rejected, because then containers share the host kernel and daemon |
| Reserved disk for the sandbox VM | Dedicated APFS volume, reserve = quota | Dedicated partition, or a fully allocated (thick) LVM logical volume. Thin, sparse or loop-file backing is rejected after inspecting the backing device (`lsblk`, `lvs -o lv_layout`) | Same "reserved, not observed" rule (security-model.md §5.1) |
| `AgentBoundary` (outer wrapper for agent processes) | Seatbelt (`sandbox-exec`) | bubblewrap, with Landlock where the kernel supports it | Same deny list. Proving it on Linux is a Phase 2 spike item. If it fails, shell-capable roles are `BLOCKED(no_agent_boundary)`, as on macOS |
| Service manager (Phase 10) | `launchd` user agent | `systemd --user` unit | Optional |
| Notifications (Phase 9) | macOS notifications | freedesktop notifications | Optional |

- **Windows** is not supported natively. WSL2 may come later; it would use the Linux path.
- CI runs the platform-independent test suite on both `macos-latest` and `ubuntu-latest`. The tests specific to each platform's sandbox and secret store run on the matching runner.

## Consequences
- The Phase 1 config schema includes the registry. The policy engine checks roles against registry entries, not provider names.
- Phase 2 builds adapters from manifests and adds one `openai_compatible_http` reviewer as proof that local models work. It also runs the `credential_isolation` and Codex `--oss` spikes. The Linux `AgentBoundary` spike sits next to the Seatbelt spike.
- Phase 4 adds the Linux disk-reservation check. The VM boundary and its checks stay the same.
- Phase 10 adds `atlas agents add | test | list` and `atlas secrets set`, plus a Linux service unit.
- A user with only local models can fill every role, provided the registry holds at least two model families, e.g. Qwen as developer and Llama as reviewer and QA. ADR-005 enforces this.
- The QA role may be any agent whose manifest allows `qa`, or the generic `manual_handoff`. The minimum QA gate (roadmap Phase 4) works with any of them. Antigravity is one manual-handoff profile, and it stays in the recommended profile. D4 still applies: an assignment is never changed while a handoff is pending.
- More combinations of providers, models and operating systems means more to test. Only `verified` manifests load.
