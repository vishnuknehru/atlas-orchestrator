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
    adapter: claude_cli           # a vetted adapter manifest (§3)
    models: [claude-opus-5-5]     # pinned explicitly (D10); no fallback models
    billing: subscription         # subscription | api_key | local   (D15)
  codex:
    adapter: codex_cli
    models: [<model id>]
    billing: subscription
  antigravity:
    adapter: manual_handoff
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
- `agents.yaml` is edited only through the dashboard or `atlas agents …` (Phase 10). Like project config, a repository can never supply or change it.

### 3. Adapter manifests (declarative, vetted)
Each adapter `kind` is ATLAS code: `cli`, `openai_compatible_http` or `manual_handoff`. A **manifest** fills in the per-tool details for a `cli` adapter:
- the argv template for each role, with no shell, using the same placeholder substitution and allowlisted values as the existing templates;
- the output format and the parser to use (`stream-json`, `jsonl-events`, a final JSON file);
- regex fixtures that map exit codes and stderr to capacity states;
- the auth probe, the version probe and the flags the capability probe must find;
- the env variables the tool needs. They come from the allowlist plus its own credential (§5), never the user's whole environment;
- the config-isolation flags, for example Codex `--ignore-user-config` and Claude `--setting-sources user`. Each must have a hostile-config fixture test (security-model.md §3.2).

Trust levels:

| Level | Meaning | Can be assigned to |
|---|---|---|
| `verified` | Manifest shipped in the ATLAS repo, with recorded fixtures and passing hostile-repo / hostile-config tests | Any role allowed by the manifest |
| `unverified` | Added by the user locally; the capability probe passes, but there are no fixture tests in the ATLAS repo | Read-only roles (challenger, reviewer) only; every gate result is labelled `unverified_agent` |

- An `unverified` manifest can never get a write role (architect output is read-only and counts as a read role; developer and fixer are write roles). A tool becomes `verified` through a reviewed PR to ATLAS.
- There is no "run this command" or free-form shell adapter.
- Built-in manifests in the first release: `claude_cli`, `codex_cli`, `manual_handoff`. Candidates added through the normal phase process include OpenCode, Aider, Goose and Gemini CLI. Each needs its own terms check, like ADR-003 and ADR-004.

### 4. Local and open-weight models (D14)
- **Write roles (developer, fixer)** use an *agent CLI* that supports local models, for example Codex with a local or OSS provider, or OpenCode. ATLAS does **not** implement its own tool-calling agent loop. That would be a new agent with a large attack surface, outside ATLAS's job.
- **Read-only roles (challenger, reviewer)** may use `openai_compatible_http`. ATLAS POSTs one chat-completion request:
  - the request has a structured-output schema;
  - the context is ATLAS-assembled: the candidate diff and the plan, fenced as `UNTRUSTED`;
  - there are no tools and no function calling;
  - the reply is validated against the findings schema, just like CLI output. The evidence label is `agent_reported`.
- **Endpoints are loopback-only by default** (`127.0.0.1`, `::1`, or a Unix socket). Any other host needs `allow_remote_endpoint: true` on that agent. The dashboard then shows that source code leaves the machine.
- Local models have no quota; their capacity states come from HTTP status and timeouts.
- A local model may be much weaker than a frontier model. Gate results show the reviewer's model, so the user can judge.

### 5. Billing and credentials (D15)
- `billing: subscription` keeps ADR-004 unchanged. The CLI owns its login, and ATLAS strips every provider API-key variable.
- `billing: api_key` is an **explicit, per-agent opt-in**:
  - the user stores the key with `atlas secrets set <agent_id>`;
  - ATLAS puts it in the OS secret store (§6) under `atlas.agent.<agent_id>`;
  - the key is injected only into that agent's own subprocess, under the variable named in its manifest. Every other agent and every test, git and gh process still has all key variables stripped.
  - Redaction already covers `sk-…` patterns. The manifest may add more patterns.
- `billing: local` passes no credential. An optional bearer token for a local server is handled like an API key.
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
| Reserved disk for the sandbox VM | Dedicated APFS volume, reserve = quota | Dedicated partition or LVM logical volume of fixed size, checked with `findmnt` / `df` | Same "reserved, not observed" rule (security-model.md §5.1) |
| `AgentBoundary` (outer wrapper for agent processes) | Seatbelt (`sandbox-exec`) | bubblewrap, with Landlock where the kernel supports it | Same deny list. Proving it on Linux is a Phase 2 spike item. If it fails, shell-capable roles are `BLOCKED(no_agent_boundary)`, as on macOS |
| Service manager (Phase 10) | `launchd` user agent | `systemd --user` unit | Optional |
| Notifications (Phase 9) | macOS notifications | freedesktop notifications | Optional |

- **Windows** is not supported natively. WSL2 may come later; it would use the Linux path.
- CI runs the platform-independent test suite on both `macos-latest` and `ubuntu-latest`. The tests specific to each platform's sandbox and secret store run on the matching runner.

## Consequences
- The Phase 1 config schema includes the registry. The policy engine checks roles against registry entries, not provider names.
- Phase 2 builds adapters from manifests and adds one `openai_compatible_http` reviewer as proof that local models work. The Linux `AgentBoundary` spike sits next to the Seatbelt spike.
- Phase 4 adds the Linux disk-reservation check. The VM boundary and its checks stay the same.
- Phase 10 adds `atlas agents add | test | list` and `atlas secrets set`, plus a Linux service unit.
- A user with only local models can fill every role, provided the registry holds at least two model families, e.g. Qwen as developer and Llama as reviewer and QA. ADR-005 enforces this.
- The QA role may be any agent whose manifest allows `qa`, or the generic `manual_handoff`. The recommended profile keeps Antigravity manual handoff. D4 still applies: an assignment is never changed while a handoff is pending.
- More combinations of providers, models and operating systems means more to test. Only `verified` manifests are supported. `unverified` ones are best-effort and limited to read roles.
