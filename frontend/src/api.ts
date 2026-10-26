export interface AgentSummary {
  id: string;
  adapter: string;
  billing: string;
  models: string[];
  provider: string;
  model_family: string;
  endpoint?: string | null;
  allow_remote_endpoint: boolean;
  capacity_state: string;
  auth_status: string;
  current_revision: number;
}

export interface ProjectRecord {
  id: string;
  name: string;
  github_repo: string;
  visibility: "private" | "public" | "internal";
  base_branch: string;
  protection_status: string;
  policy_version: number;
  status: string;
  blocked_reason?: string | null;
  created_at: string;
  updated_at: string;
}

export interface TransitionRecord {
  id: string;
  project_id: string;
  entity_type: "task" | "phase";
  entity_id: string;
  from_state: string | null;
  to_state: string;
  reason: string;
  actor: string;
  candidate_id: string | null;
  policy_version: number;
  underlying: number;
  evidence_refs_json: string;
  correlation_id: string;
  at: string;
}

export interface PhaseRecord {
  id: string;
  project_id: string;
  task_id: string;
  seq: number;
  title: string;
  objective: string;
  state: string;
  resume_to?: string | null;
  blocked_reason?: string | null;
  current_candidate_id?: string | null;
}

export interface TaskRecord {
  id: string;
  project_id: string;
  title: string;
  request_text: string;
  category: string;
  priority: number;
  state: string;
  resume_to?: string | null;
  blocked_reason?: string | null;
  version: number;
  generation: number;
  created_at: string;
  updated_at: string;
  acceptance_criteria?: Array<{ id: string; text: string; mandatory: boolean }>;
  roadmap_id?: string | null;
  roadmap_seq?: number | null;
  phases?: PhaseRecord[];
  timeline?: TransitionRecord[];
}

export interface PlanFinding {
  id: string;
  severity: "critical" | "high" | "medium" | "low";
  text: string;
}

export type PlanHistoryEntry =
  | { kind: "challenge"; cycle: number; revision: number; challenger: string; verdict: "approve" | "revise"; findings: PlanFinding[] }
  | { kind: "revision"; revision: number; responses: Array<{ finding_id: string; response: string }> }
  | { kind: "user_feedback"; text: string };

export interface TaskPlan {
  author_agent: string;
  revision: number;
  approach: string;
  steps: Array<{ title: string; description: string; files: string[]; criteria: string[] }>;
  risks: string[];
  escalated: boolean;
  history: PlanHistoryEntry[];
}

export interface TaskCandidate {
  author_agent: string;
  plan_revision: number;
  base_sha: string;
  commit_sha: string;
  worktree: string;
  changed_files: string[];
  guard_status: "clear" | "held";
  violations: Array<{ path: string; kind: string }>;
  summary: string;
}

export interface SandboxCheck {
  id: string;
  label: string;
  ok: boolean;
  detail: string;
}

export interface SandboxStatus {
  ready: boolean;
  checks: SandboxCheck[];
  start_command?: string;
  volume_command?: string;
}

export interface RoadmapRecord {
  id: string;
  project_id: string;
  goal: string;
  summary: string;
  author_agent: string;
  created_at: string;
  tasks: TaskRecord[];
}

export interface TaskHistoryResponse {
  schema_version: number;
  exported_at: string;
  task: TaskRecord;
  workflow_thread_id: string;
  executions: Array<{ id: string; kind: string; agent_id: string | null; role: string | null; model: string | null; status: string; exit_code: number | null; error_detail: string | null; correlation_id: string; started_at: string | null; finished_at: string | null }>;
  workflow_steps: Array<{ step_id: string; node: string; status: string; generation: number; created_at: string; completed_at: string | null }>;
  artifacts: Array<{ id: string; kind: string; sha256: string; execution_id: string | null }>;
  approvals: Array<{ id: string; kind: string; status: string; decided_by: string | null; reason: string | null }>;
  remote_operations: Array<{ id: string; op: string; status: string; attempts: number; last_error: string | null }>;
}

