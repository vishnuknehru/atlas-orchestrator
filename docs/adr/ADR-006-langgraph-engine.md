# ADR-006: How ATLAS uses LangGraph safely

- Status: Proposed, revision 2 (after Codex review LG-1, findings ATLAS-L01..L07)
- Date: 2026-10-22
- Depends on: ADR-001 (decision to use LangGraph), ADR-002 (SQLite + outbox)

## 1. Scope of use
- Packages: `langgraph` and `langgraph-checkpoint-sqlite` (`AsyncSqliteSaver`), plus whatever they require transitively (e.g. `langchain-core`), which is recorded and audited.
- **Not used:** LangGraph Platform/Server, Studio, LangSmith, `langchain-*` model or tool integrations, LangGraph `Store`, `BaseCache`, pickle.
- `LANGSMITH_TRACING`, `LANGCHAIN_TRACING_V2` and `LANGSMITH_API_KEY` are forced unset or false. A test asserts zero outbound connections during a full fake run.

## 2. Graphs
- **Task graph:** `thread_id = "task:<task_id>:g<generation>"`. Nodes: `analyse → plan → challenge ⇄ revise → plan_approval (interrupt)`, then phase threads are launched as dependencies allow.
- **Phase graph:** `thread_id = "phase:<phase_id>:g<generation>"`. Nodes mirror workflow-state-machine.md §2.
- `generation` starts at 1 and increases only through the audited rebuild (§5).
- **Routing functions call the ATLAS policy engine** (guards G-*), which reads evidence from the app DB. Agent output never selects an edge.
- **Long waits are interrupts:** CI, merge queue, manual handoff, approvals. The scheduler observes the outside world, re-checks the guard, and resumes with `Command(resume=<JSON primitive>)`.

## 3. Step-completion contract (replay safety) (ATLAS-L01, revised after LG-2)
LangGraph re-runs a node from its start on resume and replays external calls. ATLAS therefore uses a **two-node step pattern** with an identity **persisted in graph state before any effect**, backed by `workflow_steps` in the app DB (database-design.md §5).

```
workflow_steps(step_id PK, project_id, entity_type task|phase, entity_id, thread_id, generation, node,
               entry_version, status started|effects_settled|completed|abandoned,
               candidate_id_in, candidate_id_out, result_ref, transition_id, abandon_reason)
UNIQUE(entity_id, node, entry_version)
UNIQUE INDEX one_unfinished_step ON (entity_id, node) WHERE status IN ('started','effects_settled')
```

Every effectful FSM step `X` is two graph nodes:

1. **`X_begin`** (no external effects). `BEGIN IMMEDIATE`, then:
   - **reuse** the unfinished step for `(entity_id, node)` if one exists (the partial unique index guarantees at most one);
   - otherwise insert a new `started` row with `entry_version = entity.version`.

   It commits, then returns `{current_step_id}` into graph state, and the checkpoint is written (`durability="sync"`).
   - Replaying `X_begin` is safe. No transition happens inside it, and overlay transitions (PAUSED, recovery) can't strand a step because lookup goes by "unfinished step for this node", not by version.
2. **`X_run`** **takes `current_step_id` from graph state** and never re-derives it from the current version (that was the LG-2 defect):
   - If the step is `completed`, return the recorded result. No effect and no transition.
   - If the step is **`abandoned`** (ATLAS-L21): dispatch no effects and do not complete it. Return `{"retry": disposition}`, whose route goes **back to `X_begin`**, which allocates a fresh step:
     - only if the abandon record's retry disposition allows it **and** the retry limits (G-RETRY, repair budget) still hold;
     - for writer roles, only after the preserve-and-reset of recovery-strategy.md §2.3 is recorded as done.

     Otherwise the route goes to `BLOCKED(retry_not_permitted)`. The `X_run → X_begin` edge is graph-internal: it is not an FSM transition and writes no `transitions` row.
   - The completion transaction requires `status IN ('started','effects_settled')`, so an abandoned step can never be completed.
   - Otherwise perform the effects keyed by `step_id`:
     - executions: `idempotency_key = step_id` (a succeeded execution is reused; an interrupted one follows recovery-strategy.md §2.4);
     - outbox ops: idempotency key embeds `step_id`;
     - candidate creation: reconciled by tree SHA.
   - Then commit **one completion transaction**: result refs + outbox confirm (this *is* the outbox CONFIRM transaction, recovery-strategy.md §1) + FSM transition (`apply_transition`) + `status=completed` + `transition_id`.
   - **Overlay-aware completion (ATLAS-L03):** if the entity is in an overlay state (PAUSED or BLOCKED with `resume_to`) when the completion commits, the step's transition is applied to the **underlying** state. The transaction checks `(resume_to → target) ∈ ALLOWED`, sets `resume_to = target`, and writes the transition row with `underlying=true`. The overlay itself is untouched. This covers a node finishing just after a user pause commits, and recovery completing steps of an already-paused entity.

