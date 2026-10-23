import { afterEach, describe, expect, it, vi } from "vitest";
import { atlasApi } from "../api";

describe("API error feedback", () => {
  afterEach(() => vi.restoreAllMocks());

  it("explains an older server's missing project deletion route", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ detail: "Method Not Allowed" }), { status: 405 }));
    await expect(atlasApi.removeProject("project-1")).rejects.toThrow("Restart Atlas to load the latest changes");
  });

  it("preserves actionable project removal conflicts", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ detail: "Finish or cancel this project's tasks before removing it." }), { status: 409 }));
    await expect(atlasApi.removeProject("project-1")).rejects.toThrow("Finish or cancel this project's tasks");
  });
});
