import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ArchivedProjects, ProjectControls } from "../ProjectControls";
import { atlasApi, ProjectRecord } from "../api";

const project = { id: "project-1", name: "Demo", github_repo: "owner/demo", base_branch: "main" } as ProjectRecord;

describe("Project management", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("saves edits and retains the form when the server rejects a change", async () => {
    const onChanged = vi.fn().mockResolvedValue(undefined);
    const save = vi.spyOn(atlasApi, "updateProject").mockRejectedValueOnce(new Error("Name already exists")).mockResolvedValueOnce(project);
    render(<ProjectControls project={project} onChanged={onChanged} />);
    fireEvent.click(screen.getByRole("button", { name: "Edit project" }));
    fireEvent.change(screen.getByLabelText("Project name"), { target: { value: "Updated" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Name already exists");
    expect(screen.getByLabelText("Project name")).toHaveValue("Updated");
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await screen.findByRole("status");
    expect(save).toHaveBeenLastCalledWith("project-1", { name: "Updated", base_branch: "main" });
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it("requires confirmation before removal and permits cancellation", async () => {
    const remove = vi.spyOn(atlasApi, "removeProject").mockResolvedValue(project);
    const onChanged = vi.fn().mockResolvedValue(undefined);
    render(<ProjectControls project={project} onChanged={onChanged} />);
    fireEvent.click(screen.getByRole("button", { name: "Remove project" }));
    expect(remove).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(remove).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Remove project" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm removal" }));
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    expect(remove).toHaveBeenCalledWith("project-1");
  });

  it("shows removal failures without hiding the project", async () => {
    vi.spyOn(atlasApi, "removeProject").mockRejectedValue(new Error("Cancel unfinished tasks first"));
    const onChanged = vi.fn();
    render(<ProjectControls project={project} onChanged={onChanged} />);
    fireEvent.click(screen.getByRole("button", { name: "Remove project" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm removal" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Cancel unfinished tasks first");
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("restores archived projects", async () => {
    vi.spyOn(atlasApi, "getArchivedProjects").mockResolvedValue([project]);
    const restore = vi.spyOn(atlasApi, "restoreProject").mockResolvedValue(project);
    render(<ArchivedProjects />);
    fireEvent.click(screen.getByRole("button", { name: "Show archived projects" }));
    fireEvent.click(await screen.findByRole("button", { name: "Restore Demo" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Demo restored");
    expect(restore).toHaveBeenCalledWith("project-1");
    expect(screen.getByText("No archived projects.")).toBeInTheDocument();
  });
});
