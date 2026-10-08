"use client";

import {
  ChatCircleText,
  CheckSquareOffset,
  Flask,
  ListBullets,
  Plugs,
  ShieldCheck
} from "@phosphor-icons/react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { ReactNode, useCallback, useEffect, useState } from "react";

import { apiGet } from "@/lib/api";
import { Approval } from "@/lib/types";
import { useLiveRefresh } from "@/lib/use-live-refresh";

const NAV = [
  { href: "/chat", label: "Chat", icon: ChatCircleText },
  { href: "/guardrails", label: "Guardrails", icon: ShieldCheck },
  { href: "/approvals", label: "Approvals", icon: CheckSquareOffset },
  { href: "/logs", label: "Logs", icon: ListBullets },
  { href: "/mcp-servers", label: "Tools", icon: Plugs },
  { href: "/playground", label: "Playground", icon: Flask }
];

/** The admin pages live on an `admin.` host (password-protected by the proxy) or on localhost.
 * Anywhere else this is the public playground, so only the Playground link is shown. Access is
 * enforced by the proxy, not here: this only keeps the public site from advertising locked pages. */
function isAdminHost(hostname: string): boolean {
  return ["localhost", "127.0.0.1", "[::1]"].includes(hostname) || hostname.startsWith("admin.");
}

export function DashboardShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const [admin, setAdmin] = useState<boolean | null>(null); // unknown until mounted (no SSR flash)
  const [pending, setPending] = useState(0);

  useEffect(() => {
    setAdmin(isAdminHost(window.location.hostname));
  }, []);

  // Pending approvals are the one thing that needs a person, so the nav says how many are waiting.
  const loadPending = useCallback(() => {
    if (!admin) return;
    apiGet<Approval[]>("/api/approvals")
      .then((list) => setPending(list.filter((a) => a.status === "pending").length))
      .catch(() => undefined);
  }, [admin]);
  useEffect(loadPending, [loadPending]);
  useLiveRefresh(loadPending);

  const items = admin ? NAV : NAV.filter((item) => item.href === "/playground");

  return (
    <div className="shell">
      <aside className="side">
        <div className="side-brand">
          <span className="brand-mark" aria-hidden>
            <ShieldCheck size={16} weight="bold" />
          </span>
          <span>Boundary</span>
        </div>

        {admin !== null && (
          <nav className="nav" aria-label="Main">
            {items.map(({ href, label, icon: Icon }) => {
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
        )}

        {admin === false && <p className="side-foot">Guardrails playground</p>}
      </aside>

      <main className="main">{children}</main>
    </div>
  );
}
