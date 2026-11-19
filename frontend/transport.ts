export type WriteMethod = "POST" | "PATCH" | "DELETE";
export interface RequestOptions { requestKey?: string | null }

export const statePath = "/api/state?compact=1";

export async function apiRequest(path: string, signed: string, method: "GET" | WriteMethod = "GET",
  body?: object, { requestKey }: RequestOptions = {}): Promise<unknown> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 18000);
  try {
    const response = await fetch(path, {
      method,
      signal: controller.signal,
      credentials: "omit",
      cache: "no-store",
      headers: {
        Authorization: "tma " + signed,
        ...(requestKey ? {"Idempotency-Key": requestKey} : {}),
        ...(body ? {"Content-Type": "application/json"} : {}),
      },
      ...(body ? {body: JSON.stringify(body)} : {}),
    });
    if (!response.ok) throw new Error(String(response.status));
    if (path.endsWith(".ics")) return await response.blob();
    return response.status === 204 ? null : await response.json();
  } finally {
    clearTimeout(timeout);
  }
}
