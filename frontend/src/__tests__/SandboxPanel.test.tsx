import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SandboxPanel } from "../SandboxPanel";

const notReady = {
  ready: false,
  checks: [
    { id: "vm_running", label: "Colima VM is running", ok: false, detail: "status: Stopped" },
    { id: "daemon", label: "Docker daemon answers on the Colima socket", ok: false, detail: "not checked yet" },
    { id: "host_mounts", label: "The VM shares no folders from your computer", ok: false, detail: "Shared into the VM: /Users/me", level: "warning" },
  ],
  start_command: "colima start --activate=false",
};

describe("SandboxPanel", () => {
  afterEach(() => vi.restoreAllMocks());

  it("shows failed checks and the exact commands to run", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(JSON.stringify(notReady), { status: 200 }));
    render(<SandboxPanel />);
    expect(await screen.findByText("Not ready")).toBeInTheDocument();
    expect(screen.getByText(/status: Stopped/)).toBeInTheDocument();
    expect(screen.getByText("colima start --activate=false")).toBeInTheDocument();
    expect(screen.getByText("warning:")).toBeInTheDocument();
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
    expect(screen.queryByText("colima start --activate=false")).not.toBeInTheDocument();
  });
});
