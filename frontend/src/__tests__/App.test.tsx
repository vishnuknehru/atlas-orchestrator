import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { App } from "../App";
import { atlasApi } from "../api";

const mockOverview = {
  version: "0.1.0",
  recovery: {
    ok: true,
    paused_tasks: ["task-1"],
    paused_phases: [],
    diverged_entities: [],
    completed_at: "2026-10-22T20:00:00Z",
  },
  projects: [
    {
      id: "proj-1",
      name: "demo-app",
      github_repo: "octocat/demo-app",
      visibility: "private",
      base_branch: "main",
      protection_status: "unknown",
      policy_version: 1,
      status: "active",
      blocked_reason: null,
      created_at: "2026-10-22T20:00:00Z",
      updated_at: "2026-10-22T20:00:00Z",
    },
  ],
  tasks: [
    {
      id: "task-1",
      project_id: "proj-1",
      title: "Add Google OAuth login",
      request_text: "Implement OAuth login flow",
      category: "feature",
      priority: 3,
      state: "PAUSED",
      resume_to: "CREATED",
      blocked_reason: null,
      version: 1,
      generation: 1,
      created_at: "2026-10-22T20:00:00Z",
      updated_at: "2026-10-22T20:01:00Z",
    },
  ],
  agents: [
    {
      id: "claude",
      adapter: "claude_cli",
      billing: "subscription",
      models: ["claude-sonnet-4-5"],
      provider: "anthropic",
      model_family: "anthropic",
      allow_remote_endpoint: false,
      capacity_state: "AVAILABLE",
      auth_status: "configured",
      current_revision: 1,
    },
  ],
};

const mockTaskDetail = {
  ...mockOverview.tasks[0],
  acceptance_criteria: [{ id: "AC-1", text: "OAuth works", mandatory: true }],
  phases: [],
  timeline: [
    {
      id: "tr-1",
      project_id: "proj-1",
      entity_type: "task",
      entity_id: "task-1",
      from_state: null,
      to_state: "CREATED",
      reason: "Task created by user",
      actor: "user",
      candidate_id: null,
      policy_version: 1,
      underlying: 0,
      evidence_refs_json: "[]",
      correlation_id: "corr-1",
      at: "2026-10-22T20:00:00Z",
    },
    {
      id: "tr-2",
      project_id: "proj-1",
      entity_type: "task",
      entity_id: "task-1",
      from_state: "CREATED",
      to_state: "PAUSED",
      reason: "Minimal safe restart: paused on startup (auto_resume=false)",
      actor: "recovery",
      candidate_id: null,
      policy_version: 1,
      underlying: 0,
      evidence_refs_json: "[]",
      correlation_id: "corr-2",
      at: "2026-10-22T20:01:00Z",
    },
  ],
};

