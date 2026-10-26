import React, { useEffect, useState } from "react";
import { atlasApi, type TaskPlan } from "./api";

interface Props {
  taskId: string;
  state: string;
  version: number;
  onChanged: () => void;
}

const PLAN_STATES = ["PLAN_CHALLENGE", "PLAN_REVISION", "AWAITING_APPROVAL", "READY_FOR_IMPLEMENTATION", "BLOCKED"];

export function PlanPanel({ taskId, state, version, onChanged }: Props) {
  const [plan, setPlan] = useState<TaskPlan | null>(null);
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!PLAN_STATES.includes(state)) { setPlan(null); return; }
    let cancelled = false;
    atlasApi.getTaskPlan(taskId).then((p) => { if (!cancelled) setPlan(p); }).catch(() => { if (!cancelled) setPlan(null); });
    return () => { cancelled = true; };
  }, [taskId, state, version]);

  if (!plan || !Array.isArray(plan.steps)) return null;

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true); setError(null);
    try { await fn(); setComment(""); onChanged(); }
    catch (err) { setError(err instanceof Error ? err.message : "Action failed"); }
    finally { setBusy(false); }
  };

  const challenges = plan.history.filter((h) => h.kind === "challenge");
  const awaiting = state === "AWAITING_APPROVAL";

  return (
    <section aria-label="Plan" className="bg-stone-50 border border-stone-200 rounded p-3 space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="font-semibold text-stone-900">Plan (revision {plan.revision})</h3>
        <span className="text-xs text-stone-500">drafted by {plan.author_agent}</span>
      </div>
      <p className="text-sm text-stone-700">{plan.approach}</p>
      <ol className="list-decimal pl-5 space-y-1 text-sm text-stone-800">
        {plan.steps.map((s, i) => (
          <li key={i}><span className="font-medium">{s.title}</span> — {s.description}
            {s.criteria.length > 0 && <span className="text-xs text-stone-500"> ({s.criteria.join(", ")})</span>}
          </li>
        ))}
      </ol>
      {plan.risks.length > 0 && (
        <div className="text-xs text-stone-600"><span className="font-semibold">Risks:</span> {plan.risks.join("; ")}</div>
      )}
      {plan.history.length > 0 && (
        <details>
          <summary className="text-xs font-semibold text-stone-600 cursor-pointer">
            Challenge history ({challenges.length} {challenges.length === 1 ? "round" : "rounds"})
          </summary>
          <ul className="mt-2 space-y-2 text-xs text-stone-700">
            {plan.history.map((h, i) => (
              <li key={i}>
                {h.kind === "challenge" && (
                  <>
                    <div className="font-medium">Round {h.cycle}: {h.challenger} says {h.verdict === "approve" ? "no blocking issues" : "revise"}</div>
                    <ul className="pl-4 list-disc">
                      {h.findings.map((f) => <li key={f.id}>[{f.severity}] {f.id}: {f.text}</li>)}
                    </ul>
                  </>
                )}
                {h.kind === "revision" && (
                  <>
                    <div className="font-medium">Architect's answers (revision {h.revision})</div>
                    <ul className="pl-4 list-disc">
                      {h.responses.map((r) => <li key={r.finding_id}>{r.finding_id}: {r.response}</li>)}
                    </ul>
                  </>
                )}
                {h.kind === "user_feedback" && <div className="font-medium">You asked: {h.text}</div>}
              </li>
            ))}
          </ul>
        </details>
      )}
      {awaiting && plan.escalated && (
        <p role="alert" className="text-xs text-amber-900 bg-amber-50 border border-amber-200 rounded p-2">
          The challenge cap was reached with blocking findings still open. Read the history, then decide.
        </p>
      )}
      {error && <p role="alert" className="text-xs text-red-700">{error}</p>}
      {awaiting && (
        <div className="space-y-2">
          <textarea
            aria-label="Requested changes"
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            rows={2}
            placeholder="What should change? (needed for Request changes)"
            className="w-full text-sm border border-stone-300 rounded p-2 bg-white"
          />
          <div className="flex gap-2">
            <button type="button" disabled={busy} onClick={() => void act(() => atlasApi.approvePlan(taskId))}
              className="bg-teal-800 hover:bg-teal-900 text-stone-900 text-xs font-medium px-3 py-1 rounded">
              Approve plan
            </button>
            <button type="button" disabled={busy || comment.trim().length < 3}
              onClick={() => void act(() => atlasApi.requestPlanChanges(taskId, comment.trim()))}
              className="bg-amber-600 hover:bg-amber-500 text-stone-900 text-xs font-medium px-3 py-1 rounded disabled:opacity-50">
              Request changes
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
