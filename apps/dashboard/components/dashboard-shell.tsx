"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { ReactNode, useEffect, useState } from "react";

const NAV = [
  { href: "/chat", label: "Chat" },
  { href: "/policies", label: "Policies" },
  { href: "/guardrails", label: "Guardrails" },
  { href: "/playground", label: "Playground" },
  { href: "/approvals", label: "Approvals" },
  { href: "/logs", label: "Logs" },
  { href: "/mcp-servers", label: "MCP" }
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

  useEffect(() => {
    setAdmin(isAdminHost(window.location.hostname));
  }, []);

  const items = admin ? NAV : NAV.filter((item) => item.href === "/playground");

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <h1>Boundary</h1>
          <span className="brand-mark">{admin === false ? "Guardrails playground" : "Guarded Workspace"}</span>
        </div>

        {admin !== null && (
          <nav className="nav">
            {items.map((item) => (
              <Link
                key={item.href}
                href={item.href}
                className={`nav-link${pathname === item.href ? " active" : ""}`}
              >
                {item.label}
              </Link>
            ))}
          </nav>
        )}
      </aside>

      <main className="content">{children}</main>
    </div>
  );
}