**Crash windows** (each tested, including "completion committed at version v+1 while the checkpoint still holds `X_run` with `current_step_id` from v": no new step, execution or outbox op may be allocated):

| Crash between… | On resume |
|---|---|
| `X_begin` txn → checkpoint | `X_begin` replays and reuses the unfinished row |
| checkpoint → effect dispatch | `X_run` dispatches once (dispatch reservation, §4) |
| effect → completion txn | Execution/outbox reconciled by step_id, then completion commits once |
| completion txn → checkpoint | `X_run` replays, sees `completed`, returns the result. No second transition |

## 4. Pause and cancel: serialized fence and dispatch (ATLAS-L02, revised after LG-2)
**Dispatch reservation.** Every ordinary external effect is dispatched in two moves:
1. `BEGIN IMMEDIATE`, then:
   - check the entity's fence (`pause_requested_at` / `cancel_requested_at`); if it is set, abort the dispatch;
   - **insert the reservation**: execution row `status=launching`, or outbox op `status=in_flight`.

   Then commit.
2. Only then launch the subprocess or run the op.

The **fence** is set in its own `BEGIN IMMEDIATE` transaction. SQLite's single writer serializes the two, so every dispatch either committed its reservation **before** the fence (visible to the settle step) or saw the fence and aborted. There is no third case, including "passed the check, then launched after the fence", because the check and the reservation are one transaction.

**Effect classes under a fence:**

| Class | Examples | Allowed while fenced? |
|---|---|---|
| ordinary | agent/test executions, push, create/update PR, merge, rerun CI | ❌ rejected at dispatch |
| safety/reconciliation | `cancel_remote_merge`, outbox reconciliation reads, process quiesce, (cancel only) `close_pr` | ✅ explicitly allowed |

