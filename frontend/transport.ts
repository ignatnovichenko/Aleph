export type WriteMethod = "POST" | "PATCH" | "DELETE";
export interface RequestOptions { requestKey?: string | null; showApiMessage?: boolean; timeoutMs?: number }
export class ApiMessage extends Error {}

export const statePath = "/api/state?compact=1";

export async function apiRequest(path: string, signed: string, method: "GET" | WriteMethod = "GET",
  body?: object, { requestKey, showApiMessage, timeoutMs = 18000 }: RequestOptions = {}): Promise<unknown> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  const file = body instanceof File ? body : undefined;
  try {
    const response = await fetch(path, {
      method,
      signal: controller.signal,
      credentials: "omit",
      cache: "no-store",
      headers: {
        Authorization: "tma " + signed,
        ...(requestKey ? {"Idempotency-Key": requestKey} : {}),
        ...(file ? {"Content-Type": "application/octet-stream", "X-Source-Name": encodeURIComponent(file.name)}
          : body ? {"Content-Type": "application/json"} : {}),
      },
      ...(body ? {body: file ?? JSON.stringify(body)} : {}),
    });
    if (!response.ok) {
      if (showApiMessage && [400, 404].includes(response.status) &&
          response.headers.get("Content-Type")?.includes("application/json")) {
        const data = await response.json().catch(() => null);
        if (typeof data?.error === "string" && data.error.length <= 320) throw new ApiMessage(data.error);
      }
      throw new Error(String(response.status));
    }
    if (path.endsWith(".ics")) return await response.blob();
    return response.status === 204 ? null : await response.json();
  } finally {
    clearTimeout(timeout);
  }
}
