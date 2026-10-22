# Agent Compatibility Report

Verified on **2026-10-22** on macOS 26.6.2 (arm64). Raw `--help` output and versions are in [`evidence/`](evidence/).
This matrix holds three kinds of statement:
- **Verified**: confirmed in official docs or `--help` output, with the source cited.
- **Inferred**: a reasoned conclusion.
- **To verify**: must be confirmed by a Phase 2 spike before the adapter relies on it.

This report covers the three **built-in, verified** adapter manifests (ADR-007 §3). Other agents are added through the registry and manifest process in §6. ATLAS re-probes versions and flags at startup (§5), because these CLIs update themselves. `agy` moved from 1.4.2 to 1.4.5 *during* this Phase 0 session.

## 1. Summary matrix

| Capability | Claude Code (`claude` 2.1.308) | Codex (`codex-cli` 0.163.0) | Antigravity (`agy` 1.4.5) |
|---|---|---|---|
| Installed / authenticated | ✅ / ✅ (subscription login) | ✅ / ✅ `codex login status` → "Logged in using ChatGPT" (upgraded to 0.163.0 by the user during Phase 0; see §3.2a) | ✅ / ✅ (`agy models` lists models) |
| `interactive_execution` | ✅ | ✅ | ✅ (performed by the user) |
| `headless_execution` | ✅ `-p` [H] | ✅ `codex exec` [N] | ⚠️ exists (`-p`) [A1] but **not used by ATLAS** |
| `file_modification` | ✅ via permission mode / allowlist [H] | ✅ `-s workspace-write` [N] | n/a (manual) |
| `read_only_review` | ✅ `--permission-mode dontAsk` + read-only tools, or `--restricted` [help] | ✅ `-s read-only` (default) [N] | manual |
| `structured_output` | ✅ `--output-format json` + `--json-schema` → `structured_output` [H] | ✅ `--output-schema <file>`, `-o <file>`, `--json` JSONL [N] | manual: `result.schema.json` validated on import |
| `cancellation` | ✅ SIGINT ends the turn; SIGTERM → exit 143, turn left unfinished [H] | ⚠️ SIGINT/SIGTERM (exit semantics to verify) | n/a |
| `resume` | ✅ `--session-id`, `--resume <id>` [H] | ✅ `codex exec resume <id>` [help] | n/a |
| `capacity_signal` | ✅ `system/api_retry` events with `error` ∈ {`rate_limit`, `authentication_failed`, `billing_error`, `overloaded`, …} in stream-json [H] | ⚠️ undocumented for exec; classify from exit code + stderr (to verify) | n/a |
| `supported_subscription_auth` | ✅ for the user's own use of the unmodified binary [L] | ✅ "advanced" path for trusted runners; not for public/OSS repos [N] | ✅ for the user's own interactive use |
| `supported_third_party_orchestration` | ⚠️ Each operator runs their own copy with their own login (self-hosted, single-operator; ADR-007 §1). Never offered as a service to others; no credential handling [L] | ⚠️ Trusted local runner, private repos [N] | ❌ **Treated as not permitted.** Terms §6 [T] |
| **ATLAS integration mode** | `headless` | `headless` (private repos) / `blocked` (public repos; no override in this release, D5) | `manual_handoff` |

Sources:
- [L] https://code.claude.com/docs/en/legal-and-compliance
- [H] https://code.claude.com/docs/en/headless
- [P] https://code.claude.com/docs/en/permissions
- [N] https://learn.chatgpt.com/docs/non-interactive-mode (redirected from developers.openai.com/codex/noninteractive)
- [A1] https://antigravity.google/docs/cli/headless/
- [T] https://antigravity.google/terms
- [help] captured `--help` in `evidence/`

## 2. Claude Code

