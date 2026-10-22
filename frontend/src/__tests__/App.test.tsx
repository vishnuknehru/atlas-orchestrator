import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { App } from "../App";

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
    vi.restoreAllMocks();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.endsWith("/api/v1/overview")) {
        return new Response(JSON.stringify(mockOverview), { status: 200 });
      }
      if (url.endsWith("/api/v1/tasks/task-1")) {
        return new Response(JSON.stringify(mockTaskDetail), { status: 200 });
      }
      return new Response(JSON.stringify({}), { status: 200 });
    });
  });

  it("renders projects, recovery banner, and persisted transitions timeline", async () => {
    render(<App />);

    await waitFor(() => {
      expect(screen.getByText("demo-app")).toBeInTheDocument();
    });

    expect(screen.getByText(/Safe Restart Recovery Summary/i)).toBeInTheDocument();
    expect(screen.getByText("Add Google OAuth login")).toBeInTheDocument();

    fireEvent.click(screen.getByText("Add Google OAuth login"));

    await waitFor(() => {
      expect(screen.getByTestId("transitions-timeline")).toBeInTheDocument();
    });

    expect(screen.getByText("∅ → CREATED")).toBeInTheDocument();
    expect(screen.getByText("CREATED → PAUSED")).toBeInTheDocument();
  });
});