describe("ATLAS Dashboard v1", () => {
  beforeEach(() => {
    window.localStorage.clear();
    delete document.documentElement.dataset.theme;
    window.location.hash = "#/";
    vi.restoreAllMocks();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.endsWith("/api/v1/overview")) {
        return new Response(JSON.stringify(mockOverview), { status: 200 });
      }
      if (url.endsWith("/api/v1/tasks/task-1/history")) {
        return new Response(JSON.stringify({ task: mockTaskDetail, workflow_thread_id: "task:task-1", executions: [], workflow_steps: [], approvals: [], artifacts: [], remote_operations: [], exported_at: "2026-10-22T20:02:00Z" }), { status: 200 });
      }
      if (url.endsWith("/api/v1/tasks/task-1")) {
        return new Response(JSON.stringify(mockTaskDetail), { status: 200 });
      }
      return new Response(JSON.stringify({}), { status: 200 });
    });
  });

  it("plans a project goal and shows each phase as a task", async () => {
    window.location.hash = "#/projects/proj-1";
    const phaseTasks = [1, 2].map(seq => ({
      ...mockOverview.tasks[0], id: `phase-${seq}`, title: `Step ${seq}`, state: "CREATED", roadmap_id: "rm-1", roadmap_seq: seq,
    }));
    let planned = false;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.endsWith("/api/v1/projects/proj-1/roadmap")) {
        expect(JSON.parse(String(init?.body))).toEqual({ goal: "Build a billing dashboard" });
        planned = true;
        return new Response(JSON.stringify({ id: "rm-1", project_id: "proj-1", goal: "g", summary: "s", author_agent: "claude", created_at: "", tasks: phaseTasks }), { status: 201 });
      }
      if (url.endsWith("/api/v1/overview")) {
        return new Response(JSON.stringify({ ...mockOverview, tasks: planned ? [...phaseTasks].reverse() : [] }), { status: 200 });
      }
      return new Response(JSON.stringify({}), { status: 200 });
    });
    render(<App />);
    fireEvent.change(await screen.findByLabelText("Project goal"), { target: { value: "Build a billing dashboard" } });
    fireEvent.click(screen.getByRole("button", { name: "Create plan" }));
    expect(await screen.findByText(/2 phases were added as tasks/)).toBeInTheDocument();
    const titles = (await screen.findAllByText(/^Phase \d: Step/)).map(el => el.textContent);
    expect(titles).toEqual(["Phase 1: Step 1", "Phase 2: Step 2"]);
  });

  it("renders projects, recovery banner, and persisted transitions timeline", async () => {
    render(<App />);

    await waitFor(() => {
      expect(screen.getByText("demo-app")).toBeInTheDocument();
    });

    expect(screen.getByText(/Work paused after restart/i)).toBeInTheDocument();
    expect(screen.queryByText("Add Google OAuth login")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /demo-app/ }));
    await screen.findByText("Add Google OAuth login");
    expect(window.location.hash).toBe("#/projects/proj-1");
    expect(screen.queryByText("Register Project")).not.toBeInTheDocument();

    fireEvent.click(screen.getByText("Add Google OAuth login"));

    await waitFor(() => {
      expect(screen.getByTestId("transitions-timeline")).toBeInTheDocument();
    });

    await screen.findByText("task:task-1");
    expect(screen.getByRole("button", { name: "Download history (JSON)" })).toBeEnabled();
    expect(screen.getByText("∅ → CREATED")).toBeInTheDocument();
    expect(screen.getByText("CREATED → PAUSED")).toBeInTheDocument();
    expect(window.location.hash).toBe("#/projects/proj-1/tasks/task-1");
    expect(screen.queryByText("Create Task")).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("← Back to demo-app tasks"));
    await screen.findByText("Create Task");
    expect(screen.queryByTestId("transitions-timeline")).not.toBeInTheDocument();
  });
  it("opens a task directly from its URL within its project", async () => {
    window.location.hash = "#/projects/proj-1/tasks/task-1";
    render(<App />);
    await screen.findByTestId("transitions-timeline");
    expect(screen.getByRole("navigation", { name: "Breadcrumb" })).toHaveTextContent("demo-app");
    expect(screen.queryByText("Register Project")).not.toBeInTheDocument();
    expect(screen.queryByText("Create Task")).not.toBeInTheDocument();
  });
  it("defaults to light and remembers the selected theme across remounts", async () => {
    const { unmount } = render(<App />);
    const light = screen.getByRole("button", { name: "Light" });
    expect(light).toHaveAttribute("aria-pressed", "true");
    expect(document.documentElement).toHaveAttribute("data-theme", "light");

    fireEvent.click(screen.getByRole("button", { name: "Dark" }));
    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
    expect(window.localStorage.getItem("atlas-theme")).toBe("dark");
    unmount();
    render(<App />);
    expect(screen.getByRole("button", { name: "Dark" })).toHaveAttribute("aria-pressed", "true");

    fireEvent.click(screen.getByRole("button", { name: "Light" }));
    expect(document.documentElement).toHaveAttribute("data-theme", "light");
    await screen.findByText("demo-app");
  });

  it("returns to the project list after confirmed project removal", async () => {
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: /demo-app/ }));
    fireEvent.click(await screen.findByText("Project settings"));
    fireEvent.click(screen.getByRole("button", { name: "Remove project" }));
    const remove = vi.spyOn(atlasApi, "removeProject").mockResolvedValue({ ...mockOverview.projects[0], visibility: "private", status: "archived" });
    vi.spyOn(atlasApi, "getOverview").mockResolvedValue({ ...mockOverview, projects: [], tasks: [] });
    fireEvent.click(screen.getByRole("button", { name: "Confirm removal" }));
    await waitFor(() => expect(window.location.hash).toBe("#/"));
    expect(await screen.findByText("No repositories registered yet.")).toBeInTheDocument();
    expect(remove).toHaveBeenCalledWith("proj-1");
    expect(screen.queryByText(/project has been removed or is unavailable/)).not.toBeInTheDocument();
  });

});