export interface OverviewResponse {
  version: string;
  recovery: {
    ok: boolean;
    paused_tasks: string[];
    paused_phases: string[];
    diverged_entities: Array<{ entity_type: string; entity_id: string; reason: string }>;
    completed_at: string;
  };
  projects: ProjectRecord[];
  tasks: TaskRecord[];
  agents: AgentSummary[];
}

export type Billing = "subscription" | "api_key" | "local" | "manual";

export interface AgentConfigEntry {
  adapter: string;
  models: string[];
  billing: Billing;
  provider?: string | null;
  model_family?: string | null;
  endpoint?: string | null;
  allow_remote_endpoint: boolean;
  declared_family_required: boolean;
}

export interface AgentRegistry {
  agents: Record<string, AgentConfigEntry>;
}

export interface RoleEvaluation {
  ok: boolean;
  blocked_reason: string | null;
  detail: string | null;
  warnings: string[];
}

export interface SettingsProject {
  id: string;
  name: string;
  github_repo: string;
  status: string;
  blocked_reason: string | null;
  roles: Record<string, string>;
  role_evaluation: RoleEvaluation;
  /** null when the OS secret store can't be read */
  github_credential_configured: boolean | null;
}

export interface SettingsResponse {
  secret_store_available: boolean;
  roles: string[];
  adapters: Array<{
    name: string;
    kind: string;
    default_provider: string | null;
    allowed_roles: string[];
    suggested_models?: string[];
  }>;
  registry: AgentRegistry;
  agent_api_keys: Record<string, boolean | null>;
  projects: SettingsProject[];
  global_config: Record<string, Record<string, unknown>>;
}

export interface CredentialResult {
  project: ProjectRecord;
  verified: boolean;
  error_reason: string | null;
  error_detail?: string | null;
  role_evaluation: RoleEvaluation;
}

function formatErrorDetail(detail: unknown, fallback: string): string {
  if (typeof detail === "string") return detail;
  // FastAPI validation errors: [{loc: [...], msg: "..."}]
  if (Array.isArray(detail)) {
    return detail
      .map((d: { loc?: unknown[]; msg?: string }) => {
        const loc = (d.loc ?? []).filter((part) => part !== "body").join(".");
        return loc ? `${loc}: ${d.msg ?? ""}` : (d.msg ?? "");
      })
      .join("; ");
  }
  return fallback;
}

export function getCsrfCookie(): string {
  const match = document.cookie
    .split("; ")
    .find((row) => row.startsWith("atlas_csrf="));
  return match ? decodeURIComponent(match.split("=")[1]) : "";
}

async function apiRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers || {});
  if (init?.method && init.method.toUpperCase() !== "GET") {
    headers.set("X-Atlas-CSRF", getCsrfCookie());
    if (!headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
  }
  const resp = await fetch(path, {
    ...init,
    headers,
    credentials: "same-origin",
  });
  if (!resp.ok) {
    if (resp.status === 405) {
      throw new Error("The server does not support this action. Restart Atlas to load the latest changes, then try again.");
    }
    const errBody = await resp.json().catch(() => ({ detail: resp.statusText }));
    throw new Error(formatErrorDetail(errBody.detail, `HTTP ${resp.status}`));
  }
  return (await resp.json()) as T;
}