**Pause protocol:**
1. Set the fence.
2. **Settle:** wait until every reservation committed before the fence reaches a terminal status. Executions are finished or quiesced (user's choice; default: finish read-only roles, quiesce writers). Outbox ops are confirmed, failed or reconciled.
3. **Withdraw:** run and confirm `cancel_remote_merge` if a remote merge is queued or armed. This is a safety-class op, permitted under the fence.
4. Commit PAUSED with `resume_to`. The node ends the invocation via `interrupt({"paused": true})`.

Cancel works the same way, then closes remote objects (safety class, with user confirmation) and commits CANCELLED.

**Tests:** pause between the fence check and subprocess launch (the reservation must exist and be settled); pause during a running node; pause immediately before merge dispatch; pause while MERGE_PENDING_REMOTE (withdrawal executes under the fence).

## 5. Two stores: recovery contract, classification, rebuild (ATLAS-L03, ATLAS-L05, revised after LG-2)
| Store | File | Authority |
|---|---|---|
| LangGraph checkpoints | `~/.atlas/graph.db` (0600) | Where execution is |
| App DB | `~/.atlas/atlas.db` | What is true |

Each checkpoint state carries `{entity_version, current_step_id, generation}`.

**Recovery's write rule:** recovery applies only two kinds of FSM transition:
1. a step's **own completion** (5.1), and
2. **overlay transitions** (PAUSED, BLOCKED, and their recorded exits).

Every other state change is applied by a graph node on resume, from facts recovery recorded. For example, the confirmed withdrawal of a remote merge is a recorded fact; on resume, the `await_remote_merge` node routes to CI_VALIDATION.

### 5.1 Resolve unfinished steps first (recovery-strategy.md §3 step 11a: after process quiesce, outbox and GitHub reconciliation and remote-merge withdrawal; **before** the PAUSED overlay of step 12)
For every `workflow_steps` row in `started` or `effects_settled`:

| Durable evidence for the step | Resolution |
|---|---|
| Its execution `succeeded` with captured, parsed result; or its outbox op `confirmed` | **Complete it now** (actor `recovery`). Run the node's deterministic *completion function* on the recorded result inside the completion transaction. The effect is **not** re-run |
| Its execution `interrupted` / `failed`, or its outbox op `failed` (outcome known: not done) | Mark the step `abandoned` with a reason. The retry decision follows recovery-strategy.md §2.4: re-run read-only roles; preserve and reset for writers. A later `X_begin` allocates a fresh step |
| Its outbox op is still `reconciling` / `abandoned` (outcome unknown) | Leave the step unfinished. Entity → `BLOCKED(effect_outcome_unknown)` |
| No dispatch reservation exists (crash before dispatch) | Leave `started`. It will be reused by `X_begin`/`X_run` on resume |

### 5.2 Classification (startup step 13: after 5.1 and after the PAUSED overlay of step 12)
Let `v_c` be the checkpoint's `entity_version` and `T` the app `transitions` rows for the entity with version > `v_c`. A thread is:
- **consistent:** `T` is empty;
- **compatible (app-ahead and/or overlay):** every row in `T` is either
  - the completion transition of the checkpoint's `current_step_id` (step completed by the node or by 5.1), or
  - an **overlay transition**: actor `recovery` or `user`, with `to_state` ∈ {PAUSED, BLOCKED, AWAITING_APPROVAL} or an exit from those back to the recorded `resume_to`.

  The thread is resumed (when the user presses Resume). On resume the step pattern either returns the completed step or continues it;
- **incompatible:** anything else. That includes:
  - transitions completing a step the checkpoint doesn't reference;
  - `v_c` greater than the app version;
  - generation mismatch;
  - a missing thread for an active entity;
  - the checkpoint's `current_step_id` unknown or belonging to another candidate.

  → `BLOCKED(engine_state_divergence)`, recording `rebuild_target` = the app's last **non-overlay** state (and its `resume_to`).

### 5.3 Rebuild (the only exit from engine_state_divergence; audited, user-triggered)
Preconditions:
- 5.1 has resolved every unfinished step of the entity: none is `started` with a dispatch reservation, and none is `effects_settled`;
- no `launching`/`running` executions or `in_flight`/`reconciling` outbox ops remain;
- no remote merge is queued or armed.

Then:
1. Archive the old thread's checkpoints as read-only.
2. Increment `generation`.
3. Start a new thread at the node matching `rebuild_target`, with no `current_step_id`.

Completed steps are facts in the app DB. Abandoned steps follow their recorded retry decision. Because 5.1 completed every step whose effect succeeded, a rebuilt node cannot repeat a succeeded execution or a confirmed outbox op. Its `X_begin` sees no unfinished step and allocates a fresh one only for work that genuinely hasn't happened.

**Tests:**
- successful execution, then a crash before completion. Startup finishes, the result is consumed exactly once, and the phase ends PAUSED with `resume_to` = the step's target state;
- the same with the entity already user-PAUSED before the crash (overlay-aware completion);
- restart with an `X_run` checkpoint whose execution was interrupted, for both a read-only and a writer role. The step is abandoned, resume routes to `X_begin`, a fresh step identity is used, and the abandoned invocation is never reused;
- rebuild after an execution succeeded but before step completion (5.1 completes it; no re-execution);
- rebuild after outbox confirmation;
- rebuild after withdrawal plus the PAUSED overlay;
- classification with recovery-generated transitions;
- the completion-before-checkpoint window.

### 5.4 Backup and restore
- **Quiesced, paired backups:** the scheduler is paused and no reservations are outstanding. Both files are copied with the SQLite online backup API under one `backup_generation`, recorded in both.
- Restore refuses mismatched generations, then runs 5.1 → 5.2 for every thread.

## 6. Checkpoint serialization (ATLAS-L06)
- **Custom serializer:** `StrictJsonSerializer`, which implements LangGraph's serializer protocol and is passed explicitly to `AsyncSqliteSaver(serde=...)`.
  - **dumps:** only JSON primitives (str, int, float, bool, None, list, dict with str keys) after Pydantic validation, encoded as UTF-8 JSON. Anything else raises.
  - **loads:** `json.loads`, then structural validation against the state schema. Nothing outside JSON is decoded, so no object is constructed from type tags. A non-JSON type tag is rejected before decoding.
- Interrupt payloads, `Command(resume=…)` values and pending writes are restricted to JSON primitives by ATLAS. Engine-owned metadata that LangGraph itself serializes must also pass this serializer. **If the pinned LangGraph version needs non-JSON engine types to persist interrupts or pending writes, the integration spike (roadmap 1.0) fails and the decision is revisited.** That is not worked around with pickle or msgpack.
- Tests against the pinned versions:
  - round-trip of every checkpoint kind (interrupts, retries, crash-resume, pending writes);
  - hostile blobs in `graph.db` (msgpack ext types, pickle, `lc` constructor JSON) must be rejected before any object is constructed.

## 7. Security hardening summary
| Advisory class | Mitigation |
|---|---|
| SQLite checkpointer SQLi via metadata filter keys (CVE-2025-67644, CVE-2025-64104) | Patched pins. No filter built from input. No `Store` |
| Checkpoint deserialization RCE (CVE-2025-64439, CVE-2026-28277, CVE-2026-48775, CVE-2026-27794) | Patched pins plus `StrictJsonSerializer` (§6). No `BaseCache`. `graph.db` 0600 and sandbox-denied |
| Store namespace cross-access (CVE-2026-71433 and later) | `Store` not used |
| SDK/CLI/Platform issues | Not used |

## 8. Exit strategy and proof obligation (ATLAS-L07)
- The engine sits behind `WorkflowEngine.start/resume/pause/status`. The FSM table, guards, step table and app DB are ATLAS-owned, so the bespoke engine from the original design can replace LangGraph.
- **"LangGraph is faster" is a hypothesis.** Roadmap slice **1.0** is a timeboxed integration spike (≤ 3 working days) that must pass all of the following, or ATLAS reverts to the bespoke engine:
  - §3 crash-window tests;
  - §4 pause tests;
  - §5 classification and rebuild;
  - §6 serializer round-trip and hostile tests;
  - zero egress.

## 9. Slice 1.0 Spike Result (2026-10-22)
- **Result: PASS** on `langgraph==1.2.16`, `langgraph-checkpoint==4.3.0`, `langgraph-checkpoint-sqlite==3.1.2`.
- **Empirical finding on `StrictJsonSerializer` (§6):**
  - Dynamic `langgraph.types.interrupt()` persists a non-JSON `Interrupt` dataclass in `put_writes` (serialized by LangGraph's default `JsonPlusSerializer` as a `msgpack` extension type).
  - By modelling waiting states (`AWAITING_APPROVAL`, `AWAITING_MANUAL`, `PAUSED`, `BLOCKED`) via `StateGraph` step-pair invocations (`X_begin` → `X_run` → conditional halt/`END`) and `interrupt_before` boundaries, 100% of checkpoint payloads are pure UTF-8 JSON primitives (`('json', b'...')`) validated by `StrictJsonSerializer` with zero non-JSON types, zero `msgpack`, and zero `pickle`.
- **Partial unique index refinement on `workflow_steps` (§3, ATLAS-L21):**
  - Replaced table-level `UNIQUE(entity_id, node, entry_version)` with `CREATE UNIQUE INDEX ux_workflow_steps_non_abandoned ON workflow_steps(entity_id, node, entry_version) WHERE status <> 'abandoned'` alongside `one_unfinished_step`, allowing an `abandoned` step routing `X_run → X_begin` (with no FSM version bump) to allocate a fresh `step_id` at the same `entry_version`.
- All §3 crash-window tests, §4 pause fence and overlay-completion tests, §5 thread classification and audited rebuild tests, §6 hostile payload rejection tests, and §1 zero-egress tests pass in [`tests/test_langgraph_engine.py`](../../tests/test_langgraph_engine.py).
