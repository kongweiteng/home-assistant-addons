export type PageKey = "overview" | "timeline" | "ledger" | "media" | "stages" | "settings";

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
  context: TransactionContext | null;
  version: number;
  created_at: string;
  updated_at: string;
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
