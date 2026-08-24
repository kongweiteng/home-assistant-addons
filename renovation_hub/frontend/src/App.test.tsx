import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import type { Transaction } from "./types";

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

const ledgerItems: Transaction[] = [
  {
    id: "payment-v2",
    type: "payment",
    amount_cents: 128000,
    amount: "1280.00",
    occurred_on: "2026-08-03",
    main_category: "",
    merchant: "TATA 木门",
    note: "购买客厅和卧室木门、门套及五金安装服务",
    is_deposit: false,
    original_payment_id: null,
    status: "active",
    tags: ["主题:门窗", "专业:木作"],
    grouped_tags: { 主题: ["门窗"], 专业: ["木作"] },
    ledger_format_version: 2,
    context: { project_id: project.id, stage_id: stage.id, area_id: null, version: 1, updated_at: "2026-08-03T00:00:00Z" },
    version: 1,
    created_at: "2026-08-03T00:00:00Z",
    updated_at: "2026-08-03T00:00:00Z",
  },
  {
    id: "deposit-v2",
    type: "payment",
    amount_cents: 50000,
    amount: "500.00",
    occurred_on: "2026-08-04",
    main_category: "",
    merchant: "示例施工队",
    note: "水电进场订金",
    is_deposit: true,
    original_payment_id: null,
    status: "active",
    tags: ["专业:水电"],
    grouped_tags: { 专业: ["水电"] },
    ledger_format_version: 2,
    context: { project_id: project.id, stage_id: stage.id, area_id: null, version: 1, updated_at: "2026-08-04T00:00:00Z" },
    version: 1,
    created_at: "2026-08-04T00:00:00Z",
    updated_at: "2026-08-04T00:00:00Z",
  },
  {
    id: "refund-v2",
    type: "refund",
    amount_cents: 20000,
    amount: "200.00",
    occurred_on: "2026-08-05",
    main_category: "",
    merchant: "",
    note: "木门差价退回",
    is_deposit: false,
    original_payment_id: "payment-v2",
    status: "active",
    tags: ["主题:门窗", "专业:木作"],
    grouped_tags: { 主题: ["门窗"], 专业: ["木作"] },
    ledger_format_version: 2,
    context: { project_id: project.id, stage_id: stage.id, area_id: null, version: 1, updated_at: "2026-08-05T00:00:00Z" },
    version: 1,
    created_at: "2026-08-05T00:00:00Z",
    updated_at: "2026-08-05T00:00:00Z",
  },
  {
    id: "payment-v1",
    type: "payment",
    amount_cents: 7900,
    amount: "79.00",
    occurred_on: "2026-08-06",
    main_category: "灯具",
    merchant: "示例灯具店",
    note: "人体感应小夜灯",
    is_deposit: false,
    original_payment_id: null,
    status: "active",
    tags: ["照明"],
    ledger_format_version: 1,
    context: { project_id: project.id, stage_id: null, area_id: null, version: 1, updated_at: "2026-08-06T00:00:00Z" },
    version: 1,
    created_at: "2026-08-06T00:00:00Z",
    updated_at: "2026-08-06T00:00:00Z",
  },
];

const quote = {
  id: "quote-1",
  project_id: project.id,
  title: "厨房墙砖",
  category: "主材",
  description: "哑光暖灰色",
  specification: { 尺寸: "600×1200mm", 表面: "柔光" },
  quantity_milli: 42000,
  unit: "片",
  status: "quoted" as const,
  follow_up_at: "2026-08-24T01:30:00Z",
  selected_offer_id: null,
  source_ref: "",
  note: "",
  version: 3,
  created_at: "2026-08-23T00:00:00Z",
  updated_at: "2026-08-23T01:00:00Z",
  offer_count: 2,
  best_total_cents: 118000,
  supplier_names: ["示例建材一店", "示例建材二店"],
  cover_media: { id: "quote-media-1", original_filename: "报价单.jpg", preview_url: "/api/v1/media/quote-media-1/preview" },
};

