import React, { useEffect, useState } from "react";
import { atlasApi, type TaskCandidate } from "./api";

interface Props {
  taskId: string;
  state: string;
  version: number;
}

const CANDIDATE_STATES = ["EXECUTING_PHASES", "BLOCKED", "PAUSED", "CANCELLED"];

export function CandidatePanel({ taskId, state, version }: Props) {
  const [candidate, setCandidate] = useState<TaskCandidate | null>(null);

  useEffect(() => {
    if (!CANDIDATE_STATES.includes(state)) { setCandidate(null); return; }
    let cancelled = false;
    atlasApi.getTaskCandidate(taskId).then((c) => { if (!cancelled) setCandidate(c); }).catch(() => { if (!cancelled) setCandidate(null); });
    return () => { cancelled = true; };
  }, [taskId, state, version]);

  if (!candidate || !Array.isArray(candidate.changed_files)) return null;

  return (
    <section aria-label="Candidate" className="bg-stone-50 border border-stone-200 rounded p-3 space-y-2">
      <div className="flex items-center justify-between">
        <h3 className="font-semibold text-stone-900">Candidate change</h3>
        <span className="text-xs text-stone-500 font-mono">{candidate.commit_sha.slice(0, 10)} by {candidate.author_agent}</span>
      </div>
      {candidate.guard_status === "held" && (
        <p role="alert" className="text-xs text-amber-900 bg-amber-50 border border-amber-200 rounded p-2">
          Held for your review: {candidate.violations.map((v) => v.path).join(", ")}
        </p>
      )}
      <ul className="text-sm font-mono text-stone-800">
        {candidate.changed_files.map((f) => <li key={f}>{f}</li>)}
      </ul>
      {candidate.summary && <p className="text-xs text-stone-600 whitespace-pre-wrap">{candidate.summary}</p>}
      <p className="text-xs text-stone-500">
        See the diff in the worktree: <code>git -C {candidate.worktree} diff {candidate.base_sha.slice(0, 10)}..HEAD</code>.
        Tests, review and delivery are not built yet, so this task stops here.
      </p>
    </section>
  );
}
