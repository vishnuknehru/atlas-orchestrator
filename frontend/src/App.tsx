import React, { useCallback, useEffect, useState } from "react";
import {
  atlasApi,
  OverviewResponse,
  ProjectRecord,
  TaskRecord,
  TransitionRecord,
} from "./api";
import { Settings } from "./Settings";

type View = "dashboard" | "settings";

function viewFromHash(): View {
  return window.location.hash === "#/settings" ? "settings" : "dashboard";
}

function stateBadgeClass(state: string): string {
  if (state === "COMPLETED" || state === "active") {
    return "bg-emerald-900/60 text-emerald-300 border-emerald-700";
  }
  if (state === "PAUSED" || state.startsWith("AWAITING")) {
    return "bg-amber-900/60 text-amber-300 border-amber-700";
  }
  if (state === "BLOCKED" || state === "blocked" || state === "FAILED") {
    return "bg-rose-900/60 text-rose-300 border-rose-700";
  }
  if (state === "CANCELLED") {
    return "bg-slate-800 text-slate-400 border-slate-700";
  }
  return "bg-sky-900/60 text-sky-300 border-sky-700";
}

export function App(): React.ReactElement {
  const [view, setView] = useState<View>(viewFromHash);
  const [overview, setOverview] = useState<OverviewResponse | null>(null);
  const [selectedProjectId, setSelectedProjectId] = useState<string | null>(null);
  const [selectedTask, setSelectedTask] = useState<TaskRecord | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Project registration form state
  const [projName, setProjName] = useState("");
  const [projRepo, setProjRepo] = useState("");
  const [projBranch, setProjBranch] = useState("main");

  // Task creation form state
  const [taskTitle, setTaskTitle] = useState("");
  const [taskRequest, setTaskRequest] = useState("");
  const [taskCategory, setTaskCategory] = useState("feature");
  const [taskCriterion, setTaskCriterion] = useState("");

  const loadData = useCallback(async () => {
    try {
      const data = await atlasApi.getOverview();
      setOverview(data);
      if (!selectedProjectId && data.projects.length > 0) {
        setSelectedProjectId(data.projects[0].id);
      }
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load ATLAS overview");
    }
  }, [selectedProjectId]);

  const loadTaskDetail = useCallback(async (taskId: string) => {
    try {
      const detail = await atlasApi.getTask(taskId);
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
    const onHashChange = () => setView(viewFromHash());
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  const handleRegisterProject = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const created = await atlasApi.registerProject({
        name: projName,
        repository: projRepo,
        base_branch: projBranch,
        visibility: "private",
      });
      setProjName("");
      setProjRepo("");
      setSelectedProjectId(created.id);
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to register project");
    }
  };

  const handleCreateTask = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedProjectId) return;
    try {
      const created = await atlasApi.createTask(selectedProjectId, {
        title: taskTitle,
        request_text: taskRequest,
        category: taskCategory,
        priority: 3,
        acceptance_criteria: taskCriterion
          ? [{ text: taskCriterion, mandatory: true }]
          : [],
      });
      setTaskTitle("");
      setTaskRequest("");
      setTaskCriterion("");
      await loadData();
      await loadTaskDetail(created.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create task");
    }
  };

  const handleTaskAction = async (
    taskId: string,
    action: "start" | "pause" | "resume" | "cancel" | "rebuild"
  ) => {
    try {
      await atlasApi.taskAction(taskId, action);
      await loadData();
      await loadTaskDetail(taskId);
    } catch (err) {
      setError(err instanceof Error ? err.message : `Failed to ${action} task`);
    }
  };

  const currentProject: ProjectRecord | undefined = overview?.projects.find(
    (p) => p.id === selectedProjectId
  );
  const projectTasks: TaskRecord[] =
    overview?.tasks.filter((t) => t.project_id === selectedProjectId) || [];

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 p-6">
      <header className="max-w-7xl mx-auto flex items-center justify-between border-b border-slate-800 pb-4 mb-6">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-white">
            ATLAS Orchestrator
          </h1>
          <p className="text-sm text-slate-400">
            Local-first supervised multi-agent software engineering orchestrator (127.0.0.1)
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
                  view === v ? "bg-slate-800 text-white" : "text-slate-400 hover:text-white"
                }`}
              >
                {v}
              </a>
            ))}
          </nav>
          <div className="text-xs font-mono text-slate-400 bg-slate-900 border border-slate-800 px-3 py-1.5 rounded">
            v{overview?.version ?? "0.1.0"} · Mode: supervised
          </div>
        </div>
      </header>

      {view === "settings" ? (
        <main className="max-w-7xl mx-auto">
          <Settings />
        </main>
      ) : (
        <main className="max-w-7xl mx-auto space-y-6">
          {error && (
            <div
              role="alert"
              className="bg-rose-950/80 border border-rose-800 text-rose-200 px-4 py-3 rounded text-sm"
            >
              {error}
            </div>
          )}

          {/* Startup Recovery Summary Banner */}
          {overview?.recovery &&
            (overview.recovery.paused_tasks.length > 0 ||
              overview.recovery.diverged_entities.length > 0) && (
              <div className="bg-amber-950/60 border border-amber-800 text-amber-200 px-4 py-3 rounded text-sm">
                <strong>Safe Restart Recovery Summary:</strong>{" "}
                {overview.recovery.paused_tasks.length} task(s) and{" "}
                {overview.recovery.paused_phases.length} phase(s) paused on startup. Nothing
                resumes without your explicit approval.
              </div>
            )}

          {/* Agents Registry Status Bar */}
          <section className="bg-slate-900/70 border border-slate-800 rounded-lg p-4">
            <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-400 mb-3">
              Configured Agent Registry (~/.atlas/agents.yaml)
            </h2>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
              {overview?.agents.map((ag) => (
                <div
                  key={ag.id}
                  className="bg-slate-950 border border-slate-800 rounded p-3 flex items-center justify-between"
                >
                  <div>
                    <div className="font-semibold text-sm text-white">{ag.id}</div>
                    <div className="text-xs text-slate-400">
                      {ag.adapter} · {ag.provider}/{ag.model_family} · {ag.billing}
                    </div>
                  </div>
                  <span
                    className={`text-xs px-2 py-0.5 rounded border ${stateBadgeClass(
                      ag.capacity_state
                    )}`}
                  >
                    {ag.capacity_state}
                  </span>
                </div>
              ))}
            </div>
          </section>

          <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
            {/* Left Column: Projects & Registration */}
            <section className="bg-slate-900/70 border border-slate-800 rounded-lg p-4 space-y-4">
              <h2 className="text-base font-semibold text-white">Projects</h2>
              <div className="space-y-2">
                {overview?.projects.length === 0 && (
                  <p className="text-sm text-slate-400">No repositories registered yet.</p>
                )}
                {overview?.projects.map((proj) => (
                  <button
                    key={proj.id}
                    type="button"
                    onClick={() => {
                      setSelectedProjectId(proj.id);
                      setSelectedTask(null);
                    }}
                    className={`w-full text-left p-3 rounded border transition ${
                      proj.id === selectedProjectId
                        ? "bg-slate-800 border-sky-600"
                        : "bg-slate-950 border-slate-800 hover:border-slate-700"
                    }`}
                  >
                    <div className="flex items-center justify-between">
                      <span className="font-medium text-sm text-white">{proj.name}</span>
                      <span
                        className={`text-xs px-2 py-0.5 rounded border ${stateBadgeClass(
                          proj.status
                        )}`}
                      >
                        {proj.blocked_reason
                          ? `BLOCKED(${proj.blocked_reason})`
                          : proj.status}
                      </span>
                    </div>
                    <div className="text-xs text-slate-400 font-mono mt-1">
                      {proj.github_repo} ({proj.base_branch})
                    </div>
                  </button>
                ))}
              </div>

              <form
                onSubmit={handleRegisterProject}
                className="border-t border-slate-800 pt-4 space-y-3"
              >
                <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-400">
                  Register Repository
                </h3>
                <input
                  type="text"
                  placeholder="Project name (e.g. my-app)"
                  value={projName}
                  onChange={(e) => setProjName(e.target.value)}
                  required
                  className="w-full bg-slate-950 border border-slate-800 rounded px-3 py-1.5 text-sm text-white"
                />
                <input
                  type="text"
                  placeholder="owner/repository"
                  value={projRepo}
                  onChange={(e) => setProjRepo(e.target.value)}
                  required
                  className="w-full bg-slate-950 border border-slate-800 rounded px-3 py-1.5 text-sm text-white font-mono"
                />
                <input
                  type="text"
                  placeholder="Base branch (main)"
                  value={projBranch}
                  onChange={(e) => setProjBranch(e.target.value)}
                  required
                  className="w-full bg-slate-950 border border-slate-800 rounded px-3 py-1.5 text-sm text-white"
                />
                <button
                  type="submit"
                  className="w-full bg-sky-600 hover:bg-sky-500 text-white font-medium text-sm py-1.5 px-3 rounded"
                >
                  Register Project
                </button>
              </form>
            </section>

            {/* Middle Column: Project Tasks & New Task Form */}
            <section className="bg-slate-900/70 border border-slate-800 rounded-lg p-4 space-y-4">
              <h2 className="text-base font-semibold text-white">
                {currentProject ? `Tasks — ${currentProject.name}` : "Tasks"}
              </h2>

              {currentProject && currentProject.blocked_reason === "github_credential" && (
                <div className="bg-amber-950/50 border border-amber-800 rounded p-3 text-xs text-amber-200 space-y-1">
                  <div className="font-semibold">
                    BLOCKED(github_credential): this project needs a GitHub token
                  </div>
                  <a href="#/settings" className="underline hover:text-amber-100">
                    Add a fine-grained personal access token in Settings
                  </a>
                </div>
              )}

              <div className="space-y-2">
                {projectTasks.length === 0 && (
                  <p className="text-sm text-slate-400">No tasks created for this project.</p>
                )}
                {projectTasks.map((t) => (
                  <button
                    key={t.id}
                    type="button"
                    onClick={() => void loadTaskDetail(t.id)}
                    className={`w-full text-left p-3 rounded border transition ${
                      selectedTask?.id === t.id
                        ? "bg-slate-800 border-sky-600"
                        : "bg-slate-950 border-slate-800 hover:border-slate-700"
                    }`}
                  >
                    <div className="flex items-center justify-between">
                      <span className="font-medium text-sm text-white">{t.title}</span>
                      <span
                        className={`text-xs px-2 py-0.5 rounded border ${stateBadgeClass(
                          t.state
                        )}`}
                      >
                        {t.blocked_reason ? `BLOCKED(${t.blocked_reason})` : t.state}
                      </span>
                    </div>
                    <div className="text-xs text-slate-400 mt-1">
                      {t.category} · Priority P{t.priority} · v{t.version}
                    </div>
                  </button>
                ))}
              </div>

              {currentProject && (
                <form
                  onSubmit={handleCreateTask}
                  className="border-t border-slate-800 pt-4 space-y-3"
                >
                  <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-400">
                    New Task
                  </h3>
                  <input
                    type="text"
                    placeholder="Task title (e.g. Add Google OAuth login)"
                    value={taskTitle}
                    onChange={(e) => setTaskTitle(e.target.value)}
                    required
                    className="w-full bg-slate-950 border border-slate-800 rounded px-3 py-1.5 text-sm text-white"
                  />
                  <textarea
                    placeholder="Describe the requested change..."
                    value={taskRequest}
                    onChange={(e) => setTaskRequest(e.target.value)}
                    required
                    rows={3}
                    className="w-full bg-slate-950 border border-slate-800 rounded px-3 py-1.5 text-sm text-white"
                  />
                  <select
                    aria-label="Task category"
                    value={taskCategory}
                    onChange={(e) => setTaskCategory(e.target.value)}
                    className="w-full bg-slate-950 border border-slate-800 rounded px-3 py-1.5 text-sm text-white"
                  >
                    <option value="feature">feature</option>
                    <option value="bug_fix">bug_fix</option>
                    <option value="refactor">refactor</option>
                    <option value="security">security</option>
                    <option value="tests">tests</option>
                  </select>
                  <input
                    type="text"
                    placeholder="Acceptance criterion (optional)"
                    value={taskCriterion}
                    onChange={(e) => setTaskCriterion(e.target.value)}
                    className="w-full bg-slate-950 border border-slate-800 rounded px-3 py-1.5 text-sm text-white"
                  />
                  <button
                    type="submit"
                    className="w-full bg-emerald-600 hover:bg-emerald-500 text-white font-medium text-sm py-1.5 px-3 rounded"
                  >
                    Create Task
                  </button>
                </form>
              )}
            </section>

            {/* Right Column: Task Detail, Controls & Persisted Transitions Timeline */}
            <section className="bg-slate-900/70 border border-slate-800 rounded-lg p-4 space-y-4">
              <h2 className="text-base font-semibold text-white">
                Task State &amp; Persisted Timeline
              </h2>
              {!selectedTask ? (
                <p className="text-sm text-slate-400">
                  Select a task to inspect its controls and persisted transition timeline.
                </p>
              ) : (
                <div className="space-y-4">
                  <div className="bg-slate-950 border border-slate-800 rounded p-3 space-y-2">
                    <div className="flex items-center justify-between">
                      <h3 className="font-semibold text-white">{selectedTask.title}</h3>
                      <span
                        data-testid="selected-task-state"
                        className={`text-xs px-2 py-0.5 rounded border ${stateBadgeClass(
                          selectedTask.state
                        )}`}
                      >
                        {selectedTask.blocked_reason
                          ? `BLOCKED(${selectedTask.blocked_reason})`
                          : selectedTask.state}
                      </span>
                    </div>
                    <p className="text-xs text-slate-300">{selectedTask.request_text}</p>
                    {selectedTask.resume_to && (
                      <div className="text-xs text-amber-300 font-mono">
                        resume_to: {selectedTask.resume_to}
                      </div>
                    )}

                    {/* Day-to-day Controls */}
                    <div className="flex flex-wrap gap-2 pt-2">
                      {selectedTask.state === "CREATED" && (
                        <button
                          type="button"
                          onClick={() => void handleTaskAction(selectedTask.id, "start")}
                          className="bg-sky-600 hover:bg-sky-500 text-white text-xs font-medium px-3 py-1 rounded"
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
                            className="bg-amber-600 hover:bg-amber-500 text-white text-xs font-medium px-3 py-1 rounded"
                          >
                            Pause
                          </button>
                        )}
                      {selectedTask.state === "PAUSED" && (
                        <button
                          type="button"
                          onClick={() => void handleTaskAction(selectedTask.id, "resume")}
                          className="bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-medium px-3 py-1 rounded"
                        >
                          Resume
                        </button>
                      )}
                      {selectedTask.blocked_reason === "engine_state_divergence" && (
                        <button
                          type="button"
                          onClick={() => void handleTaskAction(selectedTask.id, "rebuild")}
                          className="bg-purple-600 hover:bg-purple-500 text-white text-xs font-medium px-3 py-1 rounded"
                        >
                          Rebuild
                        </button>
                      )}
                      {selectedTask.state !== "CANCELLED" &&
                        selectedTask.state !== "COMPLETED" && (
                          <button
                            type="button"
                            onClick={() => void handleTaskAction(selectedTask.id, "cancel")}
                            className="bg-rose-700 hover:bg-rose-600 text-white text-xs font-medium px-3 py-1 rounded"
                          >
                            Cancel
                          </button>
                        )}
                    </div>
                  </div>

                  {/* Timeline strictly from persisted transitions */}
                  <div className="space-y-2">
                    <h4 className="text-xs font-semibold uppercase tracking-wider text-slate-400">
                      Timeline (from transitions table only)
                    </h4>
                    <ul
                      data-testid="transitions-timeline"
                      className="space-y-2 max-h-96 overflow-y-auto"
                    >
                      {selectedTask.timeline?.map((tr: TransitionRecord) => (
                        <li
                          key={tr.id}
                          className="bg-slate-950 border border-slate-800 rounded p-2.5 text-xs space-y-1"
                        >
                          <div className="flex items-center justify-between font-mono">
                            <span className="text-sky-300">
                              {tr.from_state ?? "∅"} → {tr.to_state}
                              {tr.underlying === 1 ? " (underlying)" : ""}
                            </span>
                            <span className="text-slate-400">{tr.actor}</span>
                          </div>
                          <div className="text-slate-300">{tr.reason}</div>
                          <div className="text-[11px] text-slate-400 font-mono">
                            policy v{tr.policy_version} · {tr.at}
                          </div>
                        </li>
                      ))}
                    </ul>
                  </div>
                </div>
              )}
            </section>
          </div>
        </main>
      )}
    </div>
  );
}
