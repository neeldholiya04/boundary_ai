"use client";

import { CheckSquareOffset, Flask, ListBullets, Plugs, ShieldCheck, SignOut } from "@phosphor-icons/react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { ReactNode, useCallback, useEffect, useState } from "react";

import { apiGet } from "@/lib/api";
import { Session, clearSession, getSession, homeFor } from "@/lib/auth";
import { Approval } from "@/lib/types";
import { useLiveRefresh } from "@/lib/use-live-refresh";

// The admin dashboard. The chat is for user accounts and has its own full-screen layout.
const NAV = [
  { href: "/guardrails", label: "Guardrails", icon: ShieldCheck },
  { href: "/approvals", label: "Approvals", icon: CheckSquareOffset },
  { href: "/logs", label: "Logs", icon: ListBullets },
  { href: "/mcp-servers", label: "Tools", icon: Plugs },
  { href: "/playground", label: "Playground", icon: Flask }
];

/** Where this session may be: users only on /chat, admins anywhere but /chat, nobody signed out
 * anywhere but /login. Returns the page to send them to instead, or null when they may stay. The API
 * enforces the same split; this only keeps people off pages that would fail. */
function redirectFor(session: Session | null, pathname: string): string | null {
  if (pathname.startsWith("/login")) return null;
  if (!session) return "/login";
  if (pathname === "/") return homeFor(session.role);
  if (session.role === "user" && !pathname.startsWith("/chat")) return "/chat";
  if (session.role === "admin" && pathname.startsWith("/chat")) return "/guardrails";
  return null;
}

export function signOut() {
  clearSession();
  window.location.assign("/login");
}

export function DashboardShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [session, setSessionState] = useState<Session | null | undefined>(undefined); // undefined: not read yet
  const [pending, setPending] = useState(0);

  useEffect(() => {
    const current = getSession();
    setSessionState(current);
    const target = redirectFor(current, pathname);
    if (target && target !== pathname) router.replace(target);
  }, [pathname, router]);

  const admin = session?.role === "admin";
  // Pending approvals are the one thing that needs a person, so the nav says how many are waiting.
  const loadPending = useCallback(() => {
    if (!admin) return;
    apiGet<Approval[]>("/api/approvals")
      .then((list) => setPending(list.filter((a) => a.status === "pending").length))
      .catch(() => undefined);
  }, [admin]);
  useEffect(loadPending, [loadPending]);
  useLiveRefresh(loadPending);

  // Nothing until the session is read and the page is one this session may see (no flash of a page
  // that would only redirect).
  if (pathname.startsWith("/login")) return <>{children}</>;
  if (session === undefined || redirectFor(session, pathname)) return null;
  if (!admin) return <>{children}</>;

  return (
    <div className="shell">
      <aside className="side">
        <div className="side-brand">
          <span className="brand-mark" aria-hidden>
            <ShieldCheck size={16} weight="bold" />
          </span>
          <span>Boundary</span>
        </div>

        <nav className="nav" aria-label="Main">
          {NAV.map(({ href, label, icon: Icon }) => {
            const current = pathname === href || (href === "/guardrails" && pathname === "/policies");
            return (
              <Link key={href} href={href} title={label} className="nav-item" aria-current={current ? "page" : undefined}>
                <Icon size={18} weight={current ? "fill" : "regular"} aria-hidden />
                <span className="nav-label">{label}</span>
                {href === "/approvals" && pending > 0 && (
                  <span className="nav-count" aria-label={`${pending} waiting`}>
                    {pending}
                  </span>
                )}
              </Link>
            );
          })}
        </nav>

        <div className="side-foot side-account">
          <span className="subtle small" title="Signed in as admin">
            {session?.username}
          </span>
          <button className="btn btn-ghost btn-sm" onClick={signOut} title="Sign out">
            <SignOut size={14} aria-hidden /> <span className="nav-label">Sign out</span>
          </button>
        </div>
      </aside>

      <main className="main">{children}</main>
    </div>
  );
}
