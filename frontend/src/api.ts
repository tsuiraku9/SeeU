import type { AuthResponse, PageResponse } from "./types";

let csrfToken = sessionStorage.getItem("csrf_token") || "";
export const API_UNAUTHORIZED_EVENT = "archive-api-unauthorized";

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const method = (options.method || "GET").toUpperCase();
  const headers = new Headers(options.headers);
  if (options.body && !(options.body instanceof FormData) && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (!["GET", "HEAD", "OPTIONS"].includes(method) && csrfToken) headers.set("X-CSRF-Token", csrfToken);
  const response = await fetch(`/api${path}`, { ...options, headers, credentials: "same-origin" });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ detail: response.statusText }));
    const detail = payload.detail;
    const message = typeof detail === "string" ? detail : detail?.message || "请求失败";
    if (response.status === 401) window.dispatchEvent(new Event(API_UNAUTHORIZED_EVENT));
    throw new ApiError(response.status, message);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export function normalizePage<T>(value: PageResponse<T> | T[], offset = 0, limit = 50): PageResponse<T> {
  if (Array.isArray(value)) {
    return { items: value, total: value.length, offset, limit, has_more: false };
  }
  return value;
}

export function saveAuth(auth: AuthResponse): void {
  csrfToken = auth.csrf_token;
  sessionStorage.setItem("csrf_token", csrfToken);
}

export function clearAuth(): void {
  csrfToken = "";
  sessionStorage.removeItem("csrf_token");
}
