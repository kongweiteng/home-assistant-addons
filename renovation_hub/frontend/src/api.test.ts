import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";

function response(result: unknown): Response {
  return new Response(JSON.stringify({ version: 1, result }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function idempotencyHeader(fetchMock: ReturnType<typeof vi.fn>): string {
  const init = fetchMock.mock.calls[0]?.[1] as RequestInit | undefined;
  return (init?.headers as Record<string, string> | undefined)?.["Idempotency-Key"] || "";
}

describe("Renovation Hub API idempotency keys", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("uses native randomUUID when the browser exposes it", async () => {
    const randomUUID = vi.fn(() => "123e4567-e89b-42d3-a456-426614174000");
    vi.stubGlobal("crypto", { randomUUID, getRandomValues: vi.fn() });
    const fetchMock = vi.fn().mockResolvedValue(response({ project: {} }));
    vi.stubGlobal("fetch", fetchMock);

    await api.updateProject("project-1", { version: 1, changes: { name: "示例项目" } });

    expect(randomUUID).toHaveBeenCalledOnce();
    expect(idempotencyHeader(fetchMock)).toBe("project-update-123e4567-e89b-42d3-a456-426614174000");
  });

  it("generates a secure UUID v4 when randomUUID is unavailable on HTTP", async () => {
    const getRandomValues = vi.fn((target: Uint8Array) => {
      target.set(Array.from({ length: 16 }, (_, index) => index));
      return target;
    });
    vi.stubGlobal("crypto", { getRandomValues });
    const fetchMock = vi.fn().mockResolvedValue(response({ project: {} }));
    vi.stubGlobal("fetch", fetchMock);

    await api.updateProject("project-1", { version: 1, changes: { name: "示例项目" } });

    expect(getRandomValues).toHaveBeenCalledOnce();
    expect(idempotencyHeader(fetchMock)).toBe("project-update-00010203-0405-4607-8809-0a0b0c0d0e0f");
  });
});
