# AgentBoundary spike (macOS Seatbelt), 2026-10-26

Profile: `backend/src/atlas/agents/boundary.py`. Default allow, then deny reads of `~/.atlas`, `~/.colima`, `~/.docker`,
`~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.config/gh` and the Docker sockets; deny all writes except the agent's worktree,
scratch space and `~/.claude`, `~/.codex`, `~/.cache`; deny outbound connections to unix sockets under the denied paths.
Network to the provider stays open.

| Check | Result |
|---|---|
| Command writes inside the worktree and `$TMPDIR` | allowed |
| Command writes elsewhere in `$HOME`, another project dir, `~/.atlas` | denied |
| Command reads `~/.atlas/session_token`, `~/.ssh/*` | denied |
| Command connects to a unix socket under `~/.colima` | denied (control: connects outside the sandbox) |
| `claude -p` read-only (Claude Code, subscription login), tiny prompt | works |
| `codex exec -s read-only`, tiny prompt | works |
| `claude -p --permission-mode acceptEdits --allowedTools Read,Edit,Write,Grep,Glob`, creates a file in the worktree | works |
| `codex exec -s workspace-write` creates a file in the worktree, **without** AgentBoundary | works |
| same, **inside** AgentBoundary | **fails**: Codex reports "the environment blocked filesystem writes" (its own sandbox cannot be applied inside ours) |

Consequence: Codex stays read-only (planning, challenge, review). Developer work needs Claude Code until a separate decision
on running Codex without its own sandbox inside ours. That trade-off matters: Codex's sandbox also blocks network for the
commands it runs, ours does not. Not enabled.

Not yet verified: Linux (bubblewrap/Landlock), Claude with Bash under its own sandbox, API-key credential isolation.