### 2.1 Terms (quoted from [L])
- "OAuth authentication is intended exclusively for purchasers of Claude Free, Pro, Max, Team, and Enterprise subscription plans and is designed to support ordinary use of Claude Code and other native Anthropic applications."
- "Anthropic does not permit third-party developers to offer Claude.ai login into their own applications, or to route requests through Free, Pro, or Max plan credentials on behalf of their users. Moreover, developers may not collect, store, or intermediate Claude.ai credentials or session tokens."
- "Nor does it prevent an end user from signing in to the unmodified Claude Code binary with their own Claude subscription…"
- "Advertised usage limits for Pro and Max plans assume ordinary, individual usage of Claude Code and the Agent SDK."

**Assessment (inferred):** ATLAS is used by its author, on their own machine, with their own subscription. It invokes the unmodified `claude` binary and never reads or stores tokens. That fits "an end user signing in to the unmodified Claude Code binary". ATLAS **must not** be distributed as a hosted service that uses others' subscriptions, and it **must not** read `~/.claude` credentials or the keychain entries. Autonomous loops may consume a Pro allowance faster than "ordinary, individual usage". ATLAS therefore defaults to one Claude invocation at a time and pauses on `rate_limit`.

### 2.2 Gotchas found during verification
- **`--bare` cannot be used.** "In bare mode, Claude Code never reads OAuth credentials or the system keychain" [H]. Bare mode would require an API key, which is out of scope.
- **Without `--bare`, `claude -p` trusts repository content.** In a never-trusted folder, a `-p` run *uses* the project's settings-file hooks, its `env` block and helper commands, and it *connects* `.mcp.json` servers "without asking" [P]. A malicious target repo could therefore execute code on the host. **Mitigation, as documented in [P]:**
  - `--setting-sources user`: reads neither the project's settings files nor its `.mcp.json`.
  - `--settings '{"disableAllHooks": true}'`: this must be a flag. The user setting alone is not enough, because project settings can override it.
  - `--strict-mcp-config --mcp-config '{"mcpServers":{}}'`.
  - To verify: whether `--setting-sources user` also suppresses project `CLAUDE.md`, skills and agents. If it does not, they are still only *prompt* content, and §security-model prompt-injection rules apply.
- `--fallback-model` silently substitutes a model. **ATLAS never passes it.** This is a brief requirement: no silent substitution.
- Since v2.1.302, `-p` starts in Manual mode, so unapproved tool calls are denied [H]. `--permission-prompts none` stops Claude retrying denied calls [H].

### 2.3 Command templates (only flags present in `evidence/claude-help.txt` or [H])

Common prefix (`CLAUDE_BASE`):
```
claude -p
  --output-format stream-json --verbose
  --setting-sources user
  --settings '{"disableAllHooks": true}'
  --strict-mcp-config --mcp-config '{"mcpServers":{}}'
  --permission-prompts none
  --session-id <uuid>
  --model <pinned model alias from config>
```

