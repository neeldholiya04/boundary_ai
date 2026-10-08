/** The signed-in session, kept in localStorage. The token is checked by the API on every request; the
 * role here only decides which pages the dashboard shows. */

export type Role = "user" | "admin";
export type Session = { token: string; username: string; role: Role };

const KEY = "boundary.session";

export function getSession(): Session | null {
  try {
    const raw = window.localStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as Session) : null;
  } catch {
    return null;
  }
}

export function setSession(session: Session): void {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(session));
  } catch {
    // Private mode or blocked storage: the login works for this page only.
  }
}

export function clearSession(): void {
  try {
    window.localStorage.removeItem(KEY);
  } catch {
    // nothing to clear
  }
}

/** Where a role lands after signing in. */
export function homeFor(role: Role): string {
  return role === "admin" ? "/guardrails" : "/chat";
}
