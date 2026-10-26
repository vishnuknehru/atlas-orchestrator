import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SandboxPanel } from "../SandboxPanel";

const notReady = {
  ready: false,
  checks: [
    { id: "storage", label: "Reserved storage", ok: false, detail: "No APFS volume named AtlasSandbox." },
    { id: "data_path", label: "VM data is on the reserved volume", ok: false, detail: "not checked yet" },
  ],
  start_command: "COLIMA_HOME=/Volumes/AtlasSandbox/colima colima start atlas",
  volume_command: "sudo diskutil apfs addVolume <container> APFS AtlasSandbox -reserve 80g -quota 80g",
};

describe("SandboxPanel", () => {
  afterEach(() => vi.restoreAllMocks());

  it("shows failed checks and the exact commands to run", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(JSON.stringify(notReady), { status: 200 }));
    render(<SandboxPanel />);
    expect(await screen.findByText("Not ready")).toBeInTheDocument();
    expect(screen.getByText(/No APFS volume named AtlasSandbox/)).toBeInTheDocument();
    expect(screen.getByText(/sudo diskutil apfs addVolume/)).toBeInTheDocument();
    expect(screen.getByText(/colima start atlas/)).toBeInTheDocument();
  });

  it("runs the self-test and reports ready", async () => {
    const calls: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input.toString();
      calls.push(`${init?.method ?? "GET"} ${url}`);
      if (url.endsWith("/selftest")) {
        return new Response(JSON.stringify({ ready: true, checks: [{ id: "canary_network", label: "No network interface except loopback", ok: true, detail: "interfaces: lo" }] }), { status: 200 });
      }
      return new Response(JSON.stringify(notReady), { status: 200 });
    });
    render(<SandboxPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "Run self-test" }));
    expect(await screen.findByText("Ready")).toBeInTheDocument();
    await waitFor(() => expect(calls).toContain("POST /api/v1/sandbox/selftest"));
    expect(screen.queryByText(/sudo diskutil/)).not.toBeInTheDocument();
  });
});
