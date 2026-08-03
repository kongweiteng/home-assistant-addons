import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";

const project = {
  id: "project-1",
  name: "示例新家",
  timezone: "Asia/Shanghai",
  budget_cents: 26000000,
  status: "active",
  version: 1,
  created_at: "2026-08-03T00:00:00Z",
  updated_at: "2026-08-03T00:00:00Z",
};

const stage = {
  id: "stage-1",
  project_id: project.id,
  name: "水电施工",
  position: 1,
  status: "active",
  color: "#5f8f55",
  planned_start: "2026-08-01",
  planned_end: "2026-08-20",
  actual_start: "2026-08-01",
  actual_end: null,
  version: 1,
  created_at: "2026-08-03T00:00:00Z",
  updated_at: "2026-08-03T00:00:00Z",
};

function response(result: unknown): Response {
  return new Response(JSON.stringify({ version: 1, result }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function installFetch(writable: boolean) {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("api/v1/session")) return response({ csrf_token: "csrf-token", writer_mode: writable ? "primary_writer" : "read_only", writable, portable_export_state: "current" });
    if (url === "api/v1/projects") return response({ items: [project] });
    if (url.includes("api/v1/dashboard")) return response({ project, active_stage: stage, counts: { stages: 1, areas: 0, events: 0 }, ledger: { currency: "CNY", net_amount_cents: 0, net_amount: "0.00", category_totals: {}, tag_totals: {}, transaction_count: 0 }, budget_remaining_cents: project.budget_cents, budget_used_ratio: 0, recent_events: [] });
    if (url.includes("api/v1/stages")) return response({ items: [stage] });
    if (url.includes("api/v1/areas")) return response({ items: [] });
    if (url.includes("api/v1/timeline")) return response({ items: [] });
    if (url.includes("api/v1/ledger")) return response({ items: [], summary: { currency: "CNY", net_amount_cents: 0, net_amount: "0.00", category_totals: {}, tag_totals: {}, transaction_count: 0 } });
    if (url.includes("api/v1/media")) return response({ items: [] });
    throw new Error(`Unexpected request: ${url}`);
  }));
}

describe("Renovation Hub app", () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("loads the cockpit and navigates to the media archive", async () => {
    installFetch(false);
    const user = userEvent.setup();
    render(<App />);
    expect(await screen.findByText("总支出")).toBeInTheDocument();
    await user.click(screen.getAllByRole("button", { name: "图片视频" })[0]);
    expect(screen.getByRole("heading", { name: "图片视频" })).toBeInTheDocument();
    expect(screen.getByText("还没有影像档案")).toBeInTheDocument();
  });

  it("opens the payment editor when the writer is active", async () => {
    installFetch(true);
    const user = userEvent.setup();
    render(<App />);
    const add = (await screen.findAllByRole("button", { name: "新增记录" })).find((button) => !button.hasAttribute("disabled"));
    expect(add).toBeDefined();
    await user.click(add!);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "新增账目" })).toBeInTheDocument();
  });
});
