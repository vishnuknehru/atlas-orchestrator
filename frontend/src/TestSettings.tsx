import React, { useEffect, useState } from "react";
import { atlasApi } from "./api";

export function TestSettings({ projectId }: { projectId: string }) {
  const [image, setImage] = useState("");
  const [commands, setCommands] = useState("");
  const [paths, setPaths] = useState("");
  const [timeout, setTimeoutSeconds] = useState(600);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    atlasApi.getTestsConfig(projectId).then((c) => {
      if (!c || !Array.isArray(c.commands)) return;
      setImage(c.image ?? ""); setCommands(c.commands.join("\n")); setPaths((c.writable_paths ?? []).join("\n"));
      setTimeoutSeconds(c.timeout_seconds ?? 600);
    }).catch(() => undefined);
  }, [projectId]);

  const lines = (text: string) => text.split("\n").map((l) => l.trim()).filter(Boolean);
  const save = async () => {
    setBusy(true); setError(""); setMessage("");
    try {
      await atlasApi.saveTestsConfig(projectId, { test_image: image.trim(), test_commands: lines(commands), writable_paths: lines(paths), test_timeout_seconds: timeout });
      setMessage("Test settings saved.");
    } catch (err) { setError(err instanceof Error ? err.message : "Could not save the test settings"); }
    finally { setBusy(false); }
  };

  return (
    <form className="space-y-3 mt-4" aria-label="Test settings" onSubmit={(e) => { e.preventDefault(); void save(); }}>
      <h3 className="font-semibold text-sm">Test settings</h3>
      <p className="text-xs text-stone-500">
        Tests run in a locked-down container with no network, so the image must already contain your dependencies. Saving an image
        approves it. It must be pinned by digest.
      </p>
      <label className="field-label">Test image (pinned by digest)
        <input className="w-full border font-mono text-xs" placeholder="python:3.12-slim@sha256:…" value={image} disabled={busy} onChange={(e) => setImage(e.target.value)} />
      </label>
      <label className="field-label">Test commands (one per line, run in order from the repository root)
        <textarea className="w-full border font-mono text-xs" rows={3} value={commands} disabled={busy} onChange={(e) => setCommands(e.target.value)} />
      </label>
      <label className="field-label">Writable paths (one per line; everything else in the repository is read-only)
        <textarea className="w-full border font-mono text-xs" rows={2} value={paths} disabled={busy} onChange={(e) => setPaths(e.target.value)} />
      </label>
      <label className="field-label">Time limit per command (seconds)
        <input type="number" min={10} max={3600} className="w-32 border" value={timeout} disabled={busy} onChange={(e) => setTimeoutSeconds(Number(e.target.value))} />
      </label>
      {error && <p role="alert" className="text-sm text-rose-800">{error}</p>}
      {message && <p role="status" className="text-sm text-teal-800">{message}</p>}
      <button type="submit" disabled={busy} className="bg-teal-800 rounded px-3 py-2 text-sm disabled:opacity-50">Save test settings</button>
    </form>
  );
}