export const atlasApi = {
  getOverview: () => apiRequest<OverviewResponse>("/api/v1/overview"),
  registerProject: (payload: {
    name: string;
    repository: string;
    base_branch: string;
    visibility: "private" | "public" | "internal";
  }) =>
    apiRequest<ProjectRecord>("/api/v1/projects", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  updateProject: (projectId: string, payload: { name: string; base_branch: string }) =>
    apiRequest<ProjectRecord>(`/api/v1/projects/${projectId}/config`, { method: "PUT", body: JSON.stringify(payload) }),
  removeProject: (projectId: string) =>
    apiRequest<ProjectRecord>(`/api/v1/projects/${projectId}`, { method: "DELETE" }),
  getArchivedProjects: () => apiRequest<ProjectRecord[]>("/api/v1/projects?archived=true"),
  restoreProject: (projectId: string) =>
    apiRequest<ProjectRecord>(`/api/v1/projects/${projectId}/restore`, { method: "POST" }),
  createTask: (
    projectId: string,
    payload: {
      title: string;
      request_text: string;
      category: string;
      priority: number;
      acceptance_criteria: Array<{ text: string; mandatory: boolean }>;
    }
  ) =>
    apiRequest<TaskRecord>(`/api/v1/projects/${projectId}/tasks`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  createRoadmap: (projectId: string, goal: string) =>
    apiRequest<RoadmapRecord>(`/api/v1/projects/${projectId}/roadmap`, {
      method: "POST",
      body: JSON.stringify({ goal }),
    }),
  getTaskHistory: (taskId: string) => apiRequest<TaskHistoryResponse>(`/api/v1/tasks/${taskId}/history`),
  getSandbox: () => apiRequest<SandboxStatus>("/api/v1/sandbox"),
  runSandboxSelfTest: () => apiRequest<SandboxStatus>("/api/v1/sandbox/selftest", { method: "POST" }),
  getTaskPlan: (taskId: string) => apiRequest<TaskPlan>(`/api/v1/tasks/${taskId}/plan`),
  getTaskCandidate: (taskId: string) => apiRequest<TaskCandidate>(`/api/v1/tasks/${taskId}/candidate`),
  approvePlan: (taskId: string) =>
    apiRequest<TaskRecord>(`/api/v1/tasks/${taskId}/plan/approve`, { method: "POST" }),
  requestPlanChanges: (taskId: string, comment: string) =>
    apiRequest<TaskRecord>(`/api/v1/tasks/${taskId}/plan/request-changes`, {
      method: "POST",
      body: JSON.stringify({ comment }),
    }),
  getTask: (taskId: string) => apiRequest<TaskRecord>(`/api/v1/tasks/${taskId}`),
  taskAction: (taskId: string, action: "start" | "pause" | "resume" | "cancel" | "rebuild" | "retry") =>
    apiRequest<TaskRecord>(`/api/v1/tasks/${taskId}/${action}`, {
      method: "POST",
    }),
  getSettings: () => apiRequest<SettingsResponse>("/api/v1/settings"),
  setGithubCredential: (projectId: string, secret: string) =>
    apiRequest<CredentialResult>(`/api/v1/projects/${projectId}/github-credential`, {
      method: "PUT",
      body: JSON.stringify({ secret }),
    }),
  removeGithubCredential: (projectId: string) =>
    apiRequest<ProjectRecord>(`/api/v1/projects/${projectId}/github-credential`, {
      method: "DELETE",
    }),
  setAgentApiKey: (agentId: string, secret: string) =>
    apiRequest<{ agent_id: string; configured: boolean }>(
      `/api/v1/agents/${encodeURIComponent(agentId)}/api-key`,
      { method: "PUT", body: JSON.stringify({ secret }) }
    ),
  removeAgentApiKey: (agentId: string) =>
    apiRequest<{ agent_id: string; configured: boolean }>(
      `/api/v1/agents/${encodeURIComponent(agentId)}/api-key`,
      { method: "DELETE" }
    ),
  updateAgents: (registry: AgentRegistry) =>
    apiRequest<AgentRegistry>("/api/v1/agents", {
      method: "PUT",
      body: JSON.stringify(registry),
    }),
  updateProjectRoles: (projectId: string, agents: Record<string, string>) =>
    apiRequest<ProjectRecord>(`/api/v1/projects/${projectId}/config`, {
      method: "PUT",
      body: JSON.stringify({ agents }),
    }),
};
