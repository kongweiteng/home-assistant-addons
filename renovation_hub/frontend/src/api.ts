import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "@noble/hashes/utils.js";
import type {
  ApiEnvelope,
  ApiErrorShape,
  Area,
  Dashboard,
  LedgerSummary,
  MediaAsset,
  Project,
  QuoteDetail,
  QuoteMediaRole,
  QuoteOffer,
  QuoteRequest,
  SessionState,
  Stage,
  TimelineEvent,
  Transaction,
} from "./types";

let csrfToken = "";

function relative(path: string): string {
  return path.replace(/^\/+/, "");
}

export function assetUrl(path: string | null): string {
  return path ? relative(path) : "";
}

export class ApiError extends Error {
  code: string;
  status: number;

  constructor(message: string, code: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
  }
}

async function parse<T>(response: Response): Promise<T> {
  const document = (await response.json().catch(() => ({}))) as ApiEnvelope<T> & ApiErrorShape;
  if (!response.ok) {
    throw new ApiError(
      document.error?.message || "请求失败，请稍后重试",
      document.error?.code || "request_failed",
      response.status,
    );
  }
  return document.result;
}

async function get<T>(path: string): Promise<T> {
  return parse<T>(await fetch(relative(path), { credentials: "same-origin" }));
}

function idempotencyKey(prefix: string): string {
  const cryptoApi = globalThis.crypto as Crypto | undefined;
  if (typeof cryptoApi?.randomUUID === "function") {
    return `${prefix}-${cryptoApi.randomUUID()}`;
  }
  if (typeof cryptoApi?.getRandomValues !== "function") {
    throw new ApiError("当前浏览器不支持安全随机数，无法安全保存", "secure_random_unavailable", 0);
  }
  const bytes = cryptoApi.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
  const uuid = `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  return `${prefix}-${uuid}`;
}

async function write<T>(method: string, path: string, body: unknown, prefix: string): Promise<T> {
  return parse<T>(
    await fetch(relative(path), {
      method,
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken,
        "Idempotency-Key": idempotencyKey(prefix),
      },
      body: JSON.stringify(body),
    }),
  );
}

export async function loadSession(): Promise<SessionState> {
  const session = await get<SessionState>("api/v1/session");
  csrfToken = session.csrf_token;
  return session;
}

export const api = {
  projects: () => get<{ items: Project[] }>("api/v1/projects"),
  dashboard: (projectId: string) => get<Dashboard>(`api/v1/dashboard?project_id=${encodeURIComponent(projectId)}`),
  stages: (projectId: string) => get<{ items: Stage[] }>(`api/v1/stages?project_id=${encodeURIComponent(projectId)}`),
  areas: (projectId: string) => get<{ items: Area[] }>(`api/v1/areas?project_id=${encodeURIComponent(projectId)}`),
  timeline: (projectId: string) => get<{ items: TimelineEvent[] }>(`api/v1/timeline?project_id=${encodeURIComponent(projectId)}&limit=500`),
  ledger: (projectId: string) => get<{ items: Transaction[]; summary: LedgerSummary }>(`api/v1/ledger/transactions?project_id=${encodeURIComponent(projectId)}&limit=1000`),
  media: (projectId: string) => get<{ items: MediaAsset[] }>(`api/v1/media?project_id=${encodeURIComponent(projectId)}&limit=500`),
  quotes: (projectId: string) => get<{ items: QuoteRequest[] }>(`api/v1/quotes?project_id=${encodeURIComponent(projectId)}&limit=500`),
  quote: (requestId: string) => get<QuoteDetail>(`api/v1/quotes/${encodeURIComponent(requestId)}`),
  createProject: (body: unknown) => write<{ project: Project }>("POST", "api/v1/projects", body, "project"),
  updateProject: (projectId: string, body: unknown) => write<{ project: Project }>("PATCH", `api/v1/projects/${projectId}`, body, "project-update"),
  createStage: (body: unknown) => write<{ stage: Stage }>("POST", "api/v1/stages", body, "stage"),
  updateStage: (stageId: string, body: unknown) => write<{ stage: Stage }>("PATCH", `api/v1/stages/${stageId}`, body, "stage-update"),
  createArea: (body: unknown) => write<{ area: Area }>("POST", "api/v1/areas", body, "area"),
  updateArea: (areaId: string, body: unknown) => write<{ area: Area }>("PATCH", `api/v1/areas/${areaId}`, body, "area-update"),
  createEvent: (body: unknown) => write<{ event: TimelineEvent }>("POST", "api/v1/events", body, "event"),
  updateEvent: (eventId: string, body: unknown) => write<{ event: TimelineEvent }>("PATCH", `api/v1/events/${eventId}`, body, "event-update"),
  addPayment: (body: unknown) => write<{ transaction: Transaction }>("POST", "api/v1/ledger/transactions", body, "payment"),
  correctPayment: (transactionId: string, body: unknown) => write<{ transaction: Transaction }>("PATCH", `api/v1/ledger/transactions/${transactionId}`, body, "payment-update"),
  addRefund: (body: unknown) => write<{ transaction: Transaction }>("POST", "api/v1/ledger/refunds", body, "refund"),
  undoTransaction: (transactionId: string, body: unknown) => write<{ transaction: Transaction }>("POST", `api/v1/ledger/transactions/${transactionId}/undo`, body, "undo"),
  createQuote: (body: unknown) => write<{ quote: QuoteRequest }>("POST", "api/v1/quotes", body, "quote"),
  updateQuote: (requestId: string, body: unknown) => write<{ quote: QuoteRequest }>("PATCH", `api/v1/quotes/${requestId}`, body, "quote-update"),
  addQuoteOffer: (requestId: string, body: unknown) => write<{ offer: QuoteOffer }>("POST", `api/v1/quotes/${requestId}/offers`, body, "quote-offer"),
  updateQuoteOffer: (offerId: string, body: unknown) => write<{ offer: QuoteOffer }>("PATCH", `api/v1/quote-offers/${offerId}`, body, "quote-offer-update"),
  selectQuoteOffer: (requestId: string, body: unknown) => write<{ quote: QuoteRequest; offer: QuoteOffer }>("POST", `api/v1/quotes/${requestId}/select`, body, "quote-select"),
  linkQuoteMedia: (requestId: string, body: { media_id: string; offer_id?: string | null; role: QuoteMediaRole }) => write<{ link: unknown }>("POST", `api/v1/quotes/${requestId}/media`, body, "quote-media"),
};

async function fileSha256(file: File): Promise<string> {
  const hasher = sha256.create();
  const chunkSize = 4 * 1024 * 1024;
  for (let offset = 0; offset < file.size; offset += chunkSize) {
    const chunk = new Uint8Array(await file.slice(offset, offset + chunkSize).arrayBuffer());
    hasher.update(chunk);
  }
  return bytesToHex(hasher.digest());
}

export async function uploadMedia(
  file: File,
  metadata: {
    project_id: string;
    captured_at?: string | null;
    links: Array<{ target_type: "stage" | "area" | "event" | "transaction"; target_id: string }>;
  },
  onProgress: (percent: number) => void,
): Promise<MediaAsset> {
  onProgress(4);
  const digest = await fileSha256(file);
  onProgress(18);
  const upload = await write<{
    completed: boolean;
    result?: { media: MediaAsset };
    upload_id?: string;
    content_url?: string;
    complete_url?: string;
  }>(
    "POST",
    "api/v1/uploads",
    {
      project_id: metadata.project_id,
      original_filename: file.name,
      mime_type: file.type,
      size_bytes: file.size,
      sha256: digest,
      captured_at: metadata.captured_at || null,
      links: metadata.links,
    },
    "upload",
  );
  if (upload.completed && upload.result) {
    onProgress(100);
    return upload.result.media;
  }
  if (!upload.upload_id || !upload.content_url || !upload.complete_url) {
    throw new ApiError("上传会话返回不完整", "upload_session_invalid", 500);
  }
  onProgress(24);
  const contentResponse = await fetch(relative(upload.content_url), {
    method: "PUT",
    credentials: "same-origin",
    headers: {
      "Content-Type": file.type,
      "X-CSRF-Token": csrfToken,
    },
    body: file,
  });
  await parse(contentResponse);
  onProgress(84);
  const completed = await parse<{ media: MediaAsset }>(
    await fetch(relative(upload.complete_url), {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrfToken },
    }),
  );
  onProgress(100);
  return completed.media;
}
