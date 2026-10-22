# ADR-005: Review independence is enforced by provider *and* model family

- Status: Proposed
- Date: 2026-10-22

## Context
Antigravity can run Claude and GPT-OSS models (`evidence/agy-models.txt`). Claude Code and Codex may also expose models from several families over time. If the reviewer and the author share a model family, they tend to share the same blind spots.

## Decision
- Every artifact (plan, phase diff, fix) records `author_agent` and `author_model`.
- A reviewer, challenger or QA agent is assigned only if `provider ≠ author provider` **and** `model_family(reviewer_model) ≠ model_family(author_model)`.
- Model family mapping (built-in, versioned with ATLAS):
  - `claude-*` → anthropic;
  - `gpt-*` (including open-weight `gpt-oss-*`), `o*` and Codex defaults → openai;
  - `gemini-*` and `gemma-*` → google;
  - `llama-*` → meta;
  - `qwen*` → alibaba;
  - `mistral-*`, `mixtral-*`, `codestral-*`, `devstral-*` → mistral;
  - `deepseek-*` → deepseek;
  - unknown → `unknown`, which counts as matching the author's family, so independence is not satisfied.
- **Declared family (ADR-007).** A registry entry may set `model_family` for a model the built-in map doesn't know, such as a fine-tune or a renamed local model. The user's declaration is recorded with each artifact. If it contradicts the built-in map, the built-in map wins. A fine-tune counts as the family of its base model.
- **Provider identity (ADR-007 §2).** The "provider ≠ author provider" check compares the *derived* inference provider, not the registry id: `anthropic`, `openai`, `google`, `local`, or the host of an opted-in remote endpoint. Two registry aliases for the same hosted provider never satisfy it.
- **Local exception, stated explicitly.** The provider rule exists so that a single vendor's systems and policies can't both write and judge a change. Models running locally on the operator's own server share no vendor. So when **both** agents have provider `local`, the provider check counts as satisfied, and independence rests on the model-family check alone. In every other case, both checks apply.
- If independence cannot be met, the step goes to `AWAITING_MANUAL` or `BLOCKED`. The author is never used as the sole reviewer.
- A fix to a finding is "verified" only by an independent reviewer's re-check on the new SHA, plus re-executed tests.

## Consequences
- With user-configured agents, any profile is accepted only if every review, challenge and QA assignment passes these rules. A profile with a single model family can't run mandatory reviews, so those steps go to `AWAITING_MANUAL`.
- With the default roles, the developer is Codex (openai), the code reviewer is Claude (anthropic), and QA is Antigravity on a Gemini model (google). That gives three distinct families.
- Losing one provider may block mandatory reviews. This is intentional, because the brief requires that independence never be sacrificed.
