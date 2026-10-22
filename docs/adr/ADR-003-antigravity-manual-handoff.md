# ADR-003: Google Antigravity is integrated via user-run manual handoff only

- Status: Accepted by user on 2026-10-22, pending Codex challenge
- Date: 2026-10-22

## Context
- The [Antigravity terms](https://antigravity.google/terms) §6 say users must not use the Service "in connection with products not provided by us". They also call "using third party software, tools, or services to access the Service" a breach, which can lead to suspension.
- The [Headless docs](https://antigravity.google/docs/cli/headless/) document `agy -p` for scripting and CI. Google has not stated that a *third-party local orchestrator* may invoke it on a consumer AI Pro account. Forum replies saying it is fine come from unverified community members.
- The forum shows users reporting 403 ToS suspensions after using third-party tools.
- `agy` print mode has open bugs: a non-TTY hang (#318) and `permissions.allow` being ignored in print mode (#548).

## Decision
- ATLAS never spawns `agy`, never automates the Antigravity IDE, and never touches Antigravity credentials.
- Antigravity steps produce a handoff packet. The user runs Antigravity personally and imports a schema-validated `result.json`. See [antigravity-manual-handoff.md](../antigravity-manual-handoff.md).
- Imported results carry the evidence label `manual_imported`. They never count as `executed`.

## Consequences
- QA through Antigravity needs the user to be present. The Definition of Done reports this limitation explicitly.
- Fallback: **none in this release** (decision D4 = wait). The phase stays AWAITING_MANUAL with reminders. Reassignment to another agent would need a new decision.
- **Revisit when** Google publishes explicit permission for local programmatic invocation on consumer plans. Any future headless adapter will be a separately challenged phase.
