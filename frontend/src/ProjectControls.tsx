import { useState } from "react";
import { atlasApi, ProjectRecord } from "./api";

export function ProjectControls({ project, onChanged }: { project: ProjectRecord; onChanged: (removed?: boolean) => Promise<void> }) {
  const [mode, setMode] = useState<"closed" | "edit" | "remove">("closed");
  const [name, setName] = useState(project.name);
  const [branch, setBranch] = useState(project.base_branch);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const open = (next: "edit" | "remove") => {
    setName(project.name); setBranch(project.base_branch); setError(""); setMessage(""); setMode(next);
  };
  const save = async () => {
    if (busy) return;
    setBusy(true); setError("");
    try {
      if (mode === "edit") {
        await atlasApi.updateProject(project.id, { name: name.trim(), base_branch: branch.trim() });
        setMessage("Project updated.");
      } else {
        await atlasApi.removeProject(project.id);
      }
      setMode("closed");
      await onChanged(mode === "remove");
    } catch (err) { setError(err instanceof Error ? err.message : "Could not update the project"); }
    finally { setBusy(false); }
  };
  return <div className="project-controls">
    {mode === "closed" ? <div className="flex gap-3">
      <button type="button" className="text-sm text-teal-800" onClick={() => open("edit")}>Edit project</button>
      <button type="button" className="text-sm text-rose-800" onClick={() => open("remove")}>Remove project</button>
    </div> : <form className="space-y-3" aria-label={mode === "edit" ? "Edit project" : "Remove project"} onSubmit={e => { e.preventDefault(); void save(); }}>
      <h3 className="font-semibold text-sm">{mode === "edit" ? "Edit project" : `Remove ${project.name}?`}</h3>
      {mode === "edit" ? <>
        <label className="field-label">Project name<input className="w-full border" required maxLength={80} value={name} disabled={busy} onChange={e => setName(e.target.value)} /></label>
        <label className="field-label">Base branch<input className="w-full border" required maxLength={80} value={branch} disabled={busy} onChange={e => setBranch(e.target.value)} /></label>
        <p className="text-xs text-stone-500">Repository: {project.github_repo}. To use a different repository, add a new project. Finish or cancel existing work before changing the branch.</p>
      </> : <p className="text-sm text-stone-600">This archives the project and removes it from your workspace. Your repository, files, and history are kept. Finish or cancel its tasks first. You can restore it in Settings.</p>}
      {error && <p role="alert" className="text-sm text-rose-800">{error}</p>}
      <div className="flex gap-3 items-center">
        <button type="submit" disabled={busy || (mode === "edit" && (!name.trim() || !branch.trim() || (name.trim() === project.name && branch.trim() === project.base_branch)))} className="bg-teal-800 rounded px-3 py-2 text-sm disabled:opacity-50">{busy ? (mode === "remove" ? "Removing…" : "Saving…") : mode === "edit" ? "Save changes" : "Confirm removal"}</button>
        <button type="button" disabled={busy} onClick={() => { setMode("closed"); setError(""); }}>Cancel</button>
      </div>
    </form>}
    {message && <p role="status" className="text-sm text-teal-800">{message}</p>}
  </div>;
}

export function ArchivedProjects() {
  const [projects, setProjects] = useState<ProjectRecord[] | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const load = async () => {
    setError("");
    try { setProjects(await atlasApi.getArchivedProjects()); }
    catch (err) { setError(err instanceof Error ? err.message : "Could not load archived projects"); }
  };
  const restore = async (project: ProjectRecord) => {
    setBusy(project.id); setError("");
    try {
      await atlasApi.restoreProject(project.id);
      setProjects(prev => prev?.filter(p => p.id !== project.id) ?? []);
      setMessage(`${project.name} restored. It is available on the dashboard.`);
    } catch (err) { setError(err instanceof Error ? err.message : "Could not restore project"); }
    finally { setBusy(null); }
  };
  return <section className="bg-white border border-stone-200 rounded-lg p-4 space-y-4">
    <h2 className="font-semibold">Archived projects</h2>
    <p className="text-sm text-stone-500">Restore a removed project with its settings and history.</p>
    <button type="button" onClick={() => void load()} className="text-sm text-teal-800">{projects ? "Refresh archived projects" : "Show archived projects"}</button>
    {error && <p role="alert" className="text-rose-800 text-sm">{error}</p>}
    {message && <p role="status" className="text-teal-800 text-sm">{message}</p>}
    {projects?.length === 0 && <p className="text-sm text-stone-500">No archived projects.</p>}
    {projects?.map(project => <div key={project.id} className="flex justify-between items-center gap-3">
      <span className="text-sm">{project.name} <span className="text-stone-500">· {project.github_repo}</span></span>
      <button type="button" disabled={busy !== null} onClick={() => void restore(project)} className="text-sm text-teal-800 disabled:opacity-50">{busy === project.id ? "Restoring…" : `Restore ${project.name}`}</button>
    </div>)}
  </section>;
}
