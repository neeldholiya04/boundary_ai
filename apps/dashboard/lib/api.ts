import { clearSession, getSession } from "./auth";

export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000";

function authHeaders(): Record<string, string> {
  const session = typeof window === "undefined" ? null : getSession();
  return session ? { Authorization: `Bearer ${session.token}` } : {};
}

async function parseResponse<T>(response: Response): Promise<T> {
  if (response.status === 401 && typeof window !== "undefined" && !window.location.pathname.startsWith("/login")) {
    // Expired or missing session: back to the sign-in page.
    clearSession();
    window.location.assign("/login");
  }
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `Request failed with ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, { cache: "no-store", headers: authHeaders() });
  return parseResponse<T>(response);
}

export async function apiSend<T>(path: string, init: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...authHeaders(),
      ...(init.headers ?? {})
    }
  });
  return parseResponse<T>(response);
}
