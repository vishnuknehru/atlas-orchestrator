# Decisions update (D3–D12) — responses to Codex review

Author: Claude Code. Reviewer: Codex CLI 0.163.0, read-only. Raw outputs are stored next to this file.

User decisions (2026-10-22):
- **D3:** use the user's PAT. Verification is pending; the user runs `verify-github-pat.sh`, which never prints the token.
- **D4:** (a) wait.
- **D5:** blocked; the user works only on private repos.
- **D6:** auto-merge off.
- **D7:** the user approves every plan.
- **D9:** acknowledged.
- **D11:** Docker containers for tests.
- **D12:** no checkpoint encryption.

## Review D1 — `codex-decisions-d1.json` — verdict `changes_required`, 12 findings (10 HIGH, 2 MEDIUM)

All accepted.

| ID | Sev | Change made |
|---|---|---|
| ATLAS-D01 | HIGH | Source mounted **read-only** in setup and test containers. Writable paths are declared separately and may not shadow tracked files. Source-mutating projects are unsupported. security-model.md §5.1.3 |
| ATLAS-D02 | HIGH | Full NUL-delimited manifest (path, type, git mode, blob id) computed **in the VM** by the helper with lstat/readlink. Tracked entries always compared. Case collisions rejected at export. §5.1.2–5.1.3 |
| ATLAS-D03 | MEDIUM | Export built directly from git objects (no `git archive`, no attributes or filters). Submodules, LFS and escaping or absolute symlinks are blocked. §5.1.2 |
| ATLAS-D04 | HIGH | Only `/out` is collected, after all containers are confirmed stopped. Streamed tar → `tarfile` data filter plus regular-files-only, size/count/deadline limits and `O_NOFOLLOW` extraction. `/work` is never copied to the host. §5.1.5 |
| ATLAS-D05 | HIGH | An outer Seatbelt wrapper around **agent processes** denies the Colima/Docker sockets and `~/.atlas`. Phase 2 spike test. Shell-capable agent roles are BLOCKED without it. §5.1.9; roadmap 2.1; R25 |
| ATLAS-D06 | HIGH | Explicit `atlas` daemon endpoint. create → inspect → verify HostConfig → start. Per-execution attestation (daemon/VM boot ID, no VM host mounts). A trusted canary proves only `lo` exists before any repo code runs. §5.1.4 |
| ATLAS-D07 | HIGH | Container and volume names persisted before create. Daemon-side kill/wait/inspect on timeout, pause, cancel and restart. Unreachable daemon → BLOCKED(sandbox_state_unknown). §5.1.7; recovery-strategy.md §2.2, §3 |
| ATLAS-D08 | HIGH | tmpfs sizes, log limits, polled volume usage with kill, collection limits, size-capped dedicated VM disk. §5.1.8 |
| ATLAS-D09 | HIGH | Tests always `--network none` (`tests.network` unavailable). Setup egress only through an allowlist proxy on an internal network that denies host, gateway, LAN and link-local, verified by the canary. VM-escape exposure restated accurately (R16). §5.1.6 |
| ATLAS-D10 | MEDIUM | Repository Dockerfile builds disabled. Only user-approved, digest-pinned publisher images, with provenance recorded. §5.1.1 |
| ATLAS-D11 | HIGH | Schema rejects `auto_merge: true` and `execution.mode: autonomous`. G-MERGE-PRE requires a candidate-bound user approval only. The FSM's auto-merge edge is removed, and plan approval is always human. FSM; github-integration.md §2–3; architecture.md §8 |
| ATLAS-D12 | MEDIUM | D3: PAT from Keychain, verified before use, **no** keyring fallback. D4: reassignment removed from the runbook, ADR-003, roadmap and risks. D5: no public-repo override in this release |

## Review D2 — `codex-decisions-d2.json` — verdict `changes_required`: D01–D07, D09, D10 and D12 confirmed resolved; D08 and D11 reopened; 2 new

| ID | Sev | Change made |
|---|---|---|
| ATLAS-D08 (reopened) | HIGH | Per-execution **fixed-size, preallocated ext4 loop filesystems** created by a trusted provisioner, so ENOSPC is confined to the execution. Host disk headroom is checked before provisioning (`BLOCKED(insufficient_disk)`). Defined recovery for a full VM disk. security-model.md §5.1.8 |
| ATLAS-D11 (reopened) | MEDIUM | §4 destructive-ops table says auto-merge is "not available in this release". The pending-merge lapse list no longer mentions turning auto_merge off; it covers GitHub-side queued/armed merges only. Roadmap wording fixed |
| ATLAS-D21 | HIGH | Writable destinations are normalised and rejected if any component is a symlink, or if they equal, contain or are an ancestor of a tracked path. Attestation also walks the **effective mount view** in a canary with the identical mount set. Adversarial `alias -> src` fixture. §5.1.3 |
| ATLAS-D22 | MEDIUM | Two canary contracts: a test canary (`lo` only) and a setup canary (single interface on the internal egress network, proxy-only routing, direct registry and host/LAN/link-local denied), run before any installer. §5.1.4 |

## Review D3 — `codex-decisions-d3.json` — verdict `changes_required`: D11, D21 and D22 confirmed resolved; D08 reopened; 1 new

These fixes were made after D3 and are **not yet independently verified**. That was the third cycle, so the next step is escalated to the user.

| ID | Sev | Change made |
|---|---|---|
| ATLAS-D08 (reopened) | HIGH | Host capacity is **reserved**: a dedicated APFS volume with `-reserve` = `-quota` holds the Colima VM data (`COLIMA_HOME`), verified before admitting any execution. `~/.atlas` lives elsewhere. Host exhaustion by other processes is detected separately (`BLOCKED(host_disk_low)`) from guest ENOSPC. security-model.md §5.1.8; D11; roadmap Phase 4 |
| ATLAS-D31 | MEDIUM | Two attestation rules. The source volume needs exact manifest equality. The effective view needs all tracked entries to match, with any content allowed at or below validated writable mountpoints and nothing extra elsewhere. A populated-dependency fixture must pass. §5.1.3 |

## Review D4 (verification-only, user-approved) — `codex-decisions-d4.json` — verdict **`approved`**

"Both findings are fully resolved … No new CRITICAL or HIGH defect introduced by these fixes was identified. This verifies the documented architecture, not implementation."

**Status:** 20 findings across D1–D4 (including reopened ones), all accepted, none open. D3 (PAT) is still pending the user's verification-script output.
