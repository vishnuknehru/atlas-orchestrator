import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PlanPanel } from "../PlanPanel";

const plan = {
  author_agent: "claude",
  revision: 2,
  approach: "Add login in two steps.",
  steps: [{ title: "Add model", description: "Persist users", files: [], criteria: ["AC-1"] }],
  risks: ["Password storage"],
  escalated: true,
  history: [
    { kind: "challenge", cycle: 1, revision: 1, challenger: "codex", verdict: "revise", findings: [{ id: "F1", severity: "high", text: "No rate limit" }] },
    { kind: "revision", revision: 2, responses: [{ finding_id: "F1", response: "Added a limiter" }] },
  ],
};

describe("PlanPanel", () => {
  afterEach(() => vi.restoreAllMocks());

  it("shows the plan and challenge history and approves", async () => {
    const posts: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input.toString();
      if (init?.method === "POST") { posts.push(url); return new Response("{}", { status: 200 }); }
      return new Response(JSON.stringify(plan), { status: 200 });
    });
    const onChanged = vi.fn();
    render(<PlanPanel taskId="t1" state="AWAITING_APPROVAL" version={3} onChanged={onChanged} />);
    expect(await screen.findByText("Plan (revision 2)")).toBeInTheDocument();
    expect(screen.getByText(/challenge cap was reached/)).toBeInTheDocument();
    expect(screen.getByText(/F1: No rate limit/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Approve plan" }));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    expect(posts).toEqual(["/api/v1/tasks/t1/plan/approve"]);
  });

  it("requires a comment before requesting changes", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(JSON.stringify(plan), { status: 200 }));
    render(<PlanPanel taskId="t1" state="AWAITING_APPROVAL" version={3} onChanged={() => {}} />);
    const button = await screen.findByRole("button", { name: "Request changes" });
    expect(button).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Requested changes"), { target: { value: "Add rollback" } });
    expect(button).toBeEnabled();
  });

  it("renders nothing before a plan exists", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response("{}", { status: 404 }));
    const { container } = render(<PlanPanel taskId="t1" state="PLAN_CHALLENGE" version={1} onChanged={() => {}} />);
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });
});
