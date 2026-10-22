import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { Settings } from "../Settings";

const mockSettings = {
  secret_store_available: true,
  roles: ["architect", "challenger", "developer", "reviewer", "qa"],
  adapters: [
    { name: "claude_cli", kind: "cli", default_provider: "anthropic", allowed_roles: ["architect", "challenger", "developer", "qa", "reviewer"] },
    { name: "codex_cli", kind: "cli", default_provider: "openai", allowed_roles: ["architect", "challenger", "developer", "qa", "reviewer"] },
    { name: "manual_handoff", kind: "manual_handoff", default_provider: null, allowed_roles: ["architect", "challenger", "qa", "reviewer"] },
  ],
  registry: {
    agents: {
      claude: {
        adapter: "claude_cli",
        models: ["claude-sonnet-4-5"],
        billing: "subscription",
        provider: null,
        model_family: null,
        endpoint: null,
        allow_remote_endpoint: false,
        declared_family_required: false,
      },
    },
  },
  agent_api_keys: {},
  projects: [
    {
      id: "proj-1",
      name: "demo-app",
      github_repo: "octocat/demo-app",
      status: "blocked",
      blocked_reason: "github_credential",
      roles: { architect: "claude", challenger: "claude", developer: "claude", reviewer: "claude", qa: "claude" },
      role_evaluation: { ok: true, blocked_reason: null, detail: null, warnings: [] },
      github_credential_configured: false,
    },
  ],
  global_config: { safety: { allow_force_push: false } },
};

type FetchCall = { url: string; method: string; body: unknown };

describe("Settings page", () => {
  let calls: FetchCall[];

  beforeEach(() => {
    vi.restoreAllMocks();
    calls = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input.toString();
      const method = init?.method ?? "GET";
      calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      if (url.endsWith("/api/v1/settings")) {
        return new Response(JSON.stringify(mockSettings), { status: 200 });
      }
      if (url.endsWith("/github-credential") && method === "PUT") {
        return new Response(
          JSON.stringify({ project: {}, verified: true, error_reason: null, role_evaluation: {} }),
          { status: 200 }
        );
      }
      return new Response(JSON.stringify({}), { status: 200 });
    });
  });

  it("sends a GitHub token once and clears the field", async () => {
    render(<Settings />);
    const card = await screen.findByTestId("gh-cred-proj-1");
    expect(within(card).getByText("Not stored")).toBeInTheDocument();

    const input = within(card).getByPlaceholderText("github_pat_…") as HTMLInputElement;
    expect(input.type).toBe("password");
    fireEvent.change(input, { target: { value: "github_pat_example" } });
    fireEvent.click(within(card).getByRole("button", { name: "Save & verify" }));

    await waitFor(() => {
      expect(within(card).getByText("Stored and verified with GitHub.")).toBeInTheDocument();
    });
    expect(calls).toContainEqual({
      url: "/api/v1/projects/proj-1/github-credential",
      method: "PUT",
      body: { secret: "github_pat_example" },
    });
    expect(input.value).toBe("");
  });

  it("adds an agent and saves the registry", async () => {
    render(<Settings />);
    await screen.findByText("Agents");

    fireEvent.change(screen.getByPlaceholderText("e.g. local-reviewer"), { target: { value: "codex" } });
    fireEvent.click(screen.getByRole("button", { name: "Add agent" }));
    fireEvent.click(screen.getByRole("button", { name: "Save agents" }));

    await waitFor(() => {
      expect(calls.some((c) => c.url === "/api/v1/agents" && c.method === "PUT")).toBe(true);
    });
    const put = calls.find((c) => c.url === "/api/v1/agents" && c.method === "PUT");
    const agents = (put?.body as { agents: Record<string, { adapter: string; endpoint: unknown }> }).agents;
    expect(Object.keys(agents)).toEqual(["claude", "codex"]);
    expect(agents.codex.adapter).toBe("claude_cli");
    expect(agents.codex.endpoint).toBeNull();
  });
});
