// Thin fetch wrapper. The session token lives in sessionStorage so it is dropped when the tab closes
// (this app shows patient data on shared front-office machines).

const TOKEN_KEY = "dcc.session";

export type Session = { token: string; username: string; role: "Manager" | "Specialist"; displayName: string };

export function loadSession(): Session | null {
  try {
    const raw = sessionStorage.getItem(TOKEN_KEY);
    return raw ? (JSON.parse(raw) as Session) : null;
  } catch {
    return null;
  }
}

export function saveSession(s: Session | null) {
  try {
    if (s) sessionStorage.setItem(TOKEN_KEY, JSON.stringify(s));
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage unavailable: session stays in memory only */
  }
}

export class ApiError extends Error {
  constructor(public status: number, message: string, public body?: unknown) {
    super(message);
  }
}

let onUnauthorized: () => void = () => {};
export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn;
}

export async function api<T>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const session = loadSession();
  const headers = new Headers(init.headers);
  if (session) headers.set("Authorization", `Bearer ${session.token}`);
  let body = init.body;
  if (init.json !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(init.json);
  }
  const res = await fetch(`/api${path}`, { ...init, headers, body });
  if (res.status === 401 && path !== "/auth/login") {
    onUnauthorized();
    throw new ApiError(401, "Your session ended. Sign in again.");
  }
  const text = await res.text();
  const data = text ? safeJson(text) : null;
  if (!res.ok) {
    let msg = "";
    if (data && typeof data === "object") {
      const d = data as { error?: unknown; detail?: unknown; errors?: Record<string, string[]> };
      msg = d.error ? String(d.error) : d.detail ? String(d.detail) : d.errors ? Object.values(d.errors).flat().join(" ") : "";
    }
    if (!msg) msg = res.status === 403 ? "You don't have access to this." : res.status === 404 ? "Not found." : `Request failed (${res.status}).`;
    throw new ApiError(res.status, msg, data);
  }
  return data as T;
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

export async function login(username: string, password: string): Promise<Session> {
  const t = await api<{ access_token: string }>("/auth/login", { method: "POST", json: { username, password } });
  saveSession({ token: t.access_token, username, role: "Specialist", displayName: username });
  const me = await api<{ username: string; role: Session["role"]; display_name: string }>("/auth/me");
  const s: Session = { token: t.access_token, username: me.username, role: me.role, displayName: me.display_name };
  saveSession(s);
  return s;
}
