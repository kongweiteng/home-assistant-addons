export type PageKey = "overview" | "timeline" | "ledger" | "quotes" | "media" | "stages" | "settings";

export interface SessionState {
  csrf_token: string;
  writer_mode: string;
  writable: boolean;
  portable_export_state: string;
}

export interface Project {
  id: string;
  name: string;
  timezone: string;
  budget_cents: number;
  status: "active" | "completed" | "archived";
  version: number;
  created_at: string;
  updated_at: string;
}

export interface Stage {
  id: string;
  project_id: string;
  name: string;
  position: number;
  status: "planned" | "active" | "completed" | "archived";
  color: string;
  planned_start: string | null;
  planned_end: string | null;
  actual_start: string | null;
  actual_end: string | null;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface Area {
  id: string;
  project_id: string;
  name: string;
  position: number;
  status: "active" | "archived";
  version: number;
  created_at: string;
  updated_at: string;
}

export interface TimelineEvent {
  id: string;
  project_id: string;
  stage_id: string | null;
  stage_name?: string | null;
  area_id: string | null;
  area_name?: string | null;
  event_type: "progress" | "note" | "decision" | "inspection" | "milestone";
  title: string;
  description: string;
  occurred_at: string;
  status: "active" | "voided";
  version: number;
  created_at: string;
  updated_at: string;
}

export interface TransactionContext {
  project_id: string;
  stage_id: string | null;
  area_id: string | null;
  version: number;
  updated_at: string;
}

export interface Transaction {
  id: string;
  type: "payment" | "refund";
  amount_cents: number;
  amount: string;
  occurred_on: string;
  main_category: string;
  merchant: string;
  note: string;
  is_deposit: boolean;
  original_payment_id: string | null;
  status: "active" | "voided";
  tags: string[];
  grouped_tags?: Record<string, string[]>;
  category?: string;
  subcategory?: string;
  expense_type?: string;
  ledger_format_version?: number;
  context: TransactionContext | null;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface ClassificationCatalogItem {
  code: string;
  kind: "category" | "subcategory" | "expense_type";
  parent_code: string | null;
  label: string;
  active: boolean;
  position: number;
}

export interface PaymentPlanNode {
  id: string;
  name: string;
  amount_cents: number;
  due_on: string | null;
  paid_amount_cents: number;
  remaining_amount_cents: number;
  payment_status: "pending" | "partial" | "paid";
}

export interface PaymentPlan {
  id: string;
  project_id: string;
  name: string;
  total_amount_cents: number;
  paid_amount_cents: number;
  remaining_amount_cents: number;
  payment_status: "pending" | "partial" | "paid";
  payment_nodes: PaymentPlanNode[];
}

export interface LedgerSummary {
  currency: "CNY";
  net_amount_cents: number;
  net_amount: string;
  category_totals: Record<string, number>;
  tag_totals: Record<string, number>;
  transaction_count: number;
}

export interface MediaLink {
  target_type: "event" | "transaction" | "stage" | "area";
  target_id: string;
}

export interface MediaAsset {
  id: string;
  project_id: string;
  media_type: "image" | "video";
  mime_type: string;
  original_filename: string;
  size_bytes: number;
  sha256: string;
  width: number | null;
  height: number | null;
  duration_ms: number | null;
  captured_at: string | null;
  uploaded_at: string;
  source: string;
  processing_status: "uploaded" | "validating" | "ready" | "failed" | "quarantined";
  error_code: string | null;
  version: number;
  links: MediaLink[];
  content_url: string;
  preview_url: string | null;
}

export type QuoteRequestStatus = "inquiry" | "quoted" | "review_required" | "selected" | "purchased" | "closed" | "archived";
export type QuoteOfferStatus = "quoted" | "review_required" | "selected" | "rejected" | "expired" | "purchased";
export type QuoteMediaRole = "source" | "product" | "quote_sheet" | "business_card" | "address" | "other";

export interface QuoteCoverMedia {
  id: string;
  original_filename: string;
  preview_url: string;
}

export interface QuoteRequest {
  id: string;
  project_id: string;
  title: string;
  category: string;
  description: string;
  specification: Record<string, string>;
  quantity_milli: number | null;
  unit: string;
  status: QuoteRequestStatus;
  follow_up_at: string | null;
  selected_offer_id: string | null;
  source_ref: string;
  note: string;
  version: number;
  created_at: string;
  updated_at: string;
  offer_count: number;
  best_total_cents: number | null;
  supplier_names: string[];
  cover_media: QuoteCoverMedia | null;
}

export interface QuoteOffer {
  id: string;
  request_id: string;
  supplier_name: string;
  contact_name: string;
  contact_phone: string;
  supplier_address: string;
  quoted_at: string | null;
  valid_until: string | null;
  currency: "CNY";
  subtotal_cents: number | null;
  tax_cents: number;
  shipping_cents: number;
  installation_cents: number;
  discount_cents: number;
  total_cents: number | null;
  quantity_milli: number | null;
  unit: string;
  unit_price_cents: number | null;
  price_includes_tax: boolean;
  lead_time_days: number | null;
  brand: string;
  model: string;
  specification: Record<string, string>;
  payment_terms: string;
  warranty: string;
  note: string;
  status: QuoteOfferStatus;
  effective_status: QuoteOfferStatus;
  extraction_confidence: number | null;
  source_ref: string;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface QuoteMediaAsset extends Omit<MediaAsset, "links"> {
  offer_id: string | null;
  role: QuoteMediaRole;
}

export interface QuoteDetail {
  quote: QuoteRequest;
  offers: QuoteOffer[];
  media: QuoteMediaAsset[];
}

export interface Dashboard {
  project: Project;
  active_stage: Stage | null;
  counts: {
    stages: number;
    areas: number;
    events: number;
  };
  ledger: LedgerSummary;
  budget_remaining_cents: number;
  budget_used_ratio: number | null;
  recent_events: TimelineEvent[];
}

export interface HubData {
  dashboard: Dashboard | null;
  stages: Stage[];
  areas: Area[];
  timeline: TimelineEvent[];
  transactions: Transaction[];
  summary: LedgerSummary | null;
  media: MediaAsset[];
  catalog: ClassificationCatalogItem[];
  paymentPlans: PaymentPlan[];
  quotes: QuoteRequest[];
}

export interface ApiEnvelope<T> {
  version: number;
  result: T;
}

export interface ApiErrorShape {
  error?: {
    code?: string;
    message?: string;
  };
}
