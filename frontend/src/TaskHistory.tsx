import { useEffect, useState } from "react";
import { atlasApi, TaskHistoryResponse } from "./api";

export function TaskHistory({ taskId, version }: { taskId: string; version: number }) {
  const [history, setHistory] = useState<TaskHistoryResponse | null>(null);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let active = true;
    setLoading(true); setError(""); setHistory(null);
    void atlasApi.getTaskHistory(taskId).then(data => {
      if (active) setHistory(data);
    }).catch(err => {
      if (active) setError(err instanceof Error ? err.message : "Could not load task history");
    }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [taskId, version, refresh]);

  const download = () => {
    if (!history) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(history, null, 2)], { type: "application/json" }));
    const link = document.createElement("a");
    link.href = url; link.download = `atlas-task-${taskId}-history.json`;
    document.body.appendChild(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  return <section aria-label="Saved task history" className="space-y-4 border-t border-stone-200 pt-6">
    <div className="flex flex-wrap justify-between items-center gap-3">
      <div><h2 className="text-base font-semibold">Saved task history</h2><p className="text-sm text-stone-500">Review the records Atlas has saved for this task, or download them for investigation.</p></div>
      <div className="flex gap-3">
        <button type="button" className="text-sm text-teal-800" disabled={loading} onClick={() => setRefresh(value => value + 1)}>Refresh history</button>
        <button type="button" className="text-sm text-teal-800" disabled={!history || loading} onClick={download}>Download history (JSON)</button>
      </div>
    </div>
    {loading && <p role="status" className="text-sm text-stone-500">Loading saved records…</p>}
    {error && <p role="alert" className="text-sm text-rose-800">{error} <button type="button" className="underline" onClick={() => setRefresh(value => value + 1)}>Retry history</button></p>}
    {history && <>
      <dl className="grid grid-cols-1 md:grid-cols-3 gap-3 text-sm">
        <div><dt className="text-stone-500">Task reference</dt><dd className="font-mono">{history.task.id}</dd></div>
        <div><dt className="text-stone-500">Workflow reference</dt><dd className="font-mono">{history.workflow_thread_id}</dd></div>
        <div><dt className="text-stone-500">Created</dt><dd>{new Date(history.task.created_at).toLocaleString()}</dd></div>
      </dl>
      <details><summary className="text-sm font-medium py-2">Runs ({history.executions.length})</summary>
        {history.executions.length === 0 ? <p className="text-sm text-stone-500 py-2">No execution records have been saved for this task yet.</p> : history.executions.map(run => <div className="border-t border-stone-200 py-3 space-y-1" key={run.id}>
          <p className="text-sm">{run.agent_id ?? run.kind} · {run.role ?? "Execution"} · {run.status}{run.model ? ` · ${run.model}` : ""}</p>
          <p className="text-xs text-stone-500">{run.started_at ?? "Not started"} · Exit code: {run.exit_code ?? "—"}</p>
          {run.error_detail && <p className="text-sm text-rose-800">{run.error_detail}</p>}
          <p className="font-mono text-stone-500">Run {run.id} · Trace {run.correlation_id}</p>
        </div>)}
      </details>
      <details><summary className="text-sm font-medium py-2">Workflow steps ({history.workflow_steps.length})</summary>
        {history.workflow_steps.length === 0 ? <p className="text-sm text-stone-500 py-2">No workflow steps have been recorded yet.</p> : history.workflow_steps.map(step => <p className="text-sm py-2" key={step.step_id}>{step.node} · {step.status} · Attempt {step.generation}<span className="block font-mono text-stone-500">{step.step_id}</span></p>)}
      </details>
      <details><summary className="text-sm font-medium py-2">Approvals ({history.approvals.length})</summary>
        {history.approvals.length === 0 ? <p className="text-sm text-stone-500 py-2">No approvals recorded.</p> : history.approvals.map(approval => <p className="text-sm py-2" key={approval.id}>{approval.kind} · {approval.status}{approval.decided_by ? ` · ${approval.decided_by}` : ""}{approval.reason ? ` — ${approval.reason}` : ""}</p>)}
      </details>
      <details><summary className="text-sm font-medium py-2">Artifacts and remote operations ({history.artifacts.length + history.remote_operations.length})</summary>
        {history.artifacts.length + history.remote_operations.length === 0 && <p className="text-sm text-stone-500 py-2">No artifacts or remote operations recorded.</p>}
        {history.artifacts.map(artifact => <p className="text-sm py-2" key={artifact.id}>{artifact.kind}<span className="block font-mono text-stone-500">SHA-256 {artifact.sha256}</span></p>)}
        {history.remote_operations.map(op => <p className="text-sm py-2" key={op.id}>{op.op} · {op.status} · {op.attempts} attempts{op.last_error ? ` — ${op.last_error}` : ""}</p>)}
      </details>
      <p className="text-xs text-stone-500">Loaded {new Date(history.exported_at).toLocaleString()}. The download includes task details, phases, state changes, and the records listed here. Artifact contents and raw process arguments are excluded.</p>
    </>}
  </section>;
}