const quoteDetail = {
  quote,
  offers: [
    {
      id: "offer-1", request_id: quote.id, supplier_name: "示例建材一店", contact_name: "王经理", contact_phone: "13800000000", supplier_address: "示例路 88 号",
      quoted_at: null, valid_until: "2026-09-30", currency: "CNY" as const, subtotal_cents: 120000, tax_cents: 0, shipping_cents: 10000,
      installation_cents: 0, discount_cents: 0, total_cents: 130000, quantity_milli: 42000, unit: "片", unit_price_cents: 3095,
      price_includes_tax: true, lead_time_days: 3, brand: "示例陶瓷", model: "WG-612", specification: { 尺寸: "600×1200mm" },
      payment_terms: "到货付款", warranty: "一年", note: "", status: "quoted" as const, effective_status: "quoted" as const,
      extraction_confidence: 88, source_ref: "", version: 1, created_at: "2026-08-23T00:00:00Z", updated_at: "2026-08-23T00:00:00Z",
    },
    {
      id: "offer-2", request_id: quote.id, supplier_name: "示例建材二店", contact_name: "李店长", contact_phone: "13900000000", supplier_address: "样板市场 12 号",
      quoted_at: null, valid_until: "2026-09-20", currency: "CNY" as const, subtotal_cents: 118000, tax_cents: 0, shipping_cents: 0,
      installation_cents: 0, discount_cents: 0, total_cents: 118000, quantity_milli: 42000, unit: "片", unit_price_cents: 2810,
      price_includes_tax: false, lead_time_days: 5, brand: "样板砖", model: "A612", specification: { 尺寸: "600×1200mm" },
      payment_terms: "订金 30%", warranty: "", note: "", status: "quoted" as const, effective_status: "quoted" as const,
      extraction_confidence: null, source_ref: "", version: 1, created_at: "2026-08-23T00:10:00Z", updated_at: "2026-08-23T00:10:00Z",
    },
  ],
  media: [
    {
      id: "quote-media-1", project_id: project.id, media_type: "image" as const, mime_type: "image/jpeg", original_filename: "报价单.jpg", size_bytes: 1024,
      sha256: "a".repeat(64), width: 1200, height: 900, duration_ms: null, captured_at: "2026-08-23T00:00:00Z", uploaded_at: "2026-08-23T00:00:00Z",
      source: "fixture", processing_status: "ready" as const, error_code: null, version: 1, content_url: "/api/v1/media/quote-media-1/content",
      preview_url: "/api/v1/media/quote-media-1/preview", offer_id: "offer-2", role: "quote_sheet" as const,
    },
  ],
};

function installFetch(writable: boolean, transactions: Transaction[] = []) {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("api/v1/session")) return response({ csrf_token: "csrf-token", writer_mode: writable ? "primary_writer" : "read_only", writable, portable_export_state: "current" });
    if (url === "api/v1/projects") return response({ items: [project] });
    if (url.includes("api/v1/dashboard")) return response({ project, active_stage: stage, counts: { stages: 1, areas: 0, events: 0 }, ledger: { currency: "CNY", net_amount_cents: 0, net_amount: "0.00", category_totals: {}, tag_totals: {}, transaction_count: 0 }, budget_remaining_cents: project.budget_cents, budget_used_ratio: 0, recent_events: [] });
    if (url.includes("api/v1/stages")) return response({ items: [stage] });
    if (url.includes("api/v1/areas")) return response({ items: [] });
    if (url.includes("api/v1/timeline")) return response({ items: [] });
    if (url.includes("api/v1/ledger/catalog")) return response({ items: [{ code: "water", kind: "category", parent_code: null, label: "水电", active: true, position: 1 }, { code: "paint", kind: "subcategory", parent_code: "water", label: "油漆", active: true, position: 1 }, { code: "material", kind: "expense_type", parent_code: null, label: "材料", active: true, position: 1 }] });
    if (url.includes("api/v1/payment-plans")) return response({ items: [] });
    if (url.includes("api/v1/ledger")) return response({ items: transactions, summary: { currency: "CNY", net_amount_cents: 1416900, net_amount: "14169.00", category_totals: {}, tag_totals: {}, transaction_count: transactions.length } });
    if (url.includes("api/v1/quotes/quote-1")) return response(quoteDetail);
    if (url.includes("api/v1/quotes")) return response({ items: [quote] });
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

  it("shows category, transaction type, purpose and merchant separately", async () => {
    installFetch(false, ledgerItems);
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("总支出");
    await user.click(screen.getAllByRole("button", { name: "资金账目" })[0]);

    expect(screen.getAllByText("门窗")).toHaveLength(2);
    expect(screen.getByText("水电")).toBeInTheDocument();
    expect(screen.getByText("灯具")).toBeInTheDocument();
    expect(screen.getAllByText("付款")).toHaveLength(2);
    expect(screen.getByText("订金")).toBeInTheDocument();
    expect(screen.getByText("退款")).toBeInTheDocument();
    expect(screen.getByText("购买客厅和卧室木门、门套及五金安装服务")).toHaveAttribute("title", "购买客厅和卧室木门、门套及五金安装服务");
    expect(screen.getByText("商家：TATA 木门")).toBeInTheDocument();
  });

  it("shows multi-supplier quotes and opens the original image viewer", async () => {
    installFetch(false);
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("总支出");
    await user.click(screen.getAllByRole("button", { name: "询价报价" })[0]);

    expect(screen.getByRole("heading", { name: "询价报价" })).toBeInTheDocument();
    expect(await screen.findByText("示例建材一店")).toBeInTheDocument();
    expect(screen.getByText("示例建材二店")).toBeInTheDocument();
    expect(screen.getAllByText("当前最低")).toHaveLength(2);
    await user.click(screen.getByRole("button", { name: /报价单\.jpg/ }));
    expect(screen.getByRole("dialog", { name: "报价原图查看器" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "下载原图" })).toHaveAttribute("download", "报价单.jpg");
  });
});
