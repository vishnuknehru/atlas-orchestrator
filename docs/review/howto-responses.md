# How-to guide — responses to Codex review

## Review H1 — `codex-howto-h1.json` — verdict `changes_required`, 8 findings (2 HIGH, 6 MEDIUM)

All 8 accepted.

| ID | Sev | Change made |
|---|---|---|
| ATLAS-H01 | HIGH | The guide states that ATLAS reads only the per-project PAT and passes it only to its own git/gh processes. Agent-tool credentials are never read |
| ATLAS-H02 | HIGH | Public repos are described as blocked pending a new decision, not as a supported Claude + Antigravity workflow |
| ATLAS-H03 | MEDIUM | Restart text covers manual Resume before Phase 6 and the optional `recovery.auto_resume` (off by default) from Phase 6 |
| ATLAS-H04 | MEDIUM | Added the "merge on GitHub yourself" path (unknown protection, merge queues) with ATLAS's head verification. GitHub approvals still apply |
| ATLAS-H05 | MEDIUM | Phase tags corrected: protection discovery P5, test config P4, notifications P9 |
| ATLAS-H06 | MEDIUM | New §1.5 lists the project compatibility limits before registration |
| ATLAS-H07 | MEDIUM | Packet expiry (72 h) and regeneration explained, separately from the indefinite workflow wait |
| ATLAS-H08 | MEDIUM | The Docker guarantee is scoped to tests and setup. Agents in host worktrees and the D9 trust are disclosed. Approved registries listed as an egress destination |

## Review H2 — `codex-howto-h2.json` — verdict `changes_required`: H01–H08 confirmed resolved; 3 new MEDIUM

All accepted and fixed. These fixes are not re-verified; the policy blocks only on CRITICAL or HIGH, and none are open.

| ID | Sev | Change made |
|---|---|---|
| ATLAS-H11 | MEDIUM | The config example keeps only approved keys (`writable_paths`). Test setup is described as behaviour, with key names to be decided in Phase 4 |
| ATLAS-H12 | MEDIUM | The manual-merge check is the PR **head at merge** == candidate (plus repo and base), with the squash merge commit recorded and its reachability verified |
| ATLAS-H13 | MEDIUM | Added the post-merge base-CI check, your rollback decision, the revert phase through the full pipeline, and the effect on dependent phases |

## Review H3 — `codex-howto-h3.json` (step-by-step rewrite) — verdict `changes_required`, 5 MEDIUM

All accepted and fixed.

| ID | Sev | Change made |
|---|---|---|
| ATLAS-H21 | MEDIUM | The sandbox guarantee in Step 7 is scoped to tests and dependency setup, with a pointer to the host-worktree caveat for developer agents (Part 8) |
| ATLAS-H22 | MEDIUM | Step 3 installs, checks and signs in to each agent tool (setup A), or installs a model server, pulls two families and checks it answers (setup B) |
| ATLAS-H23 | MEDIUM | The PAT check script is described as part of D3 but not yet published; ATLAS checks the token at registration |
| ATLAS-H24 | MEDIUM | Step 13 is split into "on your computer [P4]" and "on GitHub [P5]" |
| ATLAS-H25 | MEDIUM | Step 14 uses a `$PKT` packet-folder variable for `PROMPT.md` and `result.json`, matching the runbook |

## Review H4 — `codex-howto-h4.json` — verdict `approved`

H21–H25 confirmed resolved; no new findings.
