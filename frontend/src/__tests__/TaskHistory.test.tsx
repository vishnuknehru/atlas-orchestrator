import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TaskHistory } from "../TaskHistory";
import { atlasApi, TaskHistoryResponse } from "../api";

const saved = {
  schema_version: 1, exported_at: "2026-10-22T20:00:00Z",
  task: { id: "task-1", created_at: "2026-10-22T19:00:00Z" },
  workflow_thread_id: "task:task-1:g1",
  executions: [{ id: "run-1", agent_id: "reviewer", kind: "agent", role: "reviewer", model: "model-1", status: "failed", started_at: "2026-10-22T20:00:00Z", finished_at: null, exit_code: 1, error_detail: "Tests failed", correlation_id: "trace-1" }],
  workflow_steps: [], approvals: [], artifacts: [], remote_operations: [],
} as unknown as TaskHistoryResponse;

describe("Saved task history", () => {
  afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

  it("loads saved run references and exports the same investigation records", async () => {
    vi.spyOn(atlasApi, "getTaskHistory").mockResolvedValue(saved);
    const create = vi.fn().mockReturnValue("blob:history");
    const originalURL = globalThis.URL;
    class DownloadURL extends originalURL {
      static createObjectURL = create;
      static revokeObjectURL = vi.fn();
    }
    vi.stubGlobal("URL", DownloadURL);
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    render(<TaskHistory taskId="task-1" version={1} />);
    await screen.findByText("task:task-1:g1");
    fireEvent.click(screen.getByText("Runs (1)"));
    expect(screen.getByText("Tests failed")).toBeInTheDocument();
    expect(screen.getByText(/Trace trace-1/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Download history (JSON)" }));
    expect(create.mock.calls[0][0]).toBeInstanceOf(Blob);
    expect(click).toHaveBeenCalledTimes(1);
    expect(click.mock.instances[0]).toHaveAttribute("download", "atlas-task-task-1-history.json");
  });

  it("offers retry on failure and reloads when the task version changes", async () => {
    const load = vi.spyOn(atlasApi, "getTaskHistory").mockRejectedValueOnce(new Error("Connection lost")).mockResolvedValue(saved);
    const { rerender } = render(<TaskHistory taskId="task-1" version={1} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Connection lost");
    expect(screen.getByRole("button", { name: "Download history (JSON)" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Retry history" }));
    await screen.findByText("task:task-1:g1");
    rerender(<TaskHistory taskId="task-1" version={2} />);
    await waitFor(() => expect(load).toHaveBeenCalledTimes(3));
    await screen.findByText("task:task-1:g1");
  });
});
