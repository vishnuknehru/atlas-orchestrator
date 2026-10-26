import React, { useCallback, useEffect, useState } from "react";
import { atlasApi, type SandboxStatus } from "./api";

const sectionClass = "bg-white border border-stone-200 rounded-lg p-4 space-y-4";

export function SandboxPanel(): React.ReactElement {
  const [status, setStatus] = useState<SandboxStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setBusy(true); setError(null);
    try {
      const next = await atlasApi.getSandbox();
      setStatus(Array.isArray(next.checks) ? next : null);
    }
    catch (err) { setError(err instanceof Error ? err.message : "Could not check the sandbox"); }
    finally { setBusy(false); }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const selfTest = async () => {
    setBusy(true); setError(null);
    try {
      const result = await atlasApi.runSandboxSelfTest();
      setStatus((prev) => ({ ...result, start_command: prev?.start_command }));
    } catch (err) { setError(err instanceof Error ? err.message : "The self-test failed to run"); }
    finally { setBusy(false); }
  };

  return (
    <section className={sectionClass} aria-label="Test sandbox">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-base font-semibold text-stone-900">Test sandbox</h2>
          <p className="text-xs text-stone-500">
            Tests run only in locked-down containers in your Colima VM. Until it is ready, no tests run and no task can complete.
          </p>
        </div>
        {status && (
          <span className={`text-xs px-2 py-0.5 rounded border ${status.ready ? "bg-emerald-50 border-emerald-200 text-emerald-900" : "bg-amber-50 border-amber-200 text-amber-900"}`}>
            {status.ready ? "Ready" : "Not ready"}
          </span>
        )}
      </div>
      {error && <p role="alert" className="text-sm text-rose-800">{error}</p>}
      {status && (
        <ul className="text-sm space-y-1">
          {status.checks.map((c) => (
            <li key={c.id}>
              <span aria-hidden="true">{c.ok ? "✓" : c.level === "warning" ? "!" : "✗"}</span> <span className="sr-only">{c.ok ? "passed: " : c.level === "warning" ? "warning: " : "failed: "}</span>
              {c.label}{c.detail && <span className="text-stone-500"> — {c.detail}</span>}
            </li>
          ))}
        </ul>
      )}
      {status && !status.ready && (
        <div className="text-xs space-y-2">
          {status.start_command && (<><p>1. Start the Colima VM:</p><pre className="bg-stone-100 rounded p-2 overflow-x-auto">{status.start_command}</pre></>)}
          <p>2. Run the self-test.</p>
        </div>
      )}
      <div className="flex gap-2">
        <button type="button" disabled={busy} onClick={() => void load()} className="text-xs border border-stone-300 rounded px-3 py-1">Check again</button>
        <button type="button" disabled={busy} onClick={() => void selfTest()} className="bg-teal-800 hover:bg-teal-900 text-stone-900 text-xs font-medium px-3 py-1 rounded disabled:opacity-50">
          {busy ? "Working…" : "Run self-test"}
        </button>
      </div>
    </section>
  );
}
