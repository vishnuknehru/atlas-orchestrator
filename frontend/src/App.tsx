import React, { useCallback, useEffect, useState } from "react";
import {
  atlasApi,
  OverviewResponse,
  ProjectRecord,
  TaskRecord,
  TransitionRecord,
} from "./api";
import { ProjectControls } from "./ProjectControls";
import { Settings } from "./Settings";
import { TaskHistory } from "./TaskHistory";
import { readTheme, applyTheme, Theme } from "./theme";

type View = "dashboard" | "settings";
type Route = { view: View; projectId: string | null; taskId: string | null };

function routeFromHash(): Route {
  const parts = window.location.hash.slice(1).split("/").filter(Boolean);
  if (parts[0] === "settings") return { view: "settings", projectId: null, taskId: null };
  return { view: "dashboard", projectId: parts[0] === "projects" ? parts[1] ?? null : null,
    taskId: parts[0] === "projects" && parts[2] === "tasks" ? parts[3] ?? null : null };
}
function projectPath(id: string): string { return `#/projects/${encodeURIComponent(id)}`; }

function readableState(state: string): string {
  return state.toLowerCase().replaceAll("_", " ");
}

function stateBadgeClass(state: string): string {
  if (state === "COMPLETED" || state === "active") {
    return "bg-emerald-50 text-emerald-800 border-emerald-200";
  }
  if (state === "PAUSED" || state.startsWith("AWAITING")) {
    return "bg-amber-50 text-amber-800 border-amber-200";
  }
  if (state === "BLOCKED" || state === "blocked" || state === "FAILED") {
    return "bg-rose-50 text-rose-800 border-rose-200";
  }
  if (state === "CANCELLED") {
    return "bg-stone-100 text-stone-500 border-stone-300";
  }
  return "bg-stone-100 text-teal-800 border-stone-200";
}

