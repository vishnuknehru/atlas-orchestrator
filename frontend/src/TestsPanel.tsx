import React, { useCallback, useEffect, useState } from "react";
import { atlasApi, type TestEvidence } from "./api";

interface Props {
  taskId: string;
  state: string;
  version: number;
}

const BADGE: Record<string, string> = {
  passed: "bg-emerald-50 border-emerald-200 text-emerald-900",
  failed: "bg-rose-50 border-rose-200 text-rose-900",
  blocked: "bg-amber-50 border-amber-200 text-amber-900",
  running: "bg-stone-100 border-stone-300 text-stone-700",
};

export function TestsPanel({ taskId, state, version }: Props) {
  const [evidence, setEvidence] = useState<TestEvidence | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    atlasApi.getTaskTests(taskId).then((e) => setEvidence(e && Array.isArray(e.commands) ? e : null)).catch(() => setEvidence(null));
  }, [taskId]);

  useEffect(() => {
    if (state !== "EXECUTING_PHASES") { setEvidence(null); return; }
    load();
  }, [state, version, load]);

  // Poll while tests run, and briefly while waiting for them to start after the candidate is produced.
  useEffect(() => {
    if (state !== "EXECUTING_PHASES" || (evidence && evidence.status !== "running")) return;
    let tries = 0;
    const timer = setInterval(() => { tries += 1; if (tries > 20 && !evidence) clearInterval(timer); else load(); }, 3000);
    return () => clearInterval(timer);
  }, [state, evidence, load]);

  if (state !== "EXECUTING_PHASES") return null;

  const run = async () => {
    setBusy(true); setError("");
    try { await atlasApi.runTaskTests(taskId); setEvidence({ status: "running", commit_sha: "", commands: [] }); setTimeout(load, 1500); }
    catch (err) { setError(err instanceof Error ? err.message : "Could not start the tests"); }
    finally { setBusy(false); }
  };

  return (
    <section aria-label="Tests" className="bg-stone-50 border border-stone-200 rounded p-3 space-y-2">
      <div className="flex items-center justify-between">
        <h3 className="font-semibold text-stone-900">Tests</h3>
        {evidence && <span className={`text-xs px-2 py-0.5 rounded border ${BADGE[evidence.status] ?? BADGE.running}`}>{evidence.status}</span>}
      </div>
      {!evidence && <p className="text-xs text-stone-600">Tests have not run for this candidate yet.</p>}
      {evidence?.stale && <p role="alert" className="text-xs text-amber-900">These results are for an older candidate and no longer count.</p>}
      {evidence?.status === "blocked" && (
        <p className="text-xs text-amber-900">{evidence.blocked_reason}{evidence.detail ? `: ${evidence.detail}` : ""}</p>
      )}
      {evidence?.status === "running" && <p role="status" className="text-xs text-stone-600">Running in the sandbox…</p>}
      {evidence?.commands.map((c, i) => (
        <details key={i} open={c.status !== "passed"}>
          <summary className="text-sm font-mono cursor-pointer">
            {c.status === "passed" ? "✓" : "✗"} {c.command} <span className="text-xs text-stone-500">({c.status}{c.exit_code !== null ? `, exit ${c.exit_code}` : ""}, {c.seconds}s)</span>
          </summary>
          <pre className="text-xs bg-white border border-stone-200 rounded p-2 overflow-x-auto max-h-64">{c.log || "(no output)"}</pre>
        </details>
      ))}
      {evidence?.status === "passed" && (
        <p className="text-xs text-stone-500">Passed in the sandbox. Independent review and QA are not built yet, so this task still stops here.</p>
      )}
      {error && <p role="alert" className="text-xs text-rose-800">{error}</p>}
      <button type="button" disabled={busy || evidence?.status === "running"} onClick={() => void run()} className="text-xs border border-stone-300 rounded px-3 py-1 disabled:opacity-50">
        {evidence ? "Run tests again" : "Run tests"}
      </button>
    </section>
  );
}
