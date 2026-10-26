import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CandidatePanel } from "../CandidatePanel";

const candidate = {
  author_agent: "claude", plan_revision: 2, base_sha: "a".repeat(40), commit_sha: "b".repeat(40),
  worktree: "/w/t1", changed_files: ["app/login.py", ".github/workflows/ci.yml"], guard_status: "held",
  violations: [{ path: ".github/workflows/ci.yml", kind: "sensitive_path" }], summary: "Added login.",
};

describe("CandidatePanel", () => {
  afterEach(() => vi.restoreAllMocks());

  it("lists changed files and flags held paths", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(JSON.stringify(candidate), { status: 200 }));
    render(<CandidatePanel taskId="t1" state="EXECUTING_PHASES" version={4} />);
    expect(await screen.findByText("Candidate change")).toBeInTheDocument();
    expect(screen.getByText("app/login.py")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Held for your review");
    expect(screen.getByText("Added login.")).toBeInTheDocument();
  });

  it("stays hidden before a candidate exists", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response("{}", { status: 404 }));
    const { container } = render(<CandidatePanel taskId="t1" state="BLOCKED" version={1} />);
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });
});