| Role | Additional flags | cwd |
|---|---|---|
| Planner / architect (read-only) | `--permission-mode dontAsk --allowedTools "Read,Grep,Glob" --json-schema '<serialized plan schema>'` | a detached read-only worktree at base SHA |
| Code reviewer (read-only) | `--permission-mode dontAsk --allowedTools "Read,Grep,Glob,Bash(git diff *),Bash(git log *),Bash(git show *)" --json-schema '<serialized review schema>'` | phase worktree at the candidate commit |
| Developer (only if assigned) | `--permission-mode acceptEdits --allowedTools "Read,Edit,Write,Grep,Glob"` (no Bash until Claude's OS sandbox is verified, see security-model.md §5.2) | phase worktree |

`--json-schema` takes the schema **text**, not a path (`evidence/claude-help.txt`: `--json-schema <schema>` with an inline example), unlike `--settings` and `--mcp-config`, which accept files. ATLAS loads the schema file, validates it, and passes `json.dumps(schema)` as a single argv element (ATLAS-016). Phase 2 records a fixture of a successful structured-output run using exactly this template.

The prompt is passed on **stdin** (it is under the 10 MB cap), never interpolated into argv. Exit code: 0 means success; non-zero means failure, with the result on stdout [H]. `structured_output` may be absent, which counts as a failure to produce a result and is never treated as success [H].

### 2.4 Capacity classification
Classification parses `system/api_retry` events and the final `result` [H]:
- `rate_limit` → RATE_LIMITED
- `authentication_failed` or `oauth_org_not_allowed` → AUTHENTICATION_REQUIRED
- `billing_error` or `account_on_hold` → QUOTA_EXHAUSTED (pause; never fall back to the API)
- `overloaded` or `server_error` → PROVIDER_UNAVAILABLE
- anything else → UNKNOWN

To verify: the exact final-result shape when a Pro usage window is exhausted, captured by a Phase 2 spike and recorded as a fixture.

## 3. OpenAI Codex

### 3.1 Terms and docs (quoted from [N])
- "`codex exec` reuses saved CLI authentication by default."
- "API keys are the right default for automation…"
- The ChatGPT sign-in path is for "users who need ChatGPT/Codex rate limits instead of API key usage". It is limited to trusted runners: "Do not use this workflow for public or open-source repositories."
- "Treat `~/.codex/auth.json` like a password: it contains access tokens."
- "By default, `codex exec` runs in a read-only sandbox." "`--full-auto` is no longer supported."

**Assessment:** ATLAS on the user's own Mac is a trusted runner, which makes this path supported for **private** repositories. For a **public** repository, ATLAS marks the Codex adapter `blocked: public_repository`. **No override exists in this release** (D5: the user works only on private repos; reconsider if a public repo is ever registered). ATLAS never reads `~/.codex/auth.json`.

### 3.2 Command templates (flags present in `evidence/codex-exec-help.txt`)

```
codex exec
  -C <worktree>
  -s read-only | workspace-write
  --json
  --output-schema <schema.json>
  -o <artifact_dir>/last_message.json
  --ignore-user-config      # do not load ~/.codex/config.toml; "auth still uses CODEX_HOME" (help text)
  --ignore-rules            # do not load user or project execpolicy .rules files
  -m <pinned model from ATLAS config>
  -c approval_policy="never"
  -c <further explicit pins decided in the Phase 2 spike, e.g. MCP servers empty, web search off>
  -                          # prompt read from stdin
```

ATLAS-006: inheriting the user's personal Codex config could add providers, MCP servers or relaxed settings. All settings ATLAS depends on are therefore pinned on the command line. The exact `-c` keys are confirmed against the installed version in the Phase 2 spike. The effective config is asserted by hostile-config tests.

| Role | Sandbox |
|---|---|
| Plan challenger / architecture challenger | `-s read-only` |
| Developer / test author / bug fixer | `-s workspace-write` (writes limited to the workspace; network policy per Codex defaults, to verify) |
| Code reviewer (when assigned) | `-s read-only`, or `codex exec review --base <base>` (help confirms `--base`, `--commit`, `--uncommitted`) |

Never passed:
- `--dangerously-bypass-approvals-and-sandbox`
- `--dangerously-bypass-hook-trust`
- `-s danger-full-access`
- `--oss` / `--local-provider` **for agents with `billing: subscription` or `api_key`**

Per-billing Codex policy (ADR-007):

| Billing | `--oss` | Auth | Public repos (D5) |
|---|---|---|---|
| `subscription` | never | ChatGPT sign-in managed by the CLI | blocked |
| `api_key` | never | key only after `credential_isolation: verified` (Phase 2 spike); until then `BLOCKED(api_key_not_isolated)` | blocked |
| `local` | **required**, with `--local-provider` and the endpoint pinned; loopback checked before launch | none | blocked (D5 unchanged; narrowing it needs a new decision) |

Each billing mode has its own hostile-config fixture.

To verify in the Phase 2 spike:
- Whether project `AGENTS.md` and `.codex/config.toml` in the worktree are loaded. If so, they are treated as prompt content and must not be able to change sandbox or approvals.
- Network access under `workspace-write`.
- Exit codes and stderr text for usage-limit exhaustion and expired login.

### 3.2a Observed failure: model not available on ChatGPT plan (2026-10-22)
The first real `codex exec` run, which was the Phase 0 review, failed with HTTP 400 `invalid_request_error`:
"The 'gpt-5.5' model is not supported when using Codex with a ChatGPT account." The retry with `-m gpt-5.4` failed the same way. The raw events are in `review/codex-phase0-r0-failed-gpt-5.5.events.jsonl` and `review/codex-phase0-r1.events.jsonl`.

The installed CLI was 0.142.4, and the latest is 0.163.0. Lessons for the adapter:
- `turn.failed` containing `not supported when using Codex with a ChatGPT account` maps to `PROVIDER_UNAVAILABLE(reason=model_not_entitled)`. It is **not** retried and **not** auto-substituted with another model.
- The `--json` event stream exposes `thread.started.thread_id` (the session id) and `turn.failed.error.message`, which are the classification inputs.
- The process exit code was 1 in this case.
- The startup probe should flag a CLI that is far behind the latest release.

After the user upgraded Codex to 0.163.0, `codex exec` with the CLI default model succeeded. The model catalog changed completely between versions, which shows that the model choice must be read from the installed CLI and never hard-coded.

### 3.3 Capacity classification
Classification is based on observed exit code and stderr, with patterns kept in a versioned fixtures file. Unknown failure modes map to UNKNOWN with conservative backoff. Auth probe: `codex login status`, which is cheap and makes no model call.

## 4. Google Antigravity

### 4.1 Terms (quoted from [T] §6)
- "You must not abuse, harm, interfere with, or disrupt the Service. This includes, but is not limited to, using the Service in connection with products not provided by us."
- "Using third party software, tools, or services to access the Service (e.g. using OpenClaw with Antigravity OAuth)" is a breach. It "may be grounds for suspension or termination of your Antigravity and/or Gemini CLI accounts."

The headless docs [A1] describe `agy -p` as a way to "script agent tasks, integrate with CI pipelines". Community forum answers say that local subprocess use is fine, but **no Google-authored statement** confirms that for consumer AI Pro accounts. There are also known headless defects: GitHub issue #318 (print mode hangs without a TTY) and #548 (`permissions.allow` ignored in print mode).

**Decision (ADR-003, user-confirmed):** ATLAS does not spawn `agy`. The Antigravity adapter is a `ManualHandoffAdapter`:
- `check_availability()` reports `MANUAL_ONLY`.
- `execute()` writes a handoff packet and returns `AWAITING_MANUAL`.
- `collect_result()` validates an imported `result.json`.

Full runbook: [antigravity-manual-handoff.md](antigravity-manual-handoff.md).

**Re-evaluation trigger:** the decision is revisited if Google publishes an explicit statement allowing local programs to invoke `agy` under consumer plans. Adding a headless Antigravity adapter would then be a new, separately reviewed phase.

### 4.2 Model-family pitfall
`agy models` lists Gemini, **Claude Opus/Sonnet** and GPT-OSS models (`evidence/agy-models.txt`). For independence, the handoff instructs the user to select a **Gemini** model. The imported result must declare `model`. Results declaring a model in the same family as the author are rejected for independent review (ADR-005).

## 5. Adapter interface

```python
class AgentAdapter(Protocol):
    agent_id: str                            # registry id from ~/.atlas/agents.yaml (ADR-007 §2)
    adapter_kind: AdapterKind                # cli | openai_compatible_http | manual_handoff
    manifest: AdapterManifest | None         # for cli: argv templates, parser, failure fixtures, probes
    provider: str                            # derived inference provider: anthropic | openai | google | local | <remote host>
    async def check_availability(self) -> AvailabilityReport      # binary present, version, capacity state
    async def check_authentication(self) -> AuthStatus            # cheap, no model call where possible
    async def get_capabilities(self) -> Capabilities              # from this file, version-gated
    async def execute(self, req: ExecutionRequest) -> ExecutionHandle   # returns immediately; supervisor owns the process
    async def cancel(self, execution_id: str) -> CancelResult
    async def collect_result(self, execution_id: str) -> ExecutionResult  # typed; includes evidence label
```

**Startup capability probe.** For each `cli` agent, ATLAS records `--version` and hashes the `--help` output. If a flag used by a template is missing from the new help text, the adapter goes to `PROVIDER_UNAVAILABLE(reason=cli_incompatible)`. ATLAS does not guess at the change.

**Version pinning.** Config may state a `min_version` and a `max_tested_version`. If the installed version is above `max_tested_version`, ATLAS shows a warning badge. Strict mode blocks the adapter until the fixture tests pass.

Tests use `FakeAdapter` implementations that replay recorded fixtures: success, malformed JSON, timeout, crash, rate_limit, auth_expired.

## 6. Adding other agents and local models (ADR-007)

### 6.1 Adapter manifest (for `cli` agents)
A manifest is data that the generic `cli` adapter reads. It cannot add code. Fields:

| Field | Purpose |
|---|---|
| `binary`, `version_cmd`, `min_version`, `max_tested_version` | Capability probe (§5) |
| `roles` | Roles the manifest supports. Each maps to an argv template (no shell; placeholders such as `{worktree}`, `{schema_path}`, `{prompt_file}`, `{model}` filled with allowlisted values) |
| `required_flags` | Flags that must appear in `--help`; missing → `PROVIDER_UNAVAILABLE(cli_incompatible)` |
| `isolation_flags` | Flags that stop repo/user config from loading hooks, MCP servers or providers. Each needs a hostile-config fixture |
| `output` | `stream-json` \| `jsonl-events` \| `final-json-file`, plus the JSON path of the structured result |
| `failure_patterns` | Exit-code/stderr regex → capacity state, in a versioned fixtures file |
| `auth_probe` | Cheapest command that reports login state without a model call where possible |
| `env` | Extra non-secret vars and, for `billing: api_key`, the one variable that receives the agent's key |
| `credential_isolation` | `verified` only if a fixture proves agent-launched commands don't inherit the key; required for `billing: api_key` |
| `redaction_patterns` | Extra token formats to redact |

Only `verified` manifests load. A user-added manifest is refused. A manifest becomes `verified` only through a reviewed ATLAS PR that adds recorded fixtures (success, malformed output, timeout, crash, rate limit, auth expired) and passes the hostile-repo and hostile-config tests. Each new CLI also needs a terms check like the one in §2–§4 before it ships as `verified`.

### 6.2 Local and open-weight models
- **Write roles:** use an agent CLI that can drive a local model. Two examples are Codex with `--oss --local-provider ollama|lmstudio` (flags verified in `evidence/codex-exec-help.txt`; effective config, model selection and endpoint override to verify in Phase 2) and OpenCode (a candidate manifest). The Codex public-repo block (D5) applies to every Codex agent, whatever its billing (§3.2 table).
- **Read-only roles:** `openai_compatible_http` sends one `POST /v1/chat/completions` request.
  - The request asks for a structured output (`response_format: json_schema`). If the server doesn't support that, ATLAS uses JSON mode and validates the reply strictly.
  - No `tools` or functions are sent.
  - Timeouts and HTTP 429/5xx map to capacity states, and an endpoint that can't be reached maps to `PROVIDER_UNAVAILABLE`.
  - Servers with this API include Ollama, LM Studio, vLLM and llama.cpp `llama-server`.
- Endpoints must be loopback unless the agent has `allow_remote_endpoint: true` (ADR-007 §4).
- The model family comes from ADR-005's map, or else from the declared `model_family`.
