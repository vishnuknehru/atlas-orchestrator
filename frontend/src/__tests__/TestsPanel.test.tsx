import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TestSettings } from "../TestSettings";
import { TestsPanel } from "../TestsPanel";

const failed = {
  status: "failed", commit_sha: "a".repeat(40), stale: false, source_attested: true, image: "x",
  commands: [
    { command: "pytest -q", status: "passed", exit_code: 0, seconds: 1.2, log: "ok" },
    { command: "mypy .", status: "failed", exit_code: 1, seconds: 3, log: "error: bad type" },
  ],
};

describe("TestsPanel", () => {
  afterEach(() => vi.restoreAllMocks());

  it("shows each command's result and opens the failing log", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(JSON.stringify(failed), { status: 200 }));
    render(<TestsPanel taskId="t1" state="EXECUTING_PHASES" version={5} />);
    expect(await screen.findByText("failed")).toBeInTheDocument();
    expect(screen.getByText("error: bad type")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run tests again" })).toBeEnabled();
  });

  it("warns when the results belong to an older candidate and can start a run", async () => {
    const calls: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input.toString();
      calls.push(`${init?.method ?? "GET"} ${url}`);
      if (init?.method === "POST") return new Response(JSON.stringify({ status: "accepted" }), { status: 202 });
      return new Response(JSON.stringify({ ...failed, stale: true }), { status: 200 });
    });
    render(<TestsPanel taskId="t1" state="EXECUTING_PHASES" version={5} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("older candidate");
    fireEvent.click(screen.getByRole("button", { name: "Run tests again" }));
    await waitFor(() => expect(calls).toContain("POST /api/v1/tasks/t1/tests/run"));
  });

  it("explains a blocked run and renders nothing outside the executing state", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(JSON.stringify({ status: "blocked", commit_sha: "b", blocked_reason: "no_test_config", detail: "Set the test image", commands: [] }), { status: 200 }));
    const { container, rerender } = render(<TestsPanel taskId="t1" state="EXECUTING_PHASES" version={1} />);
    expect(await screen.findByText(/no_test_config: Set the test image/)).toBeInTheDocument();
    rerender(<TestsPanel taskId="t1" state="PLANNING" version={1} />);
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });
});

describe("TestSettings", () => {
  afterEach(() => vi.restoreAllMocks());

  it("saves the image, commands and writable paths", async () => {
    let body: Record<string, unknown> = {};
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input.toString();
      if (init?.method === "PUT") { body = JSON.parse(String(init.body)); return new Response("{}", { status: 200 }); }
      return new Response(JSON.stringify({ image: null, commands: [], writable_paths: [".pytest_cache"], timeout_seconds: 600 }), { status: 200 });
    });
    render(<TestSettings projectId="p1" />);
    await waitFor(() => expect((screen.getByLabelText(/Writable paths/) as HTMLTextAreaElement).value).toBe(".pytest_cache"));
    fireEvent.change(screen.getByLabelText(/Test image/), { target: { value: "python:3.12-slim@sha256:" + "c".repeat(64) } });
    fireEvent.change(screen.getByLabelText(/Test commands/), { target: { value: "pytest -q\nmypy ." } });
    fireEvent.click(screen.getByRole("button", { name: "Save test settings" }));
    expect(await screen.findByText("Test settings saved.")).toBeInTheDocument();
    expect(body).toMatchObject({ test_commands: ["pytest -q", "mypy ."], writable_paths: [".pytest_cache"], test_timeout_seconds: 600 });
  });
});
