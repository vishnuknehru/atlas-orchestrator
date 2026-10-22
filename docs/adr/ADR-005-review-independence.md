# ADR-005: Review independence is enforced by provider *and* model family

- Status: Proposed
- Date: 2026-10-22

## Context
Antigravity can run Claude and GPT-OSS models (`evidence/agy-models.txt`). Claude Code and Codex may also expose models from several families over time. If the reviewer and the author share a model family, they tend to share the same blind spots.

## Decision
- Every artifact (plan, phase diff, fix) records `author_agent` and `author_model`.
- A reviewer, challenger or QA agent is assigned only if `provider ≠ author provider` **and** `model_family(reviewer_model) ≠ model_family(author_model)`.
- Model family mapping:
  - `claude-*` → anthropic;
  - `gpt-*`, `o*` and Codex defaults → openai;
  - `gemini-*` → google;
  - unknown → `unknown`, which counts as matching the author's family, so independence is not satisfied.
- If independence cannot be met, the step goes to `AWAITING_MANUAL` or `BLOCKED`. The author is never used as the sole reviewer.
- A fix to a finding is "verified" only by an independent reviewer's re-check on the new SHA, plus re-executed tests.

## Consequences
- With the default roles, the developer is Codex (openai), the code reviewer is Claude (anthropic), and QA is Antigravity on a Gemini model (google). That gives three distinct families.
- Losing one provider may block mandatory reviews. This is intentional, because the brief requires that independence never be sacrificed.