export function App(): React.ReactElement {
  const [theme, setTheme] = useState<Theme>(readTheme);
  useEffect(() => { applyTheme(theme); }, [theme]);
  const [route, setRoute] = useState<Route>(routeFromHash);
  const view = route.view;
  const selectedProjectId = route.projectId;
  const [overview, setOverview] = useState<OverviewResponse | null>(null);
  const [selectedTask, setSelectedTask] = useState<TaskRecord | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [pending, setPending] = useState<string | null>(null);
  const [notice, setNotice] = useState("");

  // Project registration form state
  const [projName, setProjName] = useState("");
  const [projRepo, setProjRepo] = useState("");
  const [projBranch, setProjBranch] = useState("main");

  // Task creation form state
  const [taskTitle, setTaskTitle] = useState("");
  const [taskRequest, setTaskRequest] = useState("");
  const [taskCategory, setTaskCategory] = useState("feature");
  const [taskPriority, setTaskPriority] = useState(3);
  const [taskCriterion, setTaskCriterion] = useState("");
  const [roadmapGoal, setRoadmapGoal] = useState("");

  const loadData = useCallback(async () => {
    try {
      const data = await atlasApi.getOverview();
      setOverview(data);
      setSelectedTask(previous => previous && data.tasks.some(t => t.id === previous.id) ? previous : null);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load ATLAS overview");
    }
  }, []);

  const loadTaskDetail = useCallback(async (taskId: string) => {
    try {
      const detail = await atlasApi.getTask(taskId);
      const currentRoute = routeFromHash();
      if (currentRoute.taskId !== taskId || currentRoute.projectId !== detail.project_id) return;
      setSelectedTask(detail);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load task details");
    }
  }, []);

  // Reload on view changes too: settings (credentials, roles) change project status
  useEffect(() => {
    void loadData();
  }, [view, loadData]);

  useEffect(() => {
    const onHashChange = () => { setRoute(routeFromHash()); setError(null); setNotice(""); };
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  useEffect(() => {
    setSelectedTask(null);
    if (!route.taskId) return;
    let active = true;
    void atlasApi.getTask(route.taskId).then(detail => {
      if (!active) return;
      if (detail.project_id !== route.projectId) { setError("This task does not belong to this project."); return; }
      setSelectedTask(detail);
    }).catch(err => { if (active) setError(err instanceof Error ? err.message : "Failed to load task details"); });
    return () => { active = false; };
  }, [route.taskId, route.projectId]);

  useEffect(() => {
    setTaskTitle(""); setTaskRequest(""); setTaskCriterion("");
    setTaskCategory("feature"); setTaskPriority(3);
  }, [route.projectId]);

  const handleRegisterProject = async (e: React.FormEvent) => {
    e.preventDefault();
    if (pending) return;
    setPending("project"); setError(null); setNotice("");
    try {
      const created = await atlasApi.registerProject({
        name: projName,
        repository: projRepo,
        base_branch: projBranch,
        visibility: "private",
      });
      setProjName("");
      setProjRepo("");
      window.location.hash = projectPath(created.id);
      setSelectedTask(null);
      setProjBranch("main");
      setNotice("Project added. You can manage its settings and create tasks here.");
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to register project");
    } finally { setPending(null); }
  };

  const handleCreateTask = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedProjectId || pending) return;
    setPending("task"); setError(null); setNotice("");
    try {
      const created = await atlasApi.createTask(selectedProjectId, {
        title: taskTitle,
        request_text: taskRequest,
        category: taskCategory,
        priority: taskPriority,
        acceptance_criteria: taskCriterion
          ? [{ text: taskCriterion, mandatory: true }]
          : [],
      });
      setTaskTitle("");
      setTaskRequest("");
      setTaskCriterion("");
      setNotice("Task created. Select Start when you’re ready.");
      await loadData();
      window.location.hash = `${projectPath(selectedProjectId)}/tasks/${encodeURIComponent(created.id)}`;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create task");
    } finally { setPending(null); }
  };

  const handleCreateRoadmap = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedProjectId || pending) return;
    setPending("roadmap"); setError(null); setNotice("");
    try {
      const roadmap = await atlasApi.createRoadmap(selectedProjectId, roadmapGoal);
      setRoadmapGoal("");
      setNotice(`Plan ready: ${roadmap.tasks.length} phases were added as tasks. Start them in order when you’re ready.`);
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to plan project");
    } finally { setPending(null); }
  };

  const handleTaskAction = async (
    taskId: string,
    action: "start" | "pause" | "resume" | "cancel" | "rebuild"
  ) => {
    if (pending) return;
    if (action === "cancel" && !window.confirm("Cancel this task? It will stop progressing and cannot be resumed.")) return;
    setPending("action"); setError(null); setNotice("");
    try {
      await atlasApi.taskAction(taskId, action);
      await loadData();
      await loadTaskDetail(taskId);
    } catch (err) {
      setError(err instanceof Error ? err.message : `Failed to ${action} task`);
    } finally { setPending(null); }
  };

  const currentProject: ProjectRecord | undefined = overview?.projects.find(
    (p) => p.id === selectedProjectId
  );
  const projectTasks: TaskRecord[] =
    (overview?.tasks.filter((t) => t.project_id === selectedProjectId) || []).sort((a, b) =>
      a.roadmap_id && a.roadmap_id === b.roadmap_id ? (a.roadmap_seq ?? 0) - (b.roadmap_seq ?? 0) : 0
    );

  return (
    <div className="app-shell">
      <header className="app-header">
        <div>
          <a className="brand" href="#/" aria-label="Atlas home"><span className="brand-mark" aria-hidden="true">a</span> atlas<span className="brand-divider">/</span><span className="brand-caption">Workspace</span></a>
          <p className="text-sm text-stone-500">
            A little structure. More room to build.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <nav className="flex gap-1 text-sm" aria-label="Main">
            {(["dashboard", "settings"] as const).map((v) => (
              <a
                key={v}
                href={v === "settings" ? "#/settings" : "#/"}
                aria-current={view === v ? "page" : undefined}
                className={`px-3 py-1.5 rounded capitalize ${
                  view === v ? "bg-stone-100 text-stone-900" : "text-stone-500 hover:text-stone-900"
                }`}
              >
                {v}
              </a>
            ))}
          </nav>
          <div className="theme-switch" role="group" aria-label="Color theme">
            {(["light", "dark"] as const).map((mode) => (
              <button key={mode} type="button" aria-pressed={theme === mode} onClick={() => setTheme(mode)}>
                {mode === "light" ? "Light" : "Dark"}
              </button>
            ))}
          </div>
          <div className="text-xs font-mono text-stone-500 bg-stone-100 border border-stone-200 px-3 py-1.5 rounded">
            <span className="status-dot" /> Supervised
          </div>
        </div>
      </header>

      {view === "settings" ? (
        <main className="workspace">
          <div className="page-intro"><div><p className="eyebrow">Your workspace, your way</p><h1>Settings</h1><p>Manage connections, agents, and project preferences.</p></div></div>
          <Settings />
        </main>
      ) : (
        <main className="workspace space-y-6">
          <nav className="breadcrumbs" aria-label="Breadcrumb"><a href="#/">Projects</a>{route.projectId && <><span aria-hidden="true">/</span><a href={projectPath(route.projectId)}>{currentProject?.name ?? "Project"}</a></>}{route.taskId && <><span aria-hidden="true">/</span><span aria-current="page">{selectedTask?.title ?? "Task"}</span></>}</nav>
          <div className="page-intro"><div><p className="eyebrow">{route.projectId ? "Project workspace" : "Workspace overview"}</p><h1>{route.taskId ? "Task details" : currentProject?.name ?? "Projects"}</h1><p>{route.taskId ? "Review the request, manage progress, and investigate activity." : currentProject ? `${currentProject.github_repo} · ${currentProject.base_branch}` : "Open a project to manage its tasks, or add a repository to get started."}</p></div><div className="workspace-count"><strong>{overview?.projects.length ?? "—"}</strong> projects <span>/</span> <strong>{overview?.tasks.length ?? "—"}</strong> tasks</div></div>
          {error && (
            <div
              role="alert"
              className="bg-rose-50 border border-rose-200 text-rose-900 px-4 py-3 rounded text-sm"
            >
              {error}
              {!overview && <button type="button" className="ml-3 underline" onClick={() => void loadData()}>Retry</button>}
            </div>
          )}

          {notice && <p role="status" className="text-sm text-teal-800">{notice}</p>}
          {!overview && !error && <p role="status">Loading workspace…</p>}
          {/* Startup Recovery Summary Banner */}
          {overview?.recovery &&
            (overview.recovery.paused_tasks.length > 0 ||
              overview.recovery.diverged_entities.length > 0) && (
              <div className="bg-amber-50 border border-amber-200 text-amber-900 px-4 py-3 rounded text-sm">
                <strong>Work paused after restart.</strong>{" "}
                {overview.recovery.paused_tasks.length} task(s) and{" "}
                {overview.recovery.paused_phases.length} phase(s) paused on startup. Nothing
                resumes without your explicit approval.
              </div>
            )}

          {/* Agents Registry Status Bar */}
          {!route.projectId && <section className="agent-strip">
            <h2 className="text-sm font-semibold uppercase tracking-wider text-stone-500 mb-3">
              Your agents
            </h2>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
              {overview?.agents.length === 0 && <p className="text-sm text-stone-500">No agents yet. <a className="underline" href="#/settings">Connect an agent in Settings</a> to get started.</p>}
              {overview?.agents.map((ag) => (
                <div
                  key={ag.id}
                  className="bg-stone-50 border border-stone-200 rounded p-3 flex items-center justify-between"
                >
                  <div>
                    <div className="font-semibold text-sm text-stone-900">{ag.id}</div>
                    <div className="text-xs text-stone-500">
                      {ag.adapter} · {ag.provider}/{ag.model_family} · {ag.billing}
                    </div>
                  </div>
                  <span
                    className={`text-xs px-2 py-0.5 rounded border ${stateBadgeClass(
                      ag.capacity_state
                    )}`}
                  >
                    {readableState(ag.capacity_state)}
                  </span>
                </div>
              ))}
            </div>
          </section>}

          {overview && route.projectId && !currentProject && <div role="alert">This project has been removed or is unavailable. <a href="#/" className="underline">Return to projects</a></div>}
          <fieldset disabled={pending !== null} className="work-grid">
            {/* Project directory and registration */}
            {!route.projectId && <section className="work-panel space-y-4">
              <h2 className="text-base font-semibold text-stone-900">Projects</h2>
              <div className="space-y-2">
                {overview?.projects.length === 0 && (
                  <p className="text-sm text-stone-500">No repositories registered yet.</p>
                )}
                {overview?.projects.map((proj) => (
                  <button
                    key={proj.id}
                    aria-pressed={proj.id === selectedProjectId}
                    type="button"
                    onClick={() => { window.location.hash = projectPath(proj.id); }}
                    className={`w-full text-left p-3 rounded border transition ${
                      proj.id === selectedProjectId
                        ? "bg-stone-100 border-teal-700"
                        : "bg-stone-50 border-stone-200 hover:border-stone-300"
                    }`}
                  >
                    <div className="flex items-center justify-between">
                      <span className="font-medium text-sm text-stone-900">{proj.name}</span>
                      <span
                        className={`text-xs px-2 py-0.5 rounded border ${stateBadgeClass(
                          proj.status
                        )}`}
                      >
                        {proj.blocked_reason
                          ? `Needs attention: ${readableState(proj.blocked_reason)}`
                          : readableState(proj.status)}
                      </span>
                    </div>
                    <div className="text-xs text-stone-500 font-mono mt-1">
                      {proj.github_repo} ({proj.base_branch})
                    </div>
                  </button>
                ))}
              </div>

              <details className="composer" open={overview?.projects.length === 0}><summary>Add a project <span aria-hidden="true">+</span></summary><form
                onSubmit={handleRegisterProject}
                className="border-t border-stone-200 pt-4 space-y-3"
              >
                <h3 className="text-xs font-semibold uppercase tracking-wider text-stone-500">
                  Project details
                </h3>
                <label className="field-label">Project name<input
                  type="text"
                  placeholder="Project name (e.g. my-app)"
                  value={projName}
                  onChange={(e) => setProjName(e.target.value)}
                  required
                  className="w-full bg-stone-50 border border-stone-200 rounded px-3 py-1.5 text-sm text-stone-900"
                /></label>
                <label className="field-label">GitHub repository<input
                  type="text"
                  placeholder="owner/repository"
                  value={projRepo}
                  onChange={(e) => setProjRepo(e.target.value)}
                  required
                  className="w-full bg-stone-50 border border-stone-200 rounded px-3 py-1.5 text-sm text-stone-900 font-mono"
                /></label>
                <label className="field-label">Base branch<input
                  type="text"
                  placeholder="Base branch (main)"
                  value={projBranch}
                  onChange={(e) => setProjBranch(e.target.value)}
                  required
                  className="w-full bg-stone-50 border border-stone-200 rounded px-3 py-1.5 text-sm text-stone-900"
                /></label>
                <button
                  type="submit"
                  className="w-full bg-teal-800 hover:bg-teal-900 text-stone-900 font-medium text-sm py-1.5 px-3 rounded"
                >
                  {pending === "project" ? "Adding project…" : "Register Project"}
                </button>
                {(projName || projRepo || projBranch !== "main") && <button type="button" className="text-sm text-stone-500" onClick={() => { setProjName(""); setProjRepo(""); setProjBranch("main"); }}>Clear project form</button>}
              </form></details>
            </section>}

            {/* Project task workspace */}
            {currentProject && !route.taskId && <section className="work-panel space-y-4">
              <div className="project-toolbar"><h2 className="text-base font-semibold text-stone-900">Tasks <span className="text-stone-500">({projectTasks.length})</span></h2><a href="#/" className="text-sm underline">← All projects</a></div>
              <details className="project-management"><summary>Project settings</summary><ProjectControls key={currentProject.id} project={currentProject} onChanged={async removed => { await loadData(); if (removed) window.location.hash = "#/"; }} /></details>

              {currentProject && currentProject.blocked_reason === "github_credential" && (
                <div className="bg-amber-50 border border-amber-200 rounded p-3 text-xs text-amber-900 space-y-1">
                  <div className="font-semibold">
                    Connect GitHub to start working on this project
                  </div>
                  <a href="#/settings" className="underline hover:text-amber-900">
                    Add a fine-grained personal access token in Settings
                  </a>
                </div>
              )}

              <div className="space-y-2">
                {projectTasks.length === 0 && (
                  <p className="text-sm text-stone-500">No tasks created for this project.</p>
                )}
                {projectTasks.map((t) => (
                  <button
                    key={t.id}
                    aria-pressed={selectedTask?.id === t.id}
                    type="button"
                    onClick={() => { window.location.hash = `${projectPath(currentProject.id)}/tasks/${encodeURIComponent(t.id)}`; }}
                    className={`w-full text-left p-3 rounded border transition ${
                      selectedTask?.id === t.id
                        ? "bg-stone-100 border-teal-700"
                        : "bg-stone-50 border-stone-200 hover:border-stone-300"
                    }`}
                  >
                    <div className="flex items-center justify-between">
                      <span className="font-medium text-sm text-stone-900">{t.roadmap_seq ? `Phase ${t.roadmap_seq}: ` : ""}{t.title}</span>
                      <span
                        className={`text-xs px-2 py-0.5 rounded border ${stateBadgeClass(
                          t.state
                        )}`}
                      >
                        {t.blocked_reason ? `Needs attention: ${readableState(t.blocked_reason)}` : readableState(t.state)}
                      </span>
                    </div>
                    <div className="text-xs text-stone-500 mt-1">
                      {t.category} · Priority P{t.priority} · v{t.version}
                    </div>
                  </button>
                ))}
              </div>

              <details className="composer" open={projectTasks.length === 0}><summary>Plan project in phases <span aria-hidden="true">+</span></summary><form
                onSubmit={handleCreateRoadmap}
                className="border-t border-stone-200 pt-4 space-y-3"
              >
                <p className="text-sm text-stone-500">Describe what you want to build. The architect agent writes a phased plan with acceptance criteria, and each phase is added below as a task.</p>
                <label className="field-label">Project goal<textarea
                  placeholder="Describe the project or outcome you want..."
                  value={roadmapGoal}
                  onChange={(e) => setRoadmapGoal(e.target.value)}
                  required
                  minLength={10}
                  rows={4}
                  className="w-full bg-stone-50 border border-stone-200 rounded px-3 py-1.5 text-sm text-stone-900"
                /></label>
                <button
                  type="submit"
                  disabled={pending === "roadmap"}
                  className="w-full bg-teal-800 hover:bg-teal-900 text-stone-900 font-medium text-sm py-1.5 px-3 rounded"
                >
                  {pending === "roadmap" ? "Planning… this can take a minute" : "Create plan"}
                </button>
              </form></details>

              {currentProject && (
                <details className="composer" open={false}><summary>New task <span aria-hidden="true">+</span></summary><form
                  onSubmit={handleCreateTask}
                  className="border-t border-stone-200 pt-4 space-y-3"
                >
                  <h3 className="text-xs font-semibold uppercase tracking-wider text-stone-500">
                    New Task
                  </h3>
                  <label className="field-label">Task title<input
                    type="text"
                    placeholder="Task title (e.g. Add Google OAuth login)"
                    value={taskTitle}
                    onChange={(e) => setTaskTitle(e.target.value)}
                    required
                    className="w-full bg-stone-50 border border-stone-200 rounded px-3 py-1.5 text-sm text-stone-900"
                  /></label>
                  <label className="field-label">What would you like to change?<textarea
                    placeholder="Describe the requested change..."
                    value={taskRequest}
                    onChange={(e) => setTaskRequest(e.target.value)}
                    required
                    rows={3}
                    className="w-full bg-stone-50 border border-stone-200 rounded px-3 py-1.5 text-sm text-stone-900"
                  /></label>
                  <label className="field-label">Task category<select
                    aria-label="Task category"
                    value={taskCategory}
                    onChange={(e) => setTaskCategory(e.target.value)}
                    className="w-full bg-stone-50 border border-stone-200 rounded px-3 py-1.5 text-sm text-stone-900"
                  >
                    <option value="feature">Feature</option>
                    <option value="bug_fix">Bug fix</option>
                    <option value="refactor">Refactor</option>
                    <option value="security">Security</option>
                    <option value="tests">Tests</option>
                    <option value="performance">Performance</option>
                    <option value="dependencies">Dependencies</option>
                    <option value="ui">User interface</option>
                    <option value="investigation">Investigation</option>
                    <option value="modernisation">Modernisation</option>
                  </select></label>
                  <label className="field-label">Priority<select className="w-full border" value={taskPriority} onChange={e => setTaskPriority(Number(e.target.value))}>
                    <option value={1}>P1 — Urgent</option><option value={2}>P2 — High</option><option value={3}>P3 — Normal</option><option value={4}>P4 — Low</option><option value={5}>P5 — When possible</option>
                  </select></label>
                  <label className="field-label">Acceptance criterion (optional)<input
                    type="text"
                    placeholder="Acceptance criterion (optional)"
                    value={taskCriterion}
                    onChange={(e) => setTaskCriterion(e.target.value)}
                    className="w-full bg-stone-50 border border-stone-200 rounded px-3 py-1.5 text-sm text-stone-900"
                  /></label>
                  <button
                    type="submit"
                    className="w-full bg-teal-800 hover:bg-teal-900 text-stone-900 font-medium text-sm py-1.5 px-3 rounded"
                  >
                    {pending === "task" ? "Creating task…" : "Create Task"}
                  </button>
                  {(taskTitle || taskRequest || taskCriterion) && <button type="button" className="text-sm text-stone-500" onClick={() => { setTaskTitle(""); setTaskRequest(""); setTaskCriterion(""); setTaskCategory("feature"); setTaskPriority(3); }}>Discard task draft</button>}
                </form></details>
              )}
            </section>}

            {/* Task detail and persisted activity */}
            {currentProject && route.taskId && <section className="work-panel space-y-4">
              <h2 className="text-base font-semibold text-stone-900">
                <a href={projectPath(currentProject.id)} className="text-sm underline">← Back to {currentProject.name} tasks</a>
              </h2>
              {!selectedTask ? (
                <p className="text-sm text-stone-500">
                  {error ? "Task details could not be loaded. Return to the project to choose a task." : "Loading task details…"}
                </p>
              ) : (
                <div className="space-y-4">
                  <div className="bg-stone-50 border border-stone-200 rounded p-3 space-y-2">
                    <div className="flex items-center justify-between">
                      <h3 className="font-semibold text-stone-900">{selectedTask.title}</h3>
                      <span
                        data-testid="selected-task-state"
                        className={`text-xs px-2 py-0.5 rounded border ${stateBadgeClass(
                          selectedTask.state
                        )}`}
                      >
                        {selectedTask.blocked_reason
                          ? `Needs attention: ${readableState(selectedTask.blocked_reason)}`
                          : readableState(selectedTask.state)}
                      </span>
                    </div>
                    <p className="text-xs text-stone-600">{selectedTask.request_text}</p>
                    {selectedTask.resume_to && (
                      <div className="text-xs text-amber-800 font-mono">
                        Resumes from: {selectedTask.resume_to}
                      </div>
                    )}

                    {/* Day-to-day Controls */}
                    {pending === "action" && <p role="status" className="text-sm">Updating task…</p>}
                    <div className="flex flex-wrap gap-2 pt-2">
                      {selectedTask.state === "CREATED" && (
                        <button
                          type="button"
                          onClick={() => void handleTaskAction(selectedTask.id, "start")}
                          className="bg-teal-800 hover:bg-teal-900 text-stone-900 text-xs font-medium px-3 py-1 rounded"
                        >
                          Start
                        </button>
                      )}
                      {selectedTask.state !== "PAUSED" &&
                        selectedTask.state !== "CANCELLED" &&
                        selectedTask.state !== "COMPLETED" && (
                          <button
                            type="button"
                            onClick={() => void handleTaskAction(selectedTask.id, "pause")}
                            className="bg-amber-600 hover:bg-amber-500 text-stone-900 text-xs font-medium px-3 py-1 rounded"
                          >
                            Pause
                          </button>
                        )}
                      {selectedTask.state === "PAUSED" && (
                        <button
                          type="button"
                          onClick={() => void handleTaskAction(selectedTask.id, "resume")}
                          className="bg-teal-800 hover:bg-teal-900 text-stone-900 text-xs font-medium px-3 py-1 rounded"
                        >
                          Resume
                        </button>
                      )}
                      {selectedTask.blocked_reason === "engine_state_divergence" && (
                        <button
                          type="button"
                          onClick={() => void handleTaskAction(selectedTask.id, "rebuild")}
                          className="bg-purple-600 hover:bg-purple-500 text-stone-900 text-xs font-medium px-3 py-1 rounded"
                        >
                          Rebuild
                        </button>
                      )}
                      {selectedTask.state !== "CANCELLED" &&
                        selectedTask.state !== "COMPLETED" && (
                          <button
                            type="button"
                            onClick={() => void handleTaskAction(selectedTask.id, "cancel")}
                            className="bg-rose-700 hover:bg-rose-600 text-stone-900 text-xs font-medium px-3 py-1 rounded"
                          >
                            Cancel
                          </button>
                        )}
                    </div>
                  </div>

                  {selectedTask.acceptance_criteria && selectedTask.acceptance_criteria.length > 0 && <div className="space-y-2"><h4 className="text-sm font-semibold">Acceptance criteria</h4><ul className="list-disc pl-4 space-y-1 text-sm text-stone-600">{selectedTask.acceptance_criteria.map(criterion => <li key={criterion.id}>{criterion.text}{criterion.mandatory ? "" : " (optional)"}</li>)}</ul></div>}
                  {/* Timeline strictly from persisted transitions */}
                  <div className="space-y-2">
                    <h4 className="text-xs font-semibold uppercase tracking-wider text-stone-500">
                      Activity
                    </h4>
                    <ul
                      data-testid="transitions-timeline"
                      className="space-y-2 max-h-96 overflow-y-auto"
                    >
                      {selectedTask.timeline?.map((tr: TransitionRecord) => (
                        <li
                          key={tr.id}
                          className="bg-stone-50 border border-stone-200 rounded p-2.5 text-xs space-y-1"
                        >
                          <div className="flex items-center justify-between font-mono">
                            <span className="text-teal-800">
                              {tr.from_state ?? "∅"} → {tr.to_state}
                              {tr.underlying === 1 ? " (underlying)" : ""}
                            </span>
                            <span className="text-stone-500">{tr.actor}</span>
                          </div>
                          <div className="text-stone-600">{tr.reason}</div>
                          <div className="text-[11px] text-stone-500 font-mono">
                            policy v{tr.policy_version} · {tr.at}
                          </div>
                        </li>
                      ))}
                    </ul>
                  </div>
                  <TaskHistory key={selectedTask.id} taskId={selectedTask.id} version={selectedTask.version} />
                </div>
              )}
            </section>}
          </fieldset>
        </main>
      )}
    </div>
  );
}
