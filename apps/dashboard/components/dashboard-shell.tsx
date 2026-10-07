"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { ReactNode } from "react";

const NAV = [
  { href: "/chat", label: "Chat" },
  { href: "/policies", label: "Policies" },
  { href: "/guardrails", label: "Guardrails" },
  { href: "/playground", label: "Playground" },
  { href: "/approvals", label: "Approvals" },
  { href: "/logs", label: "Logs" },
  { href: "/mcp-servers", label: "MCP" }
];

export function DashboardShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <h1>Boundary</h1>
          <span className="brand-mark">Guarded Workspace</span>
        </div>

        <nav className="nav">
          {NAV.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className={`nav-link${pathname === item.href ? " active" : ""}`}
            >
              {item.label}
            </Link>
          ))}
        </nav>
      </aside>

      <main className="content">{children}</main>
    </div>
  );
}
